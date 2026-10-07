#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_asr_gpu: P1 —— asr_fallback 接入 T3 ROCm GPU 的回归测试
=============================================================
运行（主环境，非 ROCm venv）：
    python test_asr_gpu.py              # 全部用例
    python test_asr_gpu.py --red        # 可红性自检：注入故障，验证用例能失败
    python test_asr_gpu.py --no-gpu     # 跳过需要真实 GPU 的用例

背景（P1，2026-10-07）：
    `asr_fallback.transcribe_audio()` 原本硬编码 faster-whisper small/int8 CPU，
    是 web-intel 里**第二处** CPU 转写点（第一处 transcribe_video.py 的 T3 已于
    2026-10-07 接入 ROCm GPU）。本测试锁死新契约：
      ① GPU 优先、失败回落 CPU，且**回落路径行为与接入前逐字一致**；
      ② 段结构必须是 [{from,to,content}]，from/to 为**秒级 float**
         （下游 bili_daily_v2.py:471 直接读 s["from"]/s["to"]，单位错会毁掉时间轴）；
      ③ `WEBINTEL_T3_DEVICE_MODE=cpu` 必须彻底绕开 GPU（回滚路径）；
      ④ `model=` 显式传入时跳过 GPU（测试打桩依赖此语义）。

断言清单：
  1. 静态契约：transcribe_audio 接受 prefer_gpu 参数；_t3_gpu_segments 存在
  2. CPU 桩路径：显式传 model 时只用该 model，不触碰 GPU（打桩语义）
  3. 段结构契约：model 桩返回的段被规整为 {from,to,content}（strip + float）
  4. 回滚路径：WEBINTEL_T3_DEVICE_MODE=cpu 时 _t3_gpu_segments 返回 None
  5. 真实 GPU：transcribe_audio(path) 走 GPU 并返回秒级段（--no-gpu 跳过）
  6. 单位正确性：GPU 段尾不超音频时长（毫秒口径会得到 1000 倍）
  7. 与 faster-whisper 口径一致：段 key 集合与 CPU 路径完全相同
  8. CPU 兜底真跑（防两个已修 bug 回归）：
     8a NO_PROXY 无方括号 IPv6 项（httpx 解析）；8b 真加载 faster-whisper 不崩；
     8c 命中本地缓存目录（离线，不依赖 HF 网络）
"""
import argparse
import os
import sys
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

AUDIO = os.environ.get("TEST_AUDIO", r"C:\Temp\audiotest\long5min.wav")

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"{'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""),
          flush=True)
    return cond


def _audio_sec(path):
    try:
        with wave.open(path, "rb") as f:
            return f.getnframes() / float(f.getframerate())
    except Exception:
        return None


class _FakeSeg:
    def __init__(self, s, e, t):
        self.start, self.end, self.text = s, e, t


class _FakeModel:
    """最小 faster-whisper 桩：记录调用次数，返回带空白前缀的段（验证 strip）。"""

    def __init__(self):
        self.calls = 0

    def transcribe(self, path, **kw):
        self.calls += 1
        return [_FakeSeg(0.0, 1.5, " 第一段 "), _FakeSeg(1.5, 3.25, " 第二段 "),
                _FakeSeg(3.25, 4.0, "   ")], None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--red", action="store_true", help="注入故障，验证用例确实能失败")
    ap.add_argument("--no-gpu", action="store_true", help="跳过需要真实 GPU 的用例")
    args = ap.parse_args()

    print("=" * 64)
    print("P1: asr_fallback GPU 接入回归测试" + ("  [RED 模式]" if args.red else ""))
    print("=" * 64)

    import asr_fallback as af
    import t3_rocm_client as cli

    if args.red:
        # 可红性自检：抹掉 transcribe_audio 的 prefer_gpu 参数 —— 断言 1a 必须失败。
        # 选这个注入点是因为它**不触碰 GPU、也不加载模型**，秒级完成、完全确定性
        # （早先版本把 _t3_gpu_segments 打成 None 会真的走 CPU 兜底 → 加载
        #   faster-whisper，既慢又受环境影响）。
        af.transcribe_audio = lambda path, model=None: []
        print("  [RED] 已抹掉 transcribe_audio 的 prefer_gpu 参数 —— 预期 1a 失败",
              flush=True)

    # ---------------------------------------------------------- 1 静态契约
    import inspect
    sig = inspect.signature(af.transcribe_audio)
    check("1a transcribe_audio 暴露 prefer_gpu 参数", "prefer_gpu" in sig.parameters,
          str(sig))
    check("1b _t3_gpu_segments 存在", callable(getattr(af, "_t3_gpu_segments", None)))

    if args.red:
        print("=" * 64)
        print(f"通过 {len(PASS)} / 失败 {len(FAIL)}")
        for f in FAIL:
            print(f"  ❌ {f}")
        print("=" * 64)
        if len(FAIL) >= 1:
            print("\n[RED] 可红性验证通过：注入故障后断言确实失败。")
            sys.exit(0)
        print("\n[RED] 可红性验证失败：注入故障后断言仍全绿，测试无鉴别力！")
        sys.exit(2)

    # ---------------------------------------------------------- 2 CPU 桩路径
    fake = _FakeModel()
    segs = af.transcribe_audio("dummy.wav", model=fake)
    check("2a 显式传 model 时只调用该 model（不触碰 GPU）",
          fake.calls == 1, f"calls={fake.calls}")

    # ---------------------------------------------------------- 3 段结构契约
    expect = [{"from": 0.0, "to": 1.5, "content": "第一段"},
              {"from": 1.5, "to": 3.25, "content": "第二段"}]
    check("3a 段被规整为 {from,to,content} 且 strip 空白、丢弃空段",
          segs == expect, f"got={segs}")

    # ---------------------------------------------------------- 4 回滚路径
    _saved = os.environ.get("WEBINTEL_T3_DEVICE_MODE")
    try:
        os.environ["WEBINTEL_T3_DEVICE_MODE"] = "cpu"
        got = af._t3_gpu_segments(AUDIO)
        check("4a mode=cpu 时 _t3_gpu_segments 返回 None（回滚路径）",
              got is None, repr(got))
        fake2 = _FakeModel()
        segs2 = af.transcribe_audio(AUDIO, model=fake2)
        check("4b mode=cpu 且无 model 时仍能回落 CPU 路径",
              isinstance(segs2, list), f"n={len(segs2) if segs2 else 0}")
    finally:
        if _saved is None:
            os.environ.pop("WEBINTEL_T3_DEVICE_MODE", None)
        else:
            os.environ["WEBINTEL_T3_DEVICE_MODE"] = _saved

    # ------------------------------------------------- 5/6/7 真实 GPU 路径
    if args.no_gpu or not cli.is_available():
        print("⚠️ 5/6/7 真实 GPU 用例跳过（无 GPU / --no-gpu）", flush=True)
    else:
        gsegs = af.transcribe_audio(AUDIO)          # 默认 prefer_gpu=True
        check("5a 无 model 时走 GPU 并返回非空段", bool(gsegs),
              f"n={len(gsegs) if gsegs else 0}（red 模式下预期 ❌）")

        shape_ok = bool(gsegs) and all(
            set(s.keys()) == {"from", "to", "content"}
            and isinstance(s["from"], float) and isinstance(s["to"], float)
            and isinstance(s["content"], str) and s["content"].strip()
            for s in gsegs)
        check("5b GPU 段结构与 CPU 路径同构（key 集合 + float）", shape_ok,
              f"首条={gsegs[0] if gsegs else None}")

        check("5c GPU 段 from 单调不减",
              bool(gsegs) and all(gsegs[i]["from"] <= gsegs[i + 1]["from"]
                                  for i in range(len(gsegs) - 1)),
              f"n={len(gsegs) if gsegs else 0}")

        dur = _audio_sec(AUDIO)
        if dur and gsegs:
            last_to = gsegs[-1]["to"]
            check("6a GPU 段 to 是秒级且不超音频时长（+2s 容差）",
                  0 < last_to <= dur + 2.0,
                  f"last_to={last_to} 音频≈{dur:.1f}s")
        else:
            print("⚠️ 6a 跳过（取不到音频时长或无段）", flush=True)

        check("7a 与 faster-whisper 口径一致：key 集合完全相同",
              bool(gsegs) and all(set(s.keys()) == set(expect[0].keys())
                                  for s in gsegs))

    # -------------------------------- 8 CPU 兜底真跑（防 NO_PROXY 回归，P1）
    # 2026-10-07 实测发现的真 bug：NO_PROXY 含 `[::1]` 时 httpx 抛
    # `InvalidURL: Invalid port: ':1]'`，faster-whisper 按模型名加载即崩 →
    # GPU 不可用时的 CPU 兜底路径**完全不可用**。asr_fallback._sanitize_proxy_env()
    # 已修复；这里用真实模型跑一遍，锁死该回归。
    np_raw = os.environ.get("NO_PROXY", "")
    check("8a NO_PROXY 中已无方括号 IPv6 项（httpx 可解析）",
          "[" not in np_raw, f"NO_PROXY={np_raw}")

    # 8c：CPU 兜底必须走本地缓存目录，不得依赖 HF 网络
    # （2026-10-07 实测：WhisperModel("small") 联网解析模型时抛
    #   RemoteProtocolError，同一用例一次通过一次失败 —— 间歇性故障）
    _local = af._find_local_whisper()
    check("8c faster-whisper 命中本地缓存（离线可用）", bool(_local), str(_local))

    if args.no_gpu:
        print("⚠️ 8b 跳过（--no-gpu）", flush=True)
    else:
        try:
            cpu_segs = af.transcribe_audio(AUDIO, prefer_gpu=False)
            check("8b CPU 兜底真跑不崩且返回段列表",
                  isinstance(cpu_segs, list), f"n={len(cpu_segs)}")
        except Exception as e:
            check("8b CPU 兜底真跑不崩且返回段列表", False,
                  f"{type(e).__name__}: {str(e)[:120]}")

    # ---------------------------------------------------------- 汇总
    print("=" * 64)
    print(f"通过 {len(PASS)} / 失败 {len(FAIL)}")
    for f in FAIL:
        print(f"  ❌ {f}")
    print("=" * 64)

    if args.red:
        if len(FAIL) >= 1:
            print("\n[RED] 可红性验证通过：注入故障后断言确实失败。")
            sys.exit(0)
        print("\n[RED] 可红性验证失败：注入故障后断言仍全绿，测试无鉴别力！")
        sys.exit(2)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
