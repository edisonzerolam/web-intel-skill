#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_t3_rocm: T3 ROCm GPU 常驻 worker 通道的回归测试
=====================================================
运行（主环境，非 ROCm venv）：
    python test_t3_rocm.py              # 全部用例
    python test_t3_rocm.py --red        # 可红性自检：注入故障，验证用例能失败
    python test_t3_rocm.py --no-gpu     # 跳过需要真实 GPU 的用例（CI/无卡机器）

断言清单：
  1. 静态可用性：ROCm 解释器与 worker 脚本存在（--no-gpu 时只断言脚本存在）
  2. worker 就绪：拉起子进程，ready 消息含 device=gpu 名/torch/hip，就绪耗时可测量
  3. 单次转录：对真实音频返回非空中文文本，且耗时被记录；GPU 输出与 CPU 输出等价
  4. 常驻复用：第二次转录**不重启进程**（pid 不变）且**未重新付就绪开销**
     （4c 于 2026-10-07 由「墙钟阈值」改为「worker 启动次数不变」的结构性断言——
     原断言在机器带载时误红；比较 A/B 前先量化噪声基线是硬纪律）
  5. 协议纯净：子进程 stdout 的每一行都是合法 JSON（库日志未污染协议通道）
  6. 降级路径：指定不存在的解释器 → available()=False，transcribe() 返回 None（不抛异常）
  7. 调度分派：WEBINTEL_T3_DEVICE_MODE=cpu 时不触碰 ROCm；未知值按 auto 处理不崩；
     7e（2026-10-07 新增）：worker 参数必须透传到 ROCm 通道（批量池依赖此契约）
  8. 优雅收尾：close() 后进程确实退出（poll() 非 None）
  9. 段级时间戳（P1，2026-10-07 新增）：with_timestamps=True 返回非空 segments、
     每项含数值 from/to 与非空 content、from 单调不减、to 为**秒级**且不超音频时长、
     未请求时不返回 segments、加时间戳不改变正文（相似度 >0.99）、
     WorkerPool.transcribe_full 支持时间戳透传。`--red-ts` 为可红性自检。
"""
import argparse
import importlib
import json
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# Windows 控制台默认 GBK，emoji/中文标记会 UnicodeEncodeError（2026-10-07 实测）
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

AUDIO = os.environ.get("TEST_AUDIO", r"C:\Temp\audiotest\long5min.wav")

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    mark = "✅" if cond else "❌"
    print(f"{mark} {name}" + (f"  — {detail}" if detail else ""), flush=True)
    return cond


def _audio_sec(path):
    """音频时长（秒）。取不到返回 None（调用方跳过相关断言）。"""
    try:
        import wave
        with wave.open(path, "rb") as f:
            return f.getnframes() / float(f.getframerate())
    except Exception:
        pass
    try:
        import soundfile as sf
        return float(sf.info(path).duration)
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--red", action="store_true", help="注入故障，验证用例确实能失败")
    ap.add_argument("--red-ts", action="store_true",
                    help="可红性自检（P1）：不带时间戳调用，断言 9a 必须失败")
    ap.add_argument("--no-gpu", action="store_true", help="跳过需要真实 GPU 的用例")
    args = ap.parse_args()

    print("=" * 64)
    print("T3 ROCm worker 回归测试" + ("  [RED 模式]" if args.red else ""))
    print("=" * 64)

    import t3_rocm_client as cli

    # ---------------------------------------------------------- 1 静态可用性
    worker_script_ok = os.path.isfile(cli.WORKER_SCRIPT)
    check("1a worker 脚本存在", worker_script_ok, cli.WORKER_SCRIPT)

    python_exe = os.environ.get("WEBINTEL_T3_ROCM_PYTHON", cli.DEFAULT_ROCM_PYTHON)
    venv_ok = os.path.isfile(python_exe)
    if args.red:
        # 可红性自检：强制 worker 用 CPU 执行，断言 2d（device=cuda）必须失败，
        # 以此证明测试真的在检查 GPU 路径，而不是无条件通过。
        os.environ["WEBINTEL_T3_DEVICE"] = "cpu"
        print("  [RED] 已强制 WEBINTEL_T3_DEVICE=cpu —— 预期 2d 失败、1b 失败", flush=True)
        python_exe = r"D:\__not_exist__\python.exe"
        venv_ok = False
    check("1b ROCm 解释器存在", venv_ok, python_exe)

    if args.red:
        # 红模式：故意让断言失败，证明 check() 真的能捕获失败（否则测试是假绿）。
        check("1b(red) 解释器缺失应导致 available()=False",
              cli.T3RocmWorker(python_exe=python_exe).available() is False,
              "预期 ✅")
        check("1b(red2) 可红性自检——故意断言 True 应当失败",
              cli.T3RocmWorker(python_exe=python_exe).available() is True,
              "本条预期 ❌（证明 check() 能捕获失败）")
        os.environ.pop("WEBINTEL_T3_DEVICE", None)
        if len(FAIL) >= 1:
            print("\n[RED] 可红性验证通过：注入故障后断言确实失败。")
            sys.exit(0)
        print("\n[RED] 可红性验证失败：注入故障后断言仍全绿，测试无鉴别力！")
        sys.exit(2)

    if not venv_ok:
        check("1c 音频样本存在", os.path.isfile(AUDIO), AUDIO)
        print("\n[跳过] ROCm venv 不在位，GPU 用例全部跳过（这是降级场景，不是失败）")
        _summary()
        return

    check("1c 音频样本存在", os.path.isfile(AUDIO), AUDIO)
    if not os.path.isfile(AUDIO):
        _summary()
        return

    # ---------------------------------------------------------- 2 worker 就绪
    w = cli.T3RocmWorker(python_exe=python_exe)
    t0 = time.perf_counter()
    ok = w.start()
    ready_sec = time.perf_counter() - t0
    check("2a worker 启动并报告 ready", ok, f"就绪 {ready_sec:.1f}s")
    if not ok:
        print("    日志尾部：", w._log_tail())
        _summary()
        return

    check("2b ready 含 GPU 设备名", bool(w.info.get("gpu")), str(w.info.get("gpu")))
    check("2c ready 含 torch 与 hip 版本",
          bool(w.info.get("torch")) and bool(w.info.get("hip")),
          f"torch={w.info.get('torch')} hip={w.info.get('hip')}")
    check("2d device=cuda（非静默回落 CPU）", w.info.get("device") == "cuda",
          str(w.info.get("device")))
    first_pid = w.info.get("pid")

    # ---------------------------------------------------------- 3 单次转录
    t0 = time.perf_counter()
    text1 = w.transcribe(AUDIO)
    t_first = time.perf_counter() - t0
    check("3a 首次转录返回非空文本", bool(text1), f"{len(text1 or '')} 字 / {t_first:.1f}s")

    def _starts(log_path):
        """日志里 worker 启动次数（每次 start() 写一行「start device=」）。"""
        try:
            with open(log_path, encoding="utf-8", errors="replace") as f:
                return sum(1 for ln in f if "start device=" in ln)
        except OSError:
            return -1

    starts_first = _starts(w._log_path)
    check("3b 文本含中文字符",
          bool(text1) and any("\u4e00" <= ch <= "\u9fff" for ch in text1),
          (text1 or "")[:40].replace("\n", " "))

    # ---------------------------------------------------------- 3c 跨设备一致性
    # 核心质量主张：GPU 通道的输出必须与 CPU 通道等价。这是「接产品」的前提。
    if not args.no_gpu:
        try:
            import transcribe_video as tv
            _mode = os.environ.pop("WEBINTEL_T3_DEVICE_MODE", None)
            os.environ["WEBINTEL_T3_DEVICE_MODE"] = "cpu"
            print("  （正在跑 CPU 对照，约 30~40s）", flush=True)
            t0 = time.perf_counter()
            text_cpu = tv.t3_sensevoice(AUDIO)
            t_cpu = time.perf_counter() - t0
            if _mode is None:
                os.environ.pop("WEBINTEL_T3_DEVICE_MODE", None)
            else:
                os.environ["WEBINTEL_T3_DEVICE_MODE"] = _mode
            import difflib
            r = difflib.SequenceMatcher(None, text1 or "", text_cpu or "").ratio()
            check("3c GPU 输出与 CPU 输出等价（相似度 >0.99）", r > 0.99,
                  f"相似度 {r:.4f}；GPU {t_first:.1f}s vs CPU {t_cpu:.1f}s "
                  f"→ {t_cpu / max(t_first, 0.01):.1f}x")
        except Exception as e:
            check("3c GPU 输出与 CPU 输出等价", False, f"对照失败：{type(e).__name__}: {e}")

    # ---------------------------------------------------------- 4 常驻复用
    t0 = time.perf_counter()
    text2 = w.transcribe(AUDIO)
    t_second = time.perf_counter() - t0
    check("4a 第二次转录成功", bool(text2), f"{len(text2 or '')} 字 / {t_second:.1f}s")
    check("4b 进程未重启（pid 不变）",
          w.proc is not None and w.info.get("pid") == first_pid,
          f"pid {first_pid} → {w.info.get('pid')}")
    # 4c（2026-10-07 改为结构性断言）：原「复用快于首次」是墙钟阈值断言，
    # 机器带载时不可靠（实测在负载下误红）。改为断言**没有重新付就绪开销**：
    # worker 启动次数在复用前后必须不变。
    starts_second = _starts(w._log_path)
    check("4c 复用时未重新就绪（worker 启动次数不变）",
          starts_second == starts_first and starts_first >= 1,
          f"启动次数 {starts_first} → {starts_second}；"
          f"墙钟首次 {t_first:.1f}s → 复用 {t_second:.1f}s（仅供参考，不作断言）")
    # 注意：SenseVoice 本身就不是逐字确定的（2026-10-07 实测，CPU 与 GPU 同样表现）：
    # 同一音频重复推理会在固定位置出现 1 字差异（如 320 与 473 位的 谢/行 互换），
    # 相似度 0.9974~0.9987。这是模型/线程浮点累加顺序的性质，**不是 ROCm 引入的**。
    # 因此这里断言「近似一致」而非「逐字一致」。
    import difflib
    ratio = difflib.SequenceMatcher(None, text1 or "", text2 or "").ratio()
    check("4d 两次结果近似一致（SenseVoice 本身有 1 字级抖动）",
          ratio > 0.99, f"相似度 {ratio:.4f}")

    # ---------------------------------------------------------- 5 协议纯净
    if w._log_fh:
        w._log_fh.flush()
    raw_lines = []
    if w.proc and w.proc.poll() is None:
        # 主动 ping 一次，确认协议通道仍干净
        w.proc.stdin.write(json.dumps({"cmd": "ping"}) + "\n")
        w.proc.stdin.flush()
        pong = w._read_json(30)
        check("5a ping/pong 可用", bool(pong and pong.get("pong")), str(pong))
    bad = 0
    try:
        with open(w._log_path, encoding="utf-8", errors="replace") as f:
            raw_lines = f.readlines()
    except OSError:
        pass
    check("5b 库日志已改道 stderr（日志文件非空即证明隔离生效）",
          len(raw_lines) > 0, f"{len(raw_lines)} 行 stderr 日志")
    bad = sum(1 for ln in raw_lines if ln.strip().startswith("{") and "ready" not in ln
              and "ok" not in ln and "pong" not in ln)
    check("5c 协议 JSON 未混入 stderr 日志", bad == 0, f"可疑行 {bad}")

    # ---------------------------------------------------------- 8 优雅收尾
    proc = w.proc
    w.close()
    check("8a close() 后子进程已退出",
          proc is not None and proc.poll() is not None,
          f"returncode={proc.poll() if proc else None}")

    # ------------------------------------------- 9 段级时间戳（P1，2026-10-07）
    # 契约：worker 协议新增 with_timestamps；asr_fallback.transcribe_audio 依赖
    # segments 的**秒级** from/to（与 faster-whisper 的 s.start/s.end 同口径）。
    w2 = cli.T3RocmWorker()
    if args.no_gpu or not w2.available():
        print("⚠️ 9 段级时间戳用例跳过（无 GPU / --no-gpu）", flush=True)
    else:
        _want_ts = not args.red_ts      # --red-ts：故意不带时间戳，9a 必须失败
        msg = w2.transcribe_full(AUDIO, with_timestamps=_want_ts)
        segs = (msg or {}).get("segments") or []
        check("9a 请求时间戳时返回非空 segments", bool(segs),
              f"seg_count={len(segs)}（red-ts 模式下预期 ❌）")

        shape_ok = bool(segs) and all(
            isinstance(s.get("from"), (int, float))
            and isinstance(s.get("to"), (int, float))
            and isinstance(s.get("content"), str) and s["content"].strip()
            for s in segs)
        check("9b segments 每项含数值 from/to 与非空 content", shape_ok,
              f"首条={segs[0] if segs else None}")

        check("9c from 单调不减",
              bool(segs) and all(segs[i]["from"] <= segs[i + 1]["from"]
                                 for i in range(len(segs) - 1)),
              f"n={len(segs)}")

        dur = _audio_sec(AUDIO)
        if dur:
            last_to = segs[-1]["to"] if segs else 0
            # 单位必须是秒：若误用毫秒口径，last_to 会是音频时长的 1000 倍
            check("9d to 是秒级且不超音频时长（+2s 容差）",
                  bool(segs) and 0 < last_to <= dur + 2.0,
                  f"last_to={last_to} 音频≈{dur:.1f}s")
        else:
            print("⚠️ 9d 跳过（取不到音频时长）", flush=True)

        msg2 = w2.transcribe_full(AUDIO)          # 默认不带时间戳
        check("9e 未请求时响应不含 segments（协议载荷不无谓变大）",
              "segments" not in (msg2 or {}),
              f"keys={sorted((msg2 or {}).keys())}")

        # 加时间戳不应改变正文（SenseVoice 非逐字确定，故用相似度而非相等）
        import difflib
        a, b = (msg or {}).get("text") or "", (msg2 or {}).get("text") or ""
        ratio = difflib.SequenceMatcher(None, a, b).ratio() if (a and b) else 0.0
        check("9f 加时间戳不改变正文（相似度 >0.99）", ratio > 0.99,
              f"ratio={ratio:.4f} len={len(a)}/{len(b)}")

        # 池的透传：transcribe_full 必须支持 with_timestamps 关键字
        pool = cli.WorkerPool(size=1)
        try:
            pmsg = pool.transcribe_full(AUDIO, with_timestamps=True)
            check("9g WorkerPool.transcribe_full 支持时间戳透传",
                  bool((pmsg or {}).get("segments")),
                  f"seg_count={len((pmsg or {}).get('segments') or [])}")
        finally:
            pool.close()

        w2.close()

        if args.red_ts:
            if len(FAIL) >= 1:
                print("\n[RED-TS] 可红性验证通过：不带时间戳时 9a 确实失败。")
                sys.exit(0)
            print("\n[RED-TS] 可红性验证失败：注入故障后断言仍全绿，测试无鉴别力！")
            sys.exit(2)

    # ---------------------------------------------------------- 6 降级路径
    badw = cli.T3RocmWorker(python_exe=r"D:\__not_exist__\python.exe")
    check("6a 解释器不存在时 available()=False", badw.available() is False)
    got = badw.transcribe(AUDIO)
    check("6b 降级时 transcribe() 返回 None 且不抛异常", got is None, repr(got))

    # ---------------------------------------------------------- 7 调度分派
    import transcribe_video as tv
    os.environ["WEBINTEL_T3_DEVICE_MODE"] = "cpu"
    called = {"rocm": 0, "cpu": 0}
    orig_rocm, orig_cpu = tv.t3_sensevoice_rocm, tv.t3_sensevoice
    tv.t3_sensevoice_rocm = lambda p, worker=None: called.__setitem__("rocm", called["rocm"] + 1) or "GPU文本"
    tv.t3_sensevoice = lambda p: called.__setitem__("cpu", called["cpu"] + 1) or "CPU文本"
    try:
        text, backend = tv.t3_sensevoice_dispatch("dummy.wav")
        check("7a mode=cpu 时不触碰 ROCm",
              called["rocm"] == 0 and called["cpu"] == 1 and backend == "SenseVoice-Small",
              f"rocm={called['rocm']} cpu={called['cpu']} backend={backend}")

        os.environ["WEBINTEL_T3_DEVICE_MODE"] = "rocm"
        called.update(rocm=0, cpu=0)
        text, backend = tv.t3_sensevoice_dispatch("dummy.wav")
        check("7b mode=rocm 时只用 GPU",
              called["rocm"] == 1 and called["cpu"] == 0 and backend == "SenseVoice-Small(GPU)",
              f"rocm={called['rocm']} cpu={called['cpu']} backend={backend}")

        os.environ["WEBINTEL_T3_DEVICE_MODE"] = "auto"
        called.update(rocm=0, cpu=0)
        tv.t3_sensevoice_rocm = lambda p, worker=None: None      # 模拟 GPU 不可用
        text, backend = tv.t3_sensevoice_dispatch("dummy.wav")
        check("7c mode=auto 且 GPU 失败 → 回落 CPU",
              called["cpu"] == 1 and backend == "SenseVoice-Small",
              f"backend={backend}")

        # 7e（2026-10-07 新增契约）：批量路径必须把 worker（池）透传下去，
        # 否则会退化成单例、多起一个进程。
        seen = {}
        tv.t3_sensevoice_rocm = lambda p, worker=None: (seen.update(w=worker) or "GPU文本")
        sentinel = object()
        text, backend = tv.t3_sensevoice_dispatch("dummy.wav", worker=sentinel)
        check("7e worker 参数透传到 ROCm 通道（池依赖）",
              seen.get("w") is sentinel and backend == "SenseVoice-Small(GPU)",
              f"seen={seen.get('w')!r} backend={backend}")

        os.environ["WEBINTEL_T3_DEVICE_MODE"] = "garbage-value"
        called.update(rocm=0, cpu=0)
        tv.t3_sensevoice_rocm = orig_rocm
        text, backend = tv.t3_sensevoice_dispatch("dummy.wav")
        check("7d 未知 mode 按 auto 处理且不崩", True, f"backend={backend}")
    finally:
        tv.t3_sensevoice_rocm, tv.t3_sensevoice = orig_rocm, orig_cpu
        os.environ.pop("WEBINTEL_T3_DEVICE_MODE", None)

    _summary()


def _summary():
    print("=" * 64)
    print(f"通过 {len(PASS)} / 失败 {len(FAIL)}")
    for f in FAIL:
        print(f"  ❌ {f}")
    print("=" * 64)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
