#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_t5_turbo: P2 —— whisper-large-v3-turbo 质量兜底引擎的回归测试
================================================================
运行（主环境，非 ROCm venv）：
    python test_t5_turbo.py              # 全部用例
    python test_t5_turbo.py --red        # 可红性自检：注入故障，验证用例能失败
    python test_t5_turbo.py --no-gpu     # 跳过需要真实 GPU 的用例

背景（P2，2026-10-07）：
    T1/T2 无字幕、T3 又失败时，原兜底是 T4 faster-whisper small（CPU）。
    P2 在两者之间插入 T5 = whisper-large-v3-turbo，**复用同一套 ROCm 常驻 worker**
    （懒加载，不额外起进程），质量显著更好。

    为什么只作兜底（探针实测，300.7s 音频）：
      - 速度：turbo 12.36s vs SenseVoice 4.78s（稳态）→ 慢 2.6x
      - 输出：无标点；尾部会幻觉出超出音频时长的内容
      - 显存：额外 1.79GB
    故定位「T3 失败后的质量兜底」，其下仍有 T4 CPU 兜底。

本测试锁死四条契约：
  ① 引擎分派必须走 worker 协议（`engine` 字段），而不是另起进程/另一套栈；
  ② `transcribe_full(engine=...)` 必须在 **单例与池两条路径** 都透传；
  ③ T5 响应**不产 meta**（turbo 无情感/事件标签），且未请求时不含 segments；
  ④ 默认路径（不传 engine）必须仍是 SenseVoice 且仍产 meta —— P1/P2b 行为不变。

断言清单：
  1. 静态契约（1a tv.t5_whisper_turbo 存在；1b 接受 worker 参数；
     1c 单例 transcribe_full 接受 engine；1d 池 transcribe_full 接受 engine；
     1e worker 模块有 ENGINE_WHISPER_TURBO/T5_MODEL_ID/_MODELS/load_engine；
     1f load_engine 对未知名抛 ValueError）
  2. 开关与降级（2a WEBINTEL_T5_ENABLE=0 → None；2b mode=cpu → None；
     2c --no-t5 等价于 WEBINTEL_T5_ENABLE=0）
  3. 真实 GPU（3a turbo ok；3b engine 字段；3c 文本非空；3d 无 meta；
     3e 无 segments；3f 默认路径仍是 sensevoice 且有 meta；
     3g turbo 与 sensevoice 文本不同）
  4. 池透传（4a 池 transcribe_full(engine=...) 的 engine 字段正确）
"""
import argparse
import inspect
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

AUDIO = os.environ.get("TEST_AUDIO", r"C:\Temp\t3audio\BV1cuZwYZEZr.m4a")

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"{'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""),
          flush=True)
    return cond


def _worker_src():
    return open(os.path.join(HERE, "t3_rocm_worker.py"), encoding="utf-8").read()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--red", action="store_true", help="可红性自检：注入故障")
    ap.add_argument("--no-gpu", action="store_true", help="跳过需要真实 GPU 的用例")
    args = ap.parse_args()

    import t3_rocm_client as cli
    import transcribe_video as tv

    red = args.red

    # ---------------------------------------------------------- [1] 静态契约
    print("\n[1] 静态契约", flush=True)
    check("1a tv.t5_whisper_turbo 存在", callable(getattr(tv, "t5_whisper_turbo", None)))
    sig = inspect.signature(tv.t5_whisper_turbo)
    check("1b 接受 worker 参数", "worker" in sig.parameters,
          f"params={list(sig.parameters)}")
    check("1c 单例 transcribe_full 接受 engine",
          "engine" in inspect.signature(cli.T3RocmWorker.transcribe_full).parameters)
    check("1d 池 transcribe_full 接受 engine",
          "engine" in inspect.signature(cli.WorkerPool.transcribe_full).parameters)

    src = _worker_src()
    have = all(k in src for k in ("ENGINE_WHISPER_TURBO", "T5_MODEL_ID", "_MODELS",
                                  "def load_engine"))
    check("1e worker 模块有引擎注册表与 load_engine", have)
    check("1f load_engine 对未知名抛 ValueError", "raise ValueError" in src)

    # ---------------------------------------------------------- [2] 开关与降级
    print("\n[2] 开关与降级", flush=True)
    if red:
        # 注入故障：让开关失效（直接返回假文本），2a/2b 必须变红
        tv.t5_whisper_turbo = lambda p, worker=None: "FAKE"
        print("  [red] 已注入：t5_whisper_turbo 忽略开关直接返回假文本", flush=True)

    old_mode = os.environ.get("WEBINTEL_T3_DEVICE_MODE")
    old_en = os.environ.get("WEBINTEL_T5_ENABLE")
    try:
        os.environ["WEBINTEL_T5_ENABLE"] = "0"
        check("2a WEBINTEL_T5_ENABLE=0 → None", tv.t5_whisper_turbo(AUDIO) is None)

        os.environ["WEBINTEL_T5_ENABLE"] = "1"
        os.environ["WEBINTEL_T3_DEVICE_MODE"] = "cpu"
        check("2b mode=cpu → None", tv.t5_whisper_turbo(AUDIO) is None)
    finally:
        if old_mode is None:
            os.environ.pop("WEBINTEL_T3_DEVICE_MODE", None)
        else:
            os.environ["WEBINTEL_T3_DEVICE_MODE"] = old_mode
        if old_en is None:
            os.environ.pop("WEBINTEL_T5_ENABLE", None)
        else:
            os.environ["WEBINTEL_T5_ENABLE"] = old_en

    src_tv = open(os.path.join(HERE, "transcribe_video.py"), encoding="utf-8").read()
    check("2c --no-t5 等价于 WEBINTEL_T5_ENABLE=0",
          'os.environ["WEBINTEL_T5_ENABLE"] = "0"' in src_tv)

    if args.no_gpu:
        return _report()

    # ---------------------------------------------------------- [3] 真实 GPU
    print("\n[3] 真实 GPU（单例）", flush=True)
    w = cli.get_worker()
    if not w.available():
        print("  ⚠️ ROCm 不可用，跳过 [3]/[4]", flush=True)
        return _report()

    turbo = w.transcribe_full(AUDIO, engine="whisper_turbo")
    if red:
        # 注入故障：吞掉 engine，worker 会用默认 sensevoice → 3b 必须变红
        print("  [red] 已注入：抹掉 turbo 响应里的 engine 字段", flush=True)
        turbo = dict(turbo or {})
        turbo.pop("engine", None)

    check("3a turbo ok=True", bool((turbo or {}).get("ok")))
    check("3b engine 字段 == whisper_turbo",
          (turbo or {}).get("engine") == "whisper_turbo",
          f"got={ (turbo or {}).get('engine')!r}")
    check("3c turbo 文本非空", bool((turbo or {}).get("text")))

    sense = w.transcribe_full(AUDIO)
    check("3d turbo 不产 meta", "meta" not in (turbo or {}))
    check("3e turbo 未请求时不含 segments", "segments" not in (turbo or {}))
    check("3f 默认路径仍是 sensevoice 且有 meta",
          (sense or {}).get("engine") is None and bool((sense or {}).get("meta")),
          f"engine={(sense or {}).get('engine')!r} has_meta={'meta' in (sense or {})}")
    check("3g turbo 与 sensevoice 文本不同",
          bool((turbo or {}).get("text")) and (turbo or {}).get("text") != (sense or {}).get("text"),
          f"turbo={(turbo or {}).get('chars')}字 sense={(sense or {}).get('chars')}字")
    cli.close_worker()

    # ---------------------------------------------------------- [4] 池透传
    print("\n[4] 池透传", flush=True)
    pool = cli.get_pool(size=1)
    pm = pool.transcribe_full(AUDIO, engine="whisper_turbo")
    check("4a 池 engine 字段正确", (pm or {}).get("engine") == "whisper_turbo",
          f"got={(pm or {}).get('engine')!r}")
    cli.close_pool()

    return _report()


def _report():
    print(f"\n{'=' * 56}", flush=True)
    print(f"通过 {len(PASS)} / 失败 {len(FAIL)}", flush=True)
    if FAIL:
        print("失败项：" + ", ".join(FAIL), flush=True)
        return 1
    print("✅ 全绿", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
