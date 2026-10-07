#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
t3_rocm_worker: T3 SenseVoice 的 ROCm GPU 常驻转录 worker
==========================================================
本脚本**必须在 ROCm 虚拟环境里运行**（默认 D:\\rocm101-venv\\Scripts\\python.exe），
由 transcribe_video.py 的 t3_sensevoice_rocm() 以子进程方式拉起，通过 stdin/stdout
上的行分隔 JSON 协议通信。主环境（torch 2.12.1+cpu）不需要任何改动。

为什么必须常驻而不是「每个视频起一次进程」（2026-10-07 实测，300s 音频）：
  - 进程就绪固定开销 ≈15s（import torch+funasr 10.6s + 模型加载 4.7s）
  - 推理本身仅 5.9s（GPU）——单次起进程会把 5.9s 变成 21s，反而比 CPU 还慢
  - 常驻后第 2 个视频起只有推理耗时，GPU 相对 CPU（27~34s）净赚 4.6~5.7x

协议（行分隔 JSON，UTF-8）：
  父 → 子  {"audio": "<音频绝对路径>", "with_timestamps": false}
                                           请求转录（with_timestamps=true 时多返回 segments）
  父 → 子  {"cmd": "ping"}                    健康检查
  父 → 子  {"audio": "...", "engine": "whisper_turbo"}
                                            指定引擎（缺省 sensevoice；P2 = T5 兜底引擎）
  父 → 子  {"cmd": "shutdown"}                优雅退出
  子 → 父  {"ready": true, ..., "engines": ["sensevoice", "whisper_turbo"]}
                                            模型就绪（父进程据此判断可用）
  子 → 父  {"ok": true, "text": "...", "chars": N, "elapsed": S, "meta": {...}}
             meta（P2b）：{"emotions": {"HAPPY":2,...}, "events": {"BGM":3,...}, "has_music": bool, "emotion_segments": [{from,to,emotion,events}]}
            with_timestamps 时额外：{"segments": [{"from": 0.31, "to": 1.67, "content": "..."}], "seg_count": N}
  子 → 父  {"ok": false, "error": "..."}
  子 → 父  {"pong": true} / {"bye": true}

stdout 污染防护（关键）：funasr / modelscope / torch 都会往 fd 1 打日志与进度条，
若与协议混流会立刻破坏 JSON 解析。做法是先把 fd 1 复制一份作为协议通道，
再把 fd 1 重定向到 fd 2（stderr），最后把 sys.stdout 也指向 stderr。
父进程把子进程 stderr 落到日志文件，失败时回读末尾若干行用于诊断。

环境变量：
  MODELSCOPE_CACHE               模型缓存根（默认 C:/ms_cache，纯 ASCII 路径）
  MIOPEN_CUSTOM_CACHE_DIR        MIOpen 内核编译缓存（默认 %TEMP%/miopen_cache_t3）
  WEBINTEL_T3_DEVICE             cuda | cpu（默认 cuda）
  WEBINTEL_CPU_THREADS           线程上限（默认 4）
  WEBINTEL_T5_MODEL              T5 模型 id（默认 openai/whisper-large-v3-turbo）
  TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL  默认 1（flash-attention 实验开关）

引擎（P2）：
  sensevoice     主引擎，启动即预热（ready 即代表 T3 可用），产 meta
  whisper_turbo  T5 兜底引擎（whisper-large-v3-turbo），**首次被请求时才加载**；
                 只作 T3 返回 None 后的质量兜底，不产 meta，速度约为主引擎的 1/2.6

直接自测：
  D:\\rocm101-venv\\Scripts\\python.exe t3_rocm_worker.py --selftest <audio>
  D:\\rocm101-venv\\Scripts\\python.exe t3_rocm_worker.py --selftest <audio> --engine whisper_turbo
"""
import argparse
import json
import os
import re
import sys
import tempfile
import time

# ---------------------------------------------------------------- 环境（必须在 torch 之前）
_TMP = tempfile.gettempdir()
os.environ.setdefault("MODELSCOPE_CACHE",
                      os.environ.get("MODELSCOPE_CACHE") or "C:/ms_cache")
os.environ.setdefault("MIOPEN_CUSTOM_CACHE_DIR",
                      os.path.join(_TMP, "miopen_cache_t3"))
os.environ.setdefault("TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL", "1")
# T5 兜底引擎（transformers pipeline）必须走本地缓存：本机 huggingface.co 不可达，
# 且 pipeline(..., local_files_only=True) 会把该参数泄漏进 generate_kwargs 直接报错
# （2026-10-07 实测），所以只能靠这两个环境变量做离线。
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
os.makedirs(os.environ["MIOPEN_CUSTOM_CACHE_DIR"], exist_ok=True)

_DEVICE = os.environ.get("WEBINTEL_T3_DEVICE", "cuda")
_CPU_THREADS = os.environ.get("WEBINTEL_CPU_THREADS", "4")
for _v in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS",
           "NUMEXPR_NUM_THREADS"):
    os.environ.setdefault(_v, _CPU_THREADS)

MODEL_ID = os.environ.get("WEBINTEL_T3_MODEL", "iic/SenseVoiceSmall")
VAD_MODEL = os.environ.get("WEBINTEL_T3_VAD_MODEL", "fsmn-vad")
VAD_MAX_SEG_MS = int(os.environ.get("WEBINTEL_T3_VAD_MAX_SEG_MS", "30000"))
BATCH_SIZE_S = int(os.environ.get("WEBINTEL_T3_BATCH_SIZE_S", "60"))

# ---- P2：引擎注册表（T5 = whisper-large-v3-turbo，质量兜底）
ENGINE_SENSEVOICE = "sensevoice"
ENGINE_WHISPER_TURBO = "whisper_turbo"
T5_MODEL_ID = os.environ.get("WEBINTEL_T5_MODEL", "openai/whisper-large-v3-turbo")

_MODELS = {}          # engine -> 已加载的模型句柄（懒加载）


def _split_protocol_channel():
    """把 fd 1 变成协议专用通道，其余 stdout 输出全部改道 stderr。"""
    proto = os.fdopen(os.dup(1), "w", encoding="utf-8", buffering=1, newline="\n")
    os.dup2(2, 1)          # 之后任何写 fd 1 的 C 库输出都进 stderr
    sys.stdout = sys.stderr  # Python 层 print 也进 stderr
    return proto


PROTO = _split_protocol_channel()


def emit(obj):
    PROTO.write(json.dumps(obj, ensure_ascii=False) + "\n")
    PROTO.flush()


def _load_sensevoice():
    """加载 SenseVoice + fsmn-vad。返回 (model, info)。"""
    import torch
    torch.set_num_threads(int(_CPU_THREADS))
    from funasr import AutoModel

    if _DEVICE != "cpu" and not torch.cuda.is_available():
        raise RuntimeError(f"请求 device={_DEVICE} 但 torch.cuda.is_available()=False")

    t0 = time.perf_counter()
    model = AutoModel(
        model=MODEL_ID,
        trust_remote_code=True,
        vad_model=VAD_MODEL,
        vad_kwargs={"max_single_segment_time": VAD_MAX_SEG_MS},
        device=_DEVICE,
        disable_update=True,
        disable_pbar=True,
        log_level="ERROR",
    )
    info = {
        "ready": True,
        "device": _DEVICE,
        "torch": torch.__version__,
        "hip": getattr(torch.version, "hip", None),
        "gpu": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
        "model": MODEL_ID,
        "load_sec": round(time.perf_counter() - t0, 2),
        "pid": os.getpid(),
    }
    return model, info


def load_model():
    """向后兼容的别名（P0/P1 的调用点与测试都按这个名字走）。"""
    return _load_sensevoice()


def load_engine(engine):
    """按引擎名懒加载模型（同引擎只加载一次）。返回模型句柄。

    - sensevoice：启动时已预热（`ready` 即代表 T3 可用）
    - whisper_turbo：**首次请求时才加载** —— 它只作为 T3 失败后的兜底，
      不该让每个 worker 启动都多背 1.8GB 显存与 2s 加载时间。
    """
    engine = engine or ENGINE_SENSEVOICE
    if engine in _MODELS:
        return _MODELS[engine]
    if engine == ENGINE_WHISPER_TURBO:
        import torch
        from transformers import pipeline
        t0 = time.perf_counter()
        pipe = pipeline("automatic-speech-recognition", model=T5_MODEL_ID,
                        device=_DEVICE if _DEVICE != "cpu" else -1,
                        torch_dtype=torch.float16 if _DEVICE != "cpu" else torch.float32)
        print(f"[worker] {ENGINE_WHISPER_TURBO} 已加载 "
              f"({time.perf_counter() - t0:.2f}s)", file=sys.stderr, flush=True)
        _MODELS[engine] = pipe
        return pipe
    if engine == ENGINE_SENSEVOICE:
        model, _info = _load_sensevoice()
        _MODELS[engine] = model
        return model
    raise ValueError(f"未知引擎：{engine}")


def _clean(text):
    """去掉 SenseVoice 的 <|zh|><|NEUTRAL|> 等富文本标签并映射情绪 emoji。"""
    from funasr.utils.postprocess_utils import rich_transcription_postprocess
    return rich_transcription_postprocess(text or "").strip()


# ---------------------------------------------------------------- P2b 情感/事件元数据
# SenseVoice 的原始输出形如 `<|zh|><|NEUTRAL|><|Speech|><|withitn|>文本…`，
# rich_transcription_postprocess 会把这些标签映射成 emoji（😡🎼👏…）**混进正文**。
# emoji 可读但不可统计，故在**后处理之前**解析原始标签，得到结构化计数。
# 标签词表来自 funasr/utils/postprocess_utils.py:339-356 / 368-390（一手核对）。
_TAG_RE = re.compile(r"<\|([A-Za-z_]+)\|>")
# 词表来自 funasr/utils/postprocess_utils.py:368-400 的 emoji_dict（逐条核对）
_EMOTION_TAGS = ("HAPPY", "SAD", "ANGRY", "NEUTRAL", "FEARFUL",
                 "DISGUSTED", "SURPRISED", "EMO_UNKNOWN")
_EVENT_TAGS = ("BGM", "Speech", "Applause", "Laughter", "Cry", "Sneeze",
               "Breath", "Cough", "Sing", "Speech_Noise", "GBG", "Event_UNK")
_ITN_MARK = "<|withitn|>"


def _parse_meta(raw):
    """从 SenseVoice 原始输出解析 (emotions, events) 两个计数字典。"""
    emotions, events = {}, {}
    for tag in _TAG_RE.findall(raw or ""):
        if tag in _EMOTION_TAGS:
            emotions[tag] = emotions.get(tag, 0) + 1
        elif tag in _EVENT_TAGS:
            events[tag] = events.get(tag, 0) + 1
    return emotions, events


def _merge_counts(dst, src):
    for k, v in src.items():
        dst[k] = dst.get(k, 0) + v


def transcribe(model, audio_path, with_timestamps=False):
    """转录一个音频。

    with_timestamps=True 时额外返回 segments=[{from,to,content}]，**单位秒**。
    来源：funasr 在 sentence_timestamp=True 下给出的 VAD sentence_info
    （其 start/end 为**毫秒**，此处换算成秒，与 faster-whisper 的段口径一致）。
    2026-10-07 探针实测：300s 音频返回 77 条 sentence_info，
    形如 {"start":310,"end":1670,"text":"...","sentence":"...","timestamp":[]}。

    始终返回 meta（P2b）：{"emotions": {...}, "events": {...}, "has_music": bool,
    "emotion_segments": [...]}。emotion_segments 需要 sentence_info，故只在
    with_timestamps=True 时可能有内容；中性且无事件的段不入列（避免长列表）。
    """
    t0 = time.perf_counter()
    kwargs = dict(input=audio_path, language="zh", use_itn=True,
                  batch_size_s=BATCH_SIZE_S)
    if with_timestamps:
        kwargs["sentence_timestamp"] = True
    result = model.generate(**kwargs)

    text = ""
    segments = []
    meta = {"emotions": {}, "events": {}, "has_music": False, "emotion_segments": []}
    for record in result or []:
        raw = record.get("text") or ""
        if "text" in record:
            text += _clean(raw) + "\n\n"
        _merge_counts(meta["emotions"], _parse_meta(raw)[0])
        _merge_counts(meta["events"], _parse_meta(raw)[1])
        if not with_timestamps:
            continue
        si_list = record.get("sentence_info") or []
        # 段级标签对齐：原始文本形如
        #   "<|zh|><|NEUTRAL|><|BGM|><|withitn|>句1 <|zh|><|ANGRY|><|BGM|><|withitn|>句2 ..."
        # 以 <|withitn|> 切分后，第 k 段（k>=1）里携带的标签即第 k 句的情感/事件。
        # 注意 sentence_info[i]["text"] 已被 funasr 清洗、**不含标签**（2026-10-07 实测），
        # 故段级标签只能从原始文本切分里取。
        tag_chunks = raw.split(_ITN_MARK)[1:]
        for i2, si in enumerate(si_list):
            content = _clean(si.get("sentence") or si.get("text") or "")
            if not content:
                continue
            seg = {
                "from": round(float(si.get("start") or 0) / 1000.0, 3),
                "to": round(float(si.get("end") or 0) / 1000.0, 3),
                "content": content,
            }
            segments.append(seg)
            s_em, s_ev = _parse_meta(tag_chunks[i2] if i2 < len(tag_chunks) else "")
            emotion = max(s_em, key=s_em.get) if s_em else None
            evs = sorted(s_ev)
            # 只保留有区分度的段：非中性情绪，或含 BGM 以外的事件。
            # （实测多数音频每句都带 <|BGM|>，若把 BGM 段全收进来，列表等于全文，
            #  没有信息量；音乐覆盖改用 music_ratio 表达。）
            if emotion in (None, "NEUTRAL") and not [e for e in evs if e != "BGM"]:
                continue
            meta["emotion_segments"].append({
                "from": seg["from"], "to": seg["to"],
                "emotion": emotion, "events": evs,
            })
    text = text.strip()
    meta["has_music"] = bool(meta["events"].get("BGM"))
    meta["music_ratio"] = round(meta["events"].get("BGM", 0) / max(1, len(segments)), 3) if segments else 0.0
    elapsed = time.perf_counter() - t0
    if not text and not segments:
        return {"ok": False, "error": "转写结果为空", "elapsed": round(elapsed, 2)}
    out = {"ok": True, "text": text, "chars": len(text),
           "elapsed": round(elapsed, 2), "meta": meta}
    if with_timestamps:
        if not segments and text:
            # 退化：sentence_info 缺失时给一个整段，避免调用方丢掉文本
            segments = [{"from": 0.0, "to": 0.0, "content": text}]
        out["segments"] = segments
        out["seg_count"] = len(segments)
    return out


def transcribe_whisper_turbo(pipe, audio_path, with_timestamps=False):
    """T5 兜底引擎：whisper-large-v3-turbo（transformers pipeline）。

    与 SenseVoice 的关键差异（2026-10-07 探针实测）：
    - **>30s 音频必须 return_timestamps=True**，否则 transformers 直接抛
      `ValueError: You have passed more than 3000 mel input features ...`。
      所以这里恒开时间戳，`with_timestamps` 参数只影响是否回传 segments。
    - 输出**无标点**，且尾部可能幻觉出超出音频时长的 chunk
      （300.7s 音频出现 (298.48, 328.46) 的「优优独播剧场…」）。
    - 速度约为 SenseVoice 的 1/2.6（12.36s vs 4.78s @300s 音频），故**只作兜底**。
    - 不产 meta（无情感/事件标签）。
    """
    t0 = time.perf_counter()
    out = pipe(audio_path, return_timestamps=True,
               generate_kwargs={"language": "chinese", "task": "transcribe"})
    chunks = out.get("chunks") or []
    text = (out.get("text") or "").strip()

    segments = []
    for c in chunks:
        content = (c.get("text") or "").strip()
        if not content:
            continue
        ts = c.get("timestamp") or (None, None)
        if not isinstance(ts, (tuple, list)):
            ts = (None, None)
        ts = tuple(ts) + (None, None)
        segments.append({
            "from": round(float(ts[0] or 0.0), 3),
            "to": round(float(ts[1] or 0.0), 3),
            "content": content,
        })

    elapsed = time.perf_counter() - t0
    if not text and not segments:
        return {"ok": False, "error": "转写结果为空", "elapsed": round(elapsed, 2),
                "engine": ENGINE_WHISPER_TURBO}
    out = {"ok": True, "text": text, "chars": len(text),
           "elapsed": round(elapsed, 2), "engine": ENGINE_WHISPER_TURBO}
    if with_timestamps:
        if not segments and text:
            segments = [{"from": 0.0, "to": 0.0, "content": text}]
        out["segments"] = segments
        out["seg_count"] = len(segments)
    return out


def serve():
    """主循环：逐行读 JSON 请求，逐行回 JSON 响应。

    请求可带 "engine"（sensevoice / whisper_turbo，缺省 sensevoice）。
    whisper_turbo 首次被请求时才加载（懒加载），避免每个 worker 白背显存。
    """
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as e:
            emit({"ok": False, "error": f"请求 JSON 解析失败：{e}"})
            continue

        cmd = req.get("cmd")
        if cmd == "shutdown":
            emit({"bye": True})
            return 0
        if cmd == "ping":
            emit({"pong": True})
            continue

        audio = req.get("audio")
        if not audio:
            emit({"ok": False, "error": "缺少 audio 字段"})
            continue
        if not os.path.exists(audio):
            emit({"ok": False, "error": f"音频不存在：{audio}"})
            continue
        try:
            engine = req.get("engine") or ENGINE_SENSEVOICE
            m = load_engine(engine)
            if engine == ENGINE_WHISPER_TURBO:
                emit(transcribe_whisper_turbo(m, audio, bool(req.get("with_timestamps"))))
            else:
                emit(transcribe(m, audio, bool(req.get("with_timestamps"))))
        except Exception as e:  # 单条失败不拖垮常驻进程
            emit({"ok": False, "error": f"{type(e).__name__}: {e}"[:400]})
    return 0


def main():
    ap = argparse.ArgumentParser(description="T3 SenseVoice ROCm 常驻转录 worker")
    ap.add_argument("--selftest", metavar="AUDIO",
                    help="不进入服务循环：加载模型并对该音频转录一次后退出")
    ap.add_argument("--timestamps", action="store_true",
                    help="配合 --selftest：额外输出段级时间戳 segments")
    ap.add_argument("--engine", default=ENGINE_SENSEVOICE,
                    choices=[ENGINE_SENSEVOICE, ENGINE_WHISPER_TURBO],
                    help="配合 --selftest：指定引擎（默认 sensevoice）")
    args = ap.parse_args()

    t_start = time.perf_counter()
    try:
        model, info = _load_sensevoice()
        _MODELS[ENGINE_SENSEVOICE] = model          # 启动即预热 T3 主引擎
    except Exception as e:
        emit({"ready": False, "error": f"{type(e).__name__}: {e}"[:400]})
        return 2

    info["engines"] = [ENGINE_SENSEVOICE, ENGINE_WHISPER_TURBO]
    info["t5_model"] = T5_MODEL_ID
    info["total_sec"] = round(time.perf_counter() - t_start, 2)
    emit(info)

    if args.selftest:
        try:
            m = load_engine(args.engine)
            if args.engine == ENGINE_WHISPER_TURBO:
                emit(transcribe_whisper_turbo(m, args.selftest, args.timestamps))
            else:
                emit(transcribe(m, args.selftest, args.timestamps))
        except Exception as e:
            emit({"ok": False, "error": f"{type(e).__name__}: {e}"[:400]})
            return 3
        return 0
    return serve()


if __name__ == "__main__":
    sys.exit(main())
