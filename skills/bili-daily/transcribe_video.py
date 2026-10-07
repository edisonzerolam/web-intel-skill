#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
transcribe_video: B站视频转录管道（字幕优先 · 多级降级）
=========================================================
融合自 chubbyskills v0.13.0（github.com/chubbyguan/chubbyskills，MIT）的
bilibili-transcribe 管道设计（字幕优先 → 音频转录，yt-dlp 通道 + VTT 解析 +
SenseVoice-Small 引擎），并保留 bili-daily 的登录态 API 主通道与严格合规框架
（TC-20260811-BILI / BILI3 终审：限速、预算、生命周期、红线）。

通道降级顺序（从快到慢）：
  T1 API 字幕    /x/player/wbi/v2（带登录 cookie，复用 fetch_subtitles）——需登录
  T2 yt-dlp 字幕  --write-subs --write-auto-subs（免登录尝试；带 cookie 提升命中）
  T3 音频+SenseVoice-Small（funasr；chubbyskills 引擎；缺依赖给出安装提示并降级）
       默认优先 ROCm GPU：把推理交给 D:\\rocm101-venv 里的常驻 worker 子进程
       （主环境 torch 是 +cpu 版，无法与 ROCm 栈同进程共存），不可用则回落 CPU。
  T4 音频+faster-whisper（复用 asr_fallback 引擎；现有依赖）
  T5 音频+whisper-large-v3-turbo（P2 质量兜底，**仅 T3 失败后**才试；同一 GPU worker，
       懒加载；不产 meta、无标点、速度约为主引擎 1/2.6，故不作默认）

输出：frontmatter Markdown（字段对齐 chubby markdown_schema_v1：title/type/platform/
source/author/created/tags/transcriber…），正文含水印「个人学习研究·勿公开传播」。

用法：
  python transcribe_video.py --bvid BV1xx411c7mD --cookies cookies.txt
  python transcribe_video.py --url https://www.bilibili.com/video/BV1xx411c7mD/ --cookies cookies.txt --out-dir out/transcripts
  python transcribe_video.py --list urls.txt --cookies cookies.txt            # 批量：每行 BV 或 URL
  python transcribe_video.py --bvid BVxxx --cookies cookies.txt --engine sensevoice   # 强制 T3 引擎
  python transcribe_video.py --bvid BVxxx --cookies cookies.txt --no-t5               # 关闭 T5 兜底
  python transcribe_video.py --bvid BVxxx --cookies cookies.txt --no-api-subtitle     # 跳过 T1
"""
import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

# 技能根：允许直接运行（python transcribe_video.py）时找到同目录模块
SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
if SKILL_DIR not in sys.path:
    sys.path.insert(0, SKILL_DIR)

# 模型缓存重定向到纯 ASCII 路径（Windows 实测：sentencepiece/ctranslate2 等 C++ 扩展
# 用 GBK 处理路径，用户主目录「<user>」中文路径导致 Not found——2026-09-28 实锤）。
# 在任何 funasr import 前设置；C:/ms_cache/hub 已预置两份模型。
MS_CACHE_ROOT = os.environ.get("MODELSCOPE_CACHE") or "C:/ms_cache"
if os.path.isdir(os.path.join(MS_CACHE_ROOT, "hub")):
    os.environ["MODELSCOPE_CACHE"] = MS_CACHE_ROOT

# CPU 线程上限（2026-10-05 实测）：ASR/标点模型几乎无法并行化——ct-punc 在
# 12 核上仅比单核快 29%（1167→1507 字/秒），却会占满 6~12 个核，导致整机卡顿。
# 默认限 4 线程换取系统响应性；需更大吞吐时用 WEBINTEL_CPU_THREADS 覆盖。
# 必须在 torch 首次加载前设置环境变量（OMP/MKL 在 torch import 时读取）。
_CPU_THREADS = os.environ.get("WEBINTEL_CPU_THREADS", "4")
for _v in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS",
           "NUMEXPR_NUM_THREADS"):
    os.environ.setdefault(_v, _CPU_THREADS)

import fetch_subtitles as fs  # T1：字幕轨 API（带登录 cookie）

# 显式收紧 torch 线程（环境变量只对尚未初始化的后端生效；torch 可能已被
# 上层 import 过，故这里再显式设一次，保证 _CPU_THREADS 一定生效）。
try:
    import torch as _torch
    _torch.set_num_threads(int(_CPU_THREADS))
except (ImportError, ValueError, RuntimeError):
    pass  # 无 torch（纯字幕通道）时无需限制

UA = fs.UA
YTDLP_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# 字幕语言优先级（中文优先；与 chubbyskills vtt.py 一致的口径）
SUB_LANGS = "zh-Hans,zh-CN,zh,zh-Hant,ai-zh,en"
SUB_PRIORITY = ("zh-hans", "zh-cn", "zh", "zh-hant", "ai-zh", "en")

WATERMARK = "个人学习研究·勿公开传播"
VIEW_API = "https://api.bilibili.com/x/web-interface/view"


# ---------------------------------------------------------------- 工具函数

def extract_bvid(text: str) -> str:
    """从 URL 或裸 BV 号中提取 bvid。"""
    m = re.search(r"(BV[0-9A-Za-z]+)", text or "")
    if m:
        return m.group(1)
    raise ValueError(f"无法从输入提取 BV 号：{text}")


def view_info(bvid, cookies):
    """view 接口取 title/UP名/cid。只取 name，不落盘 uid（合规红线）。"""
    r = fs._session_get(VIEW_API, params={"bvid": bvid}, cookies=cookies)
    if r.status_code in (412, 429):
        raise SystemExit(f"[风控] HTTP {r.status_code}：当日停止该接口，次日再试。")
    d = r.json()
    if d.get("code") != 0:
        return {"title": bvid, "uploader": "", "cid": None}
    data = d.get("data") or {}
    owner = (data.get("owner") or {}) or {}
    return {
        "title": data.get("title") or bvid,
        "uploader": owner.get("name") or "",
        "cid": data.get("cid"),
    }


# ---------------------------------------------------------------- T1：API 字幕

def t1_api_subtitle(bvid, cid, cookies):
    """T1：/x/player/wbi/v2 字幕轨（需登录）。返回 (text, lang) 或 None。"""
    if not cid:
        return None
    try:
        tracks = fs.get_subtitle_tracks(bvid, cid, None, cookies)
    except SystemExit:
        raise
    if not tracks:
        return None
    track = next((s for s in tracks if "ai" in str(s.get("lan", ""))), tracks[0])
    data = fs.download_subtitle_json(track["subtitle_url"], cookies)
    if not data or not data.get("body"):
        return None
    body = data["body"]
    text = "\n".join((seg.get("content") or "").strip() for seg in body if seg.get("content"))
    if len(text) < 50:
        return None
    lang = "zh" if re.search(r"[一-鿿]", text) else "en"
    return text, lang


# ---------------------------------------------------------------- T2：yt-dlp 字幕

def _parse_vtt(path) -> str:
    """VTT/SRT → 纯文本：去序号/时间轴/标签/连续重复行（借鉴 chubbyskills vtt.py，MIT）。

    B站自动字幕实为 srt（yt-dlp --sub-format vtt 会降级回 srt）；此解析兼容两种：
    srt 的序号行是纯数字、时间行含 -->，vtt 的 WEBVTT/NOTE 行被显式跳过。
    """
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()
    out = []
    for line in lines:
        line = line.strip()
        if not line or line == "WEBVTT" or "-->" in line:
            continue
        if line.isdigit() or line.startswith(("Kind:", "Language:", "NOTE")):
            continue
        line = re.sub(r"<[^>]+>", "", line).replace("&nbsp;", " ").strip()
        if not line or (out and out[-1] == line):
            continue
        out.append(line)
    return "\n".join(out)


def _pick_subtitle(files, priority) -> str:
    """按语言优先级挑字幕文件（借鉴 chubbyskills vtt.py）。"""
    def rank(p):
        name = os.path.basename(p).lower()
        for i, tag in enumerate(priority):
            if tag in name:
                return i
        return 99
    return sorted(files, key=rank)[0]


def t2_ytdlp_subtitle(bvid, cookies_file=None):
    """T2：yt-dlp 抓官方/自动字幕。

    实测（2026-09-28）：无 cookie 时 yt-dlp 的 wbi 签名请求被 412 风控（与
    bili-daily「字幕列表对游客隐藏」结论一致）；带 cookie 正常（ai-zh 自动字幕）。
    因此 cookies_file 为推荐参数；缺省仅作免登录尝试，失败静默返回 None。
    """
    tmpdir = tempfile.mkdtemp(prefix="bili-ytdlp-sub-")
    try:
        cmd = ["yt-dlp", "--no-check-certificates", "--user-agent", YTDLP_UA,
               "--referer", "https://www.bilibili.com",
               "--skip-download", "--write-subs", "--write-auto-subs",
               "--sub-langs", SUB_LANGS,
               "-o", os.path.join(tmpdir, "sub.%(ext)s"),
               f"https://www.bilibili.com/video/{bvid}/"]
        if cookies_file:
            cmd[1:1] = ["--cookies", cookies_file]
        for attempt in range(2):
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if proc.returncode == 0:
                break
            if attempt == 0 and "412" in proc.stderr:
                time.sleep(3)  # 412 风控：短暂退避后重试一次
        if proc.returncode != 0:
            return None
        files = glob.glob(os.path.join(tmpdir, "*.vtt")) + glob.glob(os.path.join(tmpdir, "*.srt"))
        if not files:
            return None
        chosen = _pick_subtitle(files, SUB_PRIORITY)
        text = _parse_vtt(chosen)
        if len(text) < 50:
            return None
        lang = "zh" if re.search(r"[一-鿿]", text) else "en"
        return text, lang
    except (subprocess.TimeoutExpired, OSError):
        return None
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------- T3/T4：音频转录

def _download_dash_audio(bvid, cid, cookies, dest):
    """DASH 音频主通道：playurl 取流（限速走 fs）→ requests 流式下载。

    独立实现（不依赖 asr_fallback.download_audio）：B站 CDN 在本机 schannel
    吊销检查离线时握手失败（UNEXPECTED_EOF，与 curl CRYPT_E_REVOCATION_OFFLINE
    同因），此处 verify=False + 抑制警告，等价于 yt-dlp --no-check-certificates。
    """
    from asr_fallback import get_playurl_audio
    import requests
    try:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    except ImportError:
        pass
    url = get_playurl_audio(bvid, cid, None, cookies)
    if not url:
        return False
    h = dict(fs.HEADERS)
    h["Referer"] = "https://www.bilibili.com/"
    with requests.get(url, headers=h, cookies=cookies, timeout=(15, 120),
                      stream=True, verify=False) as r:
        if r.status_code != 200:
            print(f"  [下载] DASH 音频 HTTP {r.status_code}", file=sys.stderr)
            return False
        n = 0
        with open(dest, "wb") as f:
            for chunk in r.iter_content(65536):
                f.write(chunk)
                n += len(chunk)
    return n > 0


def _find_local_model(cache_name):
    """在 modelscope/HF 本地缓存中查找含指定模型文件的快照目录。

    cache_name 例：'models--Systran--faster-whisper-small'（modelscope hub 命名）
    或 'models/iic/SenseVoiceSmall'。命中返回快照目录路径，未命中返回 None。
    先查 MODELSCOPE_CACHE（纯 ASCII，规避 C++ 扩展中文路径 bug），再查默认缓存。
    """
    import glob as _glob
    env_root = os.environ.get("MODELSCOPE_CACHE")
    search_roots = []
    if env_root:
        search_roots.append(os.path.join(env_root, "hub"))
    search_roots += [
        os.path.expanduser("~/.cache/modelscope/hub"),
        os.path.expanduser("~/.cache/huggingface/hub"),
    ]
    for root in search_roots:
        p = os.path.join(root, cache_name)
        if not os.path.isdir(p):
            continue
        for s in _glob.glob(os.path.join(p, "snapshots", "*")) + [p]:
            if os.path.exists(os.path.join(s, "model.bin")) or \
               os.path.exists(os.path.join(s, "model.pt")) or \
               os.path.exists(os.path.join(s, "model.pb")):
                return s
    return None


def t3_sensevoice(audio_path):
    """T3：SenseVoice-Small（funasr；chubbyskills 引擎）。缺依赖给提示并返回 None。

    注意：传模型 id（"iic/SenseVoiceSmall"）由 funasr 自己管理 modelscope 缓存与
    加载（实测 2026-09-28：手动 local_dir 预下载根目录结构会被 funasr 1.4.x 的
    remote-code 加载逻辑拒绝，改 model id 后自动走 snapshots 标准缓存）。
    """
    try:
        from funasr import AutoModel
        from funasr.utils.postprocess_utils import rich_transcription_postprocess
    except ImportError:
        print("  [T3] 缺 funasr：pip install funasr（本机已装 torch/modelscope/torchaudio）"
              "；降级 T4 faster-whisper。", file=sys.stderr)
        return None
    print("  [T3] 加载 SenseVoice-Small（首次运行自动从 modelscope 拉取）...", file=sys.stderr)
    # funasr 内部读取 MODELSCOPE_CACHE（顶部已注入 C:/ms_cache，纯 ASCII 路径）
    model = AutoModel(model="iic/SenseVoiceSmall", trust_remote_code=True,
                      vad_model="fsmn-vad",
                      vad_kwargs={"max_single_segment_time": 30000}, device="cpu")
    result = model.generate(input=audio_path, language="zh", use_itn=True, batch_size_s=60)
    text = ""
    if result:
        for record in result:
            if "text" in record:
                text += rich_transcription_postprocess(record["text"]) + "\n\n"
    return text.strip() or None


def t3_sensevoice_rocm_meta(audio_path, worker=None):
    """T3-ROCm：把 SenseVoice 推理交给 ROCm 虚拟环境里的常驻 worker（GPU）。

    主环境 torch 是 2.12.1+cpu（无 GPU），ROCm 栈在独立 venv
    （D:\\rocm101-venv，torch 2.14.0+rocm10.1.0）。故走子进程 + JSON 协议，
    见 t3_rocm_client.py / t3_rocm_worker.py。失败返回 (None, {})，由调用方回落 CPU。

    worker 参数：批量模式下由 WorkerPool 显式传入（池/单例互斥，避免多起进程）；
    为 None 时退化为进程内单例（单条模式）。

    返回 (text, meta)：meta 为 P2b 的情感/事件元数据
    （{"emotions": {...}, "events": {...}, "has_music": bool,
      "music_ratio": float, "emotion_segments": [{from,to,emotion,events}]}），
    失败或 CPU 通道下为空 dict。
    """
    try:
        import t3_rocm_client as cli
    except ImportError as e:
        print(f"  [T3-ROCm] 客户端模块不可用（{e}），回落 CPU", file=sys.stderr)
        return None, {}

    w = worker if worker is not None else cli.get_worker()
    if w is None:
        return None, {}
    # 池成员已由池统一做静态检查；单例仍逐次检查（廉价）
    if worker is None and not w.available():
        return None, {}

    # 只在真正需要拉起进程时提示等待，避免批量时每条都打印误导性的「首次」。
    # worker 可能是单个 T3RocmWorker，也可能是 WorkerPool（池自报 first_time()）。
    if hasattr(w, "first_time"):
        show_hint = w.first_time()
    else:
        show_hint = getattr(w, "proc", None) is None or w.proc.poll() is not None
    if show_hint:
        tag = f"#{w.tag} " if getattr(w, "tag", 0) else ""
        print(f"  [T3-ROCm] {tag}提交 GPU 转录（首次会等待模型就绪 ≈15s）...",
              file=sys.stderr)
    # with_timestamps=True 才能拿到 sentence_info → emotion_segments 的区间。
    # 配对基准（2026-10-07，300s 音频、GPU 常驻、交替各 3 轮）：
    #   不带 4.63s(中位) vs 带 4.78s(中位) → +3.2%，在噪声量级内，故恒开。
    msg = w.transcribe_full(audio_path, with_timestamps=True) if hasattr(w, "transcribe_full") else None
    text = (msg.get("text") or None) if msg else None
    meta = (msg.get("meta") or {}) if msg else {}
    if text:
        print(f"  [T3-ROCm] GPU 转写完成 {len(text)} 字", file=sys.stderr)
    return text, meta


def t3_sensevoice_rocm(audio_path, worker=None):
    """T3-ROCm 的**文本薄封装**（保持 P1 契约：只返回文本）。

    需要情感/事件元数据（P2b）时用 t3_sensevoice_rocm_meta()。
    """
    return t3_sensevoice_rocm_meta(audio_path, worker=worker)[0]


def _t3_dispatch(audio_path, worker=None, want_meta=False):
    """T3 调度核心。返回 (text, backend, meta)。

    want_meta=False 时**只走 t3_sensevoice_rocm**（文本通道），
    使 P1 既有行为逐字节不变；want_meta=True 时走 t3_sensevoice_rocm_meta
    （同一 worker、同一次推理，只是把 meta 一并取回，无额外算力）。
    """
    mode = os.environ.get("WEBINTEL_T3_DEVICE_MODE", "auto").lower()
    if mode not in ("auto", "rocm", "cpu"):
        print(f"  [T3] 未知 WEBINTEL_T3_DEVICE_MODE={mode!r}，按 auto 处理", file=sys.stderr)
        mode = "auto"

    if mode in ("auto", "rocm"):
        if want_meta:
            text, meta = t3_sensevoice_rocm_meta(audio_path, worker=worker)
        else:
            text, meta = t3_sensevoice_rocm(audio_path, worker=worker), {}
        if text:
            return text, "SenseVoice-Small(GPU)", meta
        if mode == "rocm":
            print("  [T3] ROCm 通道不可用，且已指定 rocm-only：返回空", file=sys.stderr)
            return None, "", {}
        print("  [T3] ROCm 不可用，回落 CPU funasr", file=sys.stderr)
    return t3_sensevoice(audio_path), "SenseVoice-Small", {}


def t3_sensevoice_dispatch(audio_path, worker=None):
    """T3 调度：默认优先 ROCm GPU，不可用/失败则回落 CPU funasr。

    返回 (text, backend)，backend ∈ {"SenseVoice-Small(GPU)", "SenseVoice-Small"}，
    供 build_markdown 的 transcriber 字段如实记录实际用的通道。

    环境变量 WEBINTEL_T3_DEVICE_MODE：
      auto（默认）先试 ROCm 再回落 CPU；rocm 只用 ROCm；cpu 只用 CPU。

    worker：批量模式下由 WorkerPool 传入的常驻 worker（见 t3_sensevoice_rocm）。
    """
    text, backend, _ = _t3_dispatch(audio_path, worker=worker)
    return text, backend


def t3_sensevoice_dispatch_meta(audio_path, worker=None):
    """同 t3_sensevoice_dispatch，但额外返回 P2b 的 meta（CPU 通道为 {}）。

    返回 (text, backend, meta)。
    """
    return _t3_dispatch(audio_path, worker=worker, want_meta=True)


def t5_whisper_turbo(audio_path, worker=None):
    """T5（P2）：whisper-large-v3-turbo 质量兜底 —— 只在 T3 返回 None 后尝试。

    为什么需要它：T1/T2 无字幕、T3 又失败时，原来的兜底是 T4 faster-whisper
    small（CPU，识别质量一般且吃 CPU）。T5 用同一套 ROCm 常驻 worker 跑
    large-v3-turbo，**不额外起进程**，质量显著更好。

    为什么只作兜底不作默认（2026-10-07 探针实测，300.7s 音频）：
      - 速度：turbo 12.36s vs SenseVoice 4.78s（稳态）→ **慢 2.6x**
      - 输出：turbo 无标点，且尾部会幻觉出超出音频时长的内容
        （出现 (298.48, 328.46) 的「优优独播剧场——YoYo Television Series Exclusive」）
      - 显存：额外 1.79GB（懒加载，只有真走到 T5 才付这个成本）
    故定位为「T3 失败时的质量兜底」，其下仍有 T4 CPU 兜底。

    开关：WEBINTEL_T5_ENABLE（默认 "1"；置 "0"/"false"/"no" 关闭）。
    设备模式沿用 WEBINTEL_T3_DEVICE_MODE：cpu 时直接跳过（T5 只在 GPU 上有意义）。
    worker 参数：批量模式下由 WorkerPool 传入；为 None 时用进程内单例。
    """
    enabled = (os.environ.get("WEBINTEL_T5_ENABLE", "1") or "1").strip().lower()
    if enabled in ("0", "false", "no", "off"):
        print("  [T5] 已由 WEBINTEL_T5_ENABLE 关闭，跳过", file=sys.stderr)
        return None
    mode = os.environ.get("WEBINTEL_T3_DEVICE_MODE", "auto").lower()
    if mode == "cpu":
        print("  [T5] WEBINTEL_T3_DEVICE_MODE=cpu，跳过 GPU 兜底", file=sys.stderr)
        return None

    try:
        import t3_rocm_client as cli
    except ImportError as e:
        print(f"  [T5] 客户端模块不可用（{e}），跳过", file=sys.stderr)
        return None

    w = worker if worker is not None else cli.get_worker()
    if w is None:
        return None
    if worker is None and not w.available():
        return None

    print("  [T5] 提交 whisper-large-v3-turbo 质量兜底（首次会额外加载模型 ≈2s）...",
          file=sys.stderr)
    if not hasattr(w, "transcribe_full"):
        return None
    msg = w.transcribe_full(audio_path, with_timestamps=False,
                            engine="whisper_turbo")
    text = (msg.get("text") or None) if msg else None
    if text:
        print(f"  [T5] GPU 兜底转写完成 {len(text)} 字", file=sys.stderr)
    return text


def t4_faster_whisper(audio_path):
    """T4：faster-whisper（CTranslate2 CPU）。

    模型优先用本地 modelscope 缓存（Systran/faster-whisper-small，~464MB），
    避免触发 HuggingFace 下载（本机 HF 不可达）。未缓存时给预下载指引。
    """
    local = _find_local_model("models--Systran--faster-whisper-small")
    if not local:
        print("  [T4] faster-whisper 模型未缓存。预下载命令：", file=sys.stderr)
        print("        modelscope download --model Systran/faster-whisper-small --local_dir ~/.cache/modelscope/hub/models--Systran--faster-whisper-small", file=sys.stderr)
        return None
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print("  [T4] 缺 faster-whisper：pip install faster-whisper", file=sys.stderr)
        return None
    try:
        model = WhisperModel(local, device="cpu", compute_type="int8",
                             cpu_threads=int(_CPU_THREADS))
        segments, _ = model.transcribe(audio_path, language="zh", vad_filter=True,
                                       beam_size=5, condition_on_previous_text=False)
        parts = [s.text.strip() for s in segments if s.text.strip()]
    except Exception as e:
        print(f"  [T4] faster-whisper 推理失败：{str(e)[:120]}", file=sys.stderr)
        return None
    if not parts:
        return None
    return "\n".join(parts)


# ---------------------------------------------------------------- 输出

def build_markdown(title, text, bvid, uploader, transcriber, lang, audio_meta=None):
    """对齐 chubby markdown_schema_v1 字段的 frontmatter Markdown + 合规水印。

    audio_meta（P2b，可选）：SenseVoice 的情感/事件元数据。仅在非空时写入
    frontmatter 的 audio_meta 字段 —— 既有产物格式（无该字段）保持不变。
    """
    url = f"https://www.bilibili.com/video/{bvid}/"
    meta = {
        "title": title,
        "type": "note",
        "platform": "bilibili",
        "source": url,
        "bvid": bvid,
        "author": uploader,
        "created": time.strftime("%Y-%m-%d"),
        "transcriber": transcriber,
        "language": lang,
        "tags": ["B站", "转录"],
    }
    if audio_meta:
        meta["audio_meta"] = audio_meta
    lines = ["---"]
    for k, v in meta.items():
        lines.append(f"{k}: {json.dumps(v, ensure_ascii=False)}")
    lines += ["---", "", f"# {title}", ""]
    lines += [f"- 来源：{url}", f"- UP：{uploader or '未知'}", f"- 转录方式：{transcriber}"]
    lines += ["", "> ⚠️ " + WATERMARK, "", str(text or "").strip()]
    return "\n".join(lines)


def sanitize_filename(name, fallback):
    v = re.sub(r'[<>:"/\\|?*]', "", str(name or ""))
    v = re.sub(r"\s+", "-", v).strip(" -")
    return v[:60] or fallback


# ---------------------------------------------------------------- 主流程

def transcribe_one(bvid, cookies, cookies_file, out_dir, engine, no_api_subtitle,
                   budget_spend=None, worker=None):
    """单视频全管道。返回输出路径（成功）或 None。

    worker：批量模式下由 WorkerPool 传入的常驻 GPU worker；None 走进程内单例。
    """
    if budget_spend:
        budget_spend(2)  # view + 至少一个转录通道，保守计 2
    info = view_info(bvid, cookies)
    title, uploader, cid = info["title"], info["uploader"], info["cid"]
    print(f"[{bvid}] {title[:40]}  UP:{uploader or '未知'}", file=sys.stderr)

    text, lang, transcriber = None, "zh", ""
    audio_meta = None
    if not no_api_subtitle:
        print("  T1 API字幕...", file=sys.stderr)
        sub = t1_api_subtitle(bvid, cid, cookies)
        if sub:
            text, lang = sub
            transcriber = "字幕(API)"

    if text is None:
        print("  T2 yt-dlp字幕...", file=sys.stderr)
        sub = t2_ytdlp_subtitle(bvid, cookies_file)
        if sub:
            text, lang = sub
            transcriber = "字幕(yt-dlp)"

    audio_path = None
    try:
        if text is None:
            print("  T3/T4 音频转录...", file=sys.stderr)
            tmpdir = tempfile.mkdtemp(prefix="bili-asr-")
            try:
                audio_path = os.path.join(tmpdir, f"{bvid}.m4a")
                if not _download_dash_audio(bvid, cid, cookies, audio_path):
                    print("  [转录] DASH 音频不可用（可能会员内容或无流）", file=sys.stderr)
                    return None
                if engine == "sensevoice":
                    text, transcriber, audio_meta = t3_sensevoice_dispatch_meta(
                        audio_path, worker=worker)
                    if text is None:
                        # P2 降级链：T3 失败 → T5（whisper-turbo GPU 质量兜底）→ T4（CPU）
                        text = t5_whisper_turbo(audio_path, worker=worker)
                        audio_meta = None
                        if text:
                            transcriber = "whisper-turbo(GPU兜底)"
                        else:
                            text = t4_faster_whisper(audio_path)
                            transcriber = "faster-whisper(降级)"
                else:
                    text = t4_faster_whisper(audio_path)
                    transcriber = "faster-whisper"
                if not text:
                    print("  [转录] 转写为空", file=sys.stderr)
                    return None
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)  # 音频即删（生命周期）
        if not text:
            return None
        os.makedirs(out_dir, exist_ok=True)
        safe = sanitize_filename(title, bvid)
        out_path = os.path.join(out_dir, f"{safe}_{bvid}.md")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(build_markdown(title, text, bvid, uploader, transcriber, lang,
                                   audio_meta=audio_meta))
        extra = ""
        if audio_meta:
            em = audio_meta.get("emotions") or {}
            ev = audio_meta.get("events") or {}
            extra = f" | 情感{len(em)}类/事件{len(ev)}类"
            if audio_meta.get("has_music"):
                extra += " 含音乐"
        print(f"  ✅ {transcriber} | {len(text)} 字{extra} → {out_path}", file=sys.stderr)
        return out_path
    finally:
        if audio_path and os.path.exists(audio_path):
            os.remove(audio_path)


def main():
    ap = argparse.ArgumentParser(
        description="B站视频转录管道（字幕优先·多级降级；融合 chubbyskills bilibili-transcribe）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=("通道：T1 API字幕(需登录) → T2 yt-dlp字幕 → T3 SenseVoice(ROCm GPU 优先，"
                "回落 CPU) → T5 whisper-turbo(GPU 质量兜底，仅 T3 失败后) → T4 faster-whisper\n"
                "批量：--list 时 T3 走常驻 worker 池并行（默认 2，--t3-workers N 可调）\n"
                "合规：限速(pace 已加锁，并发下仍守 1.5s 全局最小间隔)/预算记账/音频即删/"
                "水印，个人学习研究·勿公开传播"))
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--bvid", help="单个 BV 号")
    src.add_argument("--url", help="单个 B站视频链接")
    src.add_argument("--list", metavar="FILE", help="批量文件：每行一个 BV 号或 URL")
    ap.add_argument("--cookies", required=True, help="Netscape cookies.txt（T1/登录态必需）")
    ap.add_argument("--out-dir", default="out/transcripts", help="输出目录（默认 out/transcripts）")
    ap.add_argument("--engine", choices=["faster-whisper", "sensevoice"], default="sensevoice",
                    help="音频转录引擎（默认 sensevoice：实测快 1.9x 且更准；"
                         "faster-whisper 作为无 funasr 时的降级）")
    ap.add_argument("--t3-device-mode", choices=["auto", "rocm", "cpu"], default=None,
                    help="T3 SenseVoice 执行设备（默认 auto：优先 ROCm GPU 常驻 worker，"
                         "不可用则回落 CPU funasr；cpu 强制不用 GPU；rocm 只用 GPU）。"
                         "等价于环境变量 WEBINTEL_T3_DEVICE_MODE")
    ap.add_argument("--t3-workers", type=int, default=None, metavar="N",
                    help="批量模式下 T3 GPU 常驻 worker 池大小（默认 2；1 = 串行）。"
                         "实测 2 并发聚合吞吐 15.7x→23.1x，4 并发 4/4 成功达 23.8x。"
                         "等价于环境变量 WEBINTEL_T3_WORKERS。仅 --list 生效。")
    ap.add_argument("--no-t5", action="store_true",
                    help="关闭 T5 质量兜底（whisper-large-v3-turbo，仅 T3 失败后才用）。"
                         "等价于环境变量 WEBINTEL_T5_ENABLE=0")
    ap.add_argument("--no-api-subtitle", action="store_true", help="跳过 T1 API 字幕通道")
    ap.add_argument("--budget", action="store_true", help="计入 req_budget 跨进程预算")
    args = ap.parse_args()

    if args.t3_device_mode:
        os.environ["WEBINTEL_T3_DEVICE_MODE"] = args.t3_device_mode
    if args.no_t5:
        os.environ["WEBINTEL_T5_ENABLE"] = "0"

    if not os.path.exists(args.cookies):
        print(f"[错误] cookies 文件不存在：{args.cookies}", file=sys.stderr)
        sys.exit(1)
    cookies = fs.load_cookies(args.cookies)
    if not cookies.get("SESSDATA"):
        print("[警告] cookies 中未找到 SESSDATA（T1 API 字幕将不可用，走 T2 起）",
              file=sys.stderr)

    budget_spend = None
    if args.budget:
        try:
            from req_budget import budget
            budget_spend = budget.spend
        except ImportError:
            print("[警告] req_budget 不可用，跳过预算记账", file=sys.stderr)

    if args.list:
        with open(args.list, encoding="utf-8") as f:
            sources = [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
    else:
        sources = [args.url or args.bvid]

    # T3 常驻 worker 池：仅 --list 批量模式启用；单条仍走进程内单例。
    # 二者互斥使用，否则会多起进程（池 N 个 + 单例 1 个）。
    import threading
    _stop = threading.Event()
    pool = None
    if args.list and len(sources) > 1:
        try:
            import t3_rocm_client as _cli
            _n = _cli.parse_pool_size(args.t3_workers)
            if _n > 1:
                pool = _cli.get_pool(size=_n)
                print(f"[批量] T3 worker 池 = {pool.size} 个常驻 GPU worker"
                      f"（--t3-workers / WEBINTEL_T3_WORKERS 可调，1 = 串行）",
                      file=sys.stderr)
            else:
                print("[批量] T3 worker 池 = 1（串行）", file=sys.stderr)
        except Exception as e:
            print(f"[批量] worker 池不可用（{type(e).__name__}: {e}），T3 走串行", file=sys.stderr)

    def _one(idx, src, worker):
        """跑一条。返回 ((kind, payload), out)；SystemExit 不外抛，登记后由主线程处理。

        并发下不可直接 raise：线程里的异常不会打断主线程提交新任务。
        """
        try:
            bvid = extract_bvid(src)
        except ValueError as e:
            print(f"[{idx}/{len(sources)}] 跳过：{e}", file=sys.stderr)
            return ("skip", src), None
        print(f"\n[{idx}/{len(sources)}] {bvid}", file=sys.stderr)
        try:
            out = transcribe_one(bvid, cookies, args.cookies, args.out_dir,
                                 args.engine, args.no_api_subtitle, budget_spend,
                                 worker=worker)
        except SystemExit as e:
            _stop.set()      # 风控/预算硬停：立即停发新任务
            return ("stop", e), None
        except Exception as e:
            print(f"  ❌ {bvid}：{str(e)[:120]}", file=sys.stderr)
            out = None
        return ("ok", bvid), out

    ok, failed = [], []
    if pool is not None:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        results = {}
        with ThreadPoolExecutor(max_workers=pool.size) as ex:
            futs = {}
            for i, s in enumerate(sources, 1):
                if _stop.is_set():
                    break
                futs[ex.submit(_one, i, s, pool)] = i
            for fu in as_completed(futs):
                i = futs[fu]
                try:
                    results[i] = fu.result()
                except Exception as e:          # 线程内的意外异常
                    print(f"  ❌ {sources[i-1]}：{type(e).__name__}: {str(e)[:100]}",
                          file=sys.stderr)
                    results[i] = (("ok", sources[i - 1]), None)
                if results[i][0][0] == "stop":
                    _stop.set()
        for i in sorted(results):
            kind, payload = results[i][0]
            out = results[i][1]
            if kind == "stop":
                raise payload
            if out:
                ok.append(out)
            else:
                failed.append(payload)
        for i, s in enumerate(sources, 1):      # 未调度/未完成
            if i not in results:
                failed.append(s)
        try:
            import t3_rocm_client as _cli
            _cli.close_pool()
        except Exception:
            pass
    else:
        for i, s in enumerate(sources, 1):
            (kind, payload), out = _one(i, s, None)
            if kind == "stop":
                raise payload
            if out:
                ok.append(out)
            else:
                failed.append(payload)

    print(f"\n[完成] 成功 {len(ok)} / 失败 {len(failed)}", file=sys.stderr)
    for p in ok:
        print(p)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
