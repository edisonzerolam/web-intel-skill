#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
asr_fallback: 无字幕轨视频的本地 ASR 兜底转写
==============================================
按终审 TC-20260811-BILI3 V1-V3 实现。

主通道：复用 /x/player/wbi/v2 响应内嵌 dash.audio（与字幕同请求同会话，0 额外 API 请求）
备通道：yt-dlp --cookies（retries2/sleep1.5/禁-F）
引擎：T3 ROCm GPU（SenseVoice，段级时间戳）优先 → faster-whisper small/int8 CPU 兜底
记账：asr_state.json 占位 → 失败回补名额 → 仅成功转写消耗日限 5
白名单：bvid∈当日快照 ∩ 无字幕轨 ∩ 非会员，任一不满足硬失败（无兜底）
生命周期：音频转写后立即删除；转写文本仅内存使用，落盘仅统计字段

用法：
  from asr_fallback import transcribe_videos
  segments = transcribe_videos(videos, cookies, tmp_dir, budget_spend=budget.spend)
"""
import json
import os
import time

# CPU 线程上限（2026-10-05 实测）：whisper 类模型在 12 核上并行收益很低，
# 却会占满全部核心导致整机卡顿。默认限 4；用 WEBINTEL_CPU_THREADS 覆盖。
# 必须在 torch/ctranslate2 首次加载前设置环境变量。
_CPU_THREADS = os.environ.get("WEBINTEL_CPU_THREADS", "4")
for _v in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS",
           "NUMEXPR_NUM_THREADS"):
    os.environ.setdefault(_v, _CPU_THREADS)

DAILY_LIMIT = 5
STATE_FILE = "asr_state.json"
FALLBACK_BUDGET = 2  # yt-dlp 每视频保守计 2 请求


def _sanitize_proxy_env():
    """剥掉 NO_PROXY 里的方括号 IPv6 项。

    2026-10-07 实测（本机）：环境变量 `NO_PROXY` 含 `[::1]`，httpx 在构造 Client
    解析代理白名单时抛 `httpx.InvalidURL: Invalid port: ':1]'`。
    faster-whisper 用模型名（如 "small"）时会经 huggingface_hub → httpx，
    于是**加载模型即崩**，CPU 兜底路径完全不可用。
    这里把 `[::1]` 归一为 `::1`（语义不变：都是 IPv6 环路地址），httpx 即可正常解析。
    """
    for key in ("NO_PROXY", "no_proxy"):
        raw = os.environ.get(key)
        if not raw:
            continue
        items = []
        for item in raw.split(","):
            it = item.strip()
            if not it:
                continue
            if it.startswith("[") and it.endswith("]"):
                it = it[1:-1]
            if it not in items:
                items.append(it)
        os.environ[key] = ",".join(items)


_sanitize_proxy_env()


def load_state():
    if os.path.exists(STATE_FILE):
        return json.load(open(STATE_FILE, encoding="utf-8"))
    return {"date": "", "used": 0, "bvids": []}


def save_state(st):
    json.dump(st, open(STATE_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def _today():
    from datetime import date
    return date.today().isoformat()


def get_playurl_audio(bvid, cid, aid, cookies):
    """主通道：/x/player/playurl?fnval=4048 提取 dash.audio baseUrl（带 cookie，实测可用）。

    实测（2026-08-11）：/x/player/wbi/v2 响应不含 dash 字段（仅字幕/播放器状态）；
    音频流须走 playurl 接口（fnval=4048 返回 DASH）。每视频 1 API 请求。
    """
    from fetch_subtitles import _session_get
    params = {"bvid": bvid, "cid": cid, "fnval": 4048, "fnver": 0, "fourk": 1}
    r = _session_get("https://api.bilibili.com/x/player/playurl", params=params, cookies=cookies)
    if r.status_code in (412, 429):
        raise RuntimeError("HTTP 412/429")
    d = r.json()
    if d.get("code") != 0:
        return None
    dash = (d.get("data") or {}).get("dash") or {}
    audios = dash.get("audio") or []
    if not audios:
        return None
    audios.sort(key=lambda a: -(a.get("bandwidth") or 0))
    for a in audios:
        if a.get("baseUrl"):
            return a["baseUrl"]
    return None


def download_audio(bvid, url, dest, cookies, retries=2):
    """流式下载音频（带 Referer 防盗链），CDN 慢速时重试。"""
    import requests
    from fetch_subtitles import HEADERS
    h = dict(HEADERS)
    h["Referer"] = "https://www.bilibili.com/"
    last = None
    for attempt in range(retries + 1):
        try:
            with requests.get(url, headers=h, cookies=cookies, timeout=(15, 120), stream=True) as r:
                if r.status_code != 200:
                    raise RuntimeError(f"下载 HTTP {r.status_code}")
                n = 0
                with open(dest, "wb") as f:
                    for chunk in r.iter_content(65536):
                        f.write(chunk)
                        n += len(chunk)
            return n
        except requests.RequestException as e:
            last = e
            if attempt < retries:
                print(f"  [ASR] 音频下载重试 {attempt+1}/{retries}（{str(e)[:60]}）", file=__import__("sys").stderr)
                time.sleep(3)
    raise RuntimeError(f"音频下载失败: {last}")


def download_audio_ytdlp(bvid, dest_dir, cookies_file, budget_spend=None):
    """备通道：yt-dlp --cookies（V2 fallback，retries2/sleep1.5/禁-F）。"""
    import subprocess
    out_tmpl = os.path.join(dest_dir, f"{bvid}.%(ext)s")
    cmd = ["yt-dlp", "--cookies", cookies_file,
           "-f", "bestaudio/best", "-o", out_tmpl,
           "--no-playlist", "--no-warnings", "--retries", "2", "--fragment-retries", "2",
           "--socket-timeout", "15", "--sleep-requests", "1.5",
           f"https://www.bilibili.com/video/{bvid}"]
    if budget_spend:
        budget_spend(FALLBACK_BUDGET)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        raise RuntimeError(f"yt-dlp 失败: {proc.stderr[-200:]}")
    for fn in os.listdir(dest_dir):
        if fn.startswith(bvid):
            return os.path.join(dest_dir, fn)
    raise RuntimeError("yt-dlp 未产出音频文件")


_WHISPER_CACHE_NAME = "models--Systran--faster-whisper-small"


def _find_local_whisper():
    """定位本地已缓存的 faster-whisper small 快照目录；找不到返回 None。

    与 `transcribe_video._find_local_model` 同口径（先 MODELSCOPE_CACHE，再默认缓存）。

    为什么必须走本地路径（2026-10-07 实测）：`WhisperModel("small")` 会经
    huggingface_hub 联网解析模型；本机 huggingface.co 不可达、`HF_ENDPOINT`
    指向的镜像也常断连，实测抛
    `RemoteProtocolError: Server disconnected without sending a response.`
    —— 同一用例一次通过、一次失败，属**间歇性**故障，会让 CPU 兜底时好时坏。
    模型本已缓存，用本地目录即可完全离线，消除该不确定性。
    """
    import glob as _glob
    env_root = os.environ.get("MODELSCOPE_CACHE")
    roots = []
    if env_root:
        roots.append(os.path.join(env_root, "hub"))
    roots += [os.path.expanduser("~/.cache/modelscope/hub"),
              os.path.expanduser("~/.cache/huggingface/hub")]
    for root in roots:
        p = os.path.join(root, _WHISPER_CACHE_NAME)
        if not os.path.isdir(p):
            continue
        for s in _glob.glob(os.path.join(p, "snapshots", "*")) + [p]:
            if os.path.exists(os.path.join(s, "model.bin")):
                return s
    return None


def _t3_gpu_segments(path):
    """经 T3 ROCm GPU worker 转写 → [{from,to,content}]（秒）。

    不可用 / 未启用 / 失败一律返回 None，由调用方回落 CPU faster-whisper。
    受 WEBINTEL_T3_DEVICE_MODE 控制（cpu 时直接跳过 GPU）。
    """
    mode = (os.environ.get("WEBINTEL_T3_DEVICE_MODE") or "auto").strip().lower()
    if mode == "cpu":
        return None
    try:
        import sys as _sys
        _d = os.path.dirname(os.path.abspath(__file__))
        if _d not in _sys.path:
            _sys.path.insert(0, _d)
        import t3_rocm_client as cli
    except Exception:
        return None
    if not cli.is_available():
        return None
    try:
        msg = cli.get_worker().transcribe_full(path, with_timestamps=True)
    except Exception:
        return None
    if not msg:
        return None
    out = []
    for s in (msg.get("segments") or []):
        content = (s.get("content") or "").strip()
        if not content:
            continue
        out.append({"from": float(s.get("from") or 0.0),
                    "to": float(s.get("to") or 0.0),
                    "content": content})
    return out or None


def transcribe_audio(path, model=None, prefer_gpu=True):
    """转写 → [{from,to,content}]，from/to 单位秒。

    优先走 T3 ROCm GPU（SenseVoice，段级时间戳；2026-10-07 接入），
    不可用时回落 faster-whisper small/int8 CPU。
    model 显式传入时跳过 GPU（测试打桩用）。
    """
    if model is None and prefer_gpu:
        segs = _t3_gpu_segments(path)
        if segs:
            return segs
    if model is None:
        from faster_whisper import WhisperModel
        # 优先本地缓存目录（离线、确定性）；未缓存才退回模型名（会联网）
        local = _find_local_whisper()
        model = WhisperModel(local or "small", device="cpu", compute_type="int8",
                             cpu_threads=int(_CPU_THREADS))
    segments, info = model.transcribe(path, language="zh", vad_filter=True,
                                      beam_size=5, condition_on_previous_text=False)
    return [{"from": s.start, "to": s.end, "content": s.text.strip()}
            for s in segments if s.text.strip()]


def transcribe_videos(videos, cookies, tmp_dir, cookies_file=None, budget_spend=None,
                      need_vip_check=None):
    """批量 ASR 兜底。

    videos: [{bvid, cid, aid, title, tname}]（已过白名单：当日榜单∩无字幕轨）
    cookies: cookie dict；cookies_file: Netscape 路径（fallback 用）
    返回: {bvid: [{from,to,content}]}（仅成功转写的）
    """
    from datetime import date
    st = load_state()
    today = date.today().isoformat()
    if st.get("date") != today:
        st = {"date": today, "used": 0, "bvids": []}
    os.makedirs(tmp_dir, exist_ok=True)

    results = {}
    for v in videos:
        bvid = v["bvid"]
        if st["used"] >= DAILY_LIMIT or bvid in st["bvids"]:
            continue
        # 占位记账（尝试即计，V3；失败回补）
        st["used"] += 1
        st["bvids"].append(bvid)
        save_state(st)
        audio_path = None
        try:
            # 白名单硬校验（V3）：need_vip 检测（会员内容硬失败）
            if need_vip_check and need_vip_check(bvid, v.get("cid"), v.get("aid"), cookies):
                raise RuntimeError("会员内容（白名单硬失败）")
            # 主通道：DASH（0 额外预算）
            url = None
            try:
                url = get_playurl_audio(bvid, v.get("cid"), v.get("aid"), cookies)
            except RuntimeError as e:
                if "412" in str(e):
                    raise  # 风控：全局语义由上层处理
                url = None
            if url:
                audio_path = os.path.join(tmp_dir, f"{bvid}.m4a")
                n = download_audio(bvid, url, audio_path, cookies)
                if n <= 0:
                    raise RuntimeError("空音频")
            elif cookies_file:
                audio_path = download_audio_ytdlp(bvid, tmp_dir, cookies_file, budget_spend)
            else:
                raise RuntimeError("主通道无 DASH 且无 fallback 配置")
            segs = transcribe_audio(audio_path)
            if not segs:
                raise RuntimeError("转写为空")
            results[bvid] = segs
            print(f"  [ASR] {v['title'][:24]} ✓ {len(segs)} 段（{st['used']}/{DAILY_LIMIT}）")
        except Exception as e:
            # 失败回补名额（V3）
            st["used"] = max(0, st["used"] - 1)
            st["bvids"] = [b for b in st["bvids"] if b != bvid]
            if "412" in str(e):
                print(f"  [ASR] {bvid} 风控停止：{e}", flush=True)
                save_state(st)
                raise SystemExit(3)
            print(f"  [ASR] {v['title'][:24]} 失败：{str(e)[:80]}")
        finally:
            # 音频即删（V3）
            if audio_path and os.path.exists(audio_path):
                os.remove(audio_path)
            save_state(st)
    return results
