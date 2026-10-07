#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_t3_pool: T3 ROCm worker 池（P0 并行）验收测试
==================================================
覆盖：
  1. parse_pool_size 语义（默认/钳制/非法值）
  2. 池成员：tag 唯一、日志文件唯一、first_time()
  3. 真并发转录 4 条音频：4/4 成功、**逐条字数对号入座（防串话）**
  4. 结构性并行证据：运行期同时存活的 worker 进程峰值 ≥ 2
  5. 每个 worker 都真的用 cuda（非静默回落 CPU）
  6. 池关闭后进程全部释放
  7. 池不可用时优雅返回 None（不抛异常）
  8. 合规：fetch_subtitles.pace() 加锁后，多线程并发仍满足全局最小间隔

用法：
  python -u test_t3_pool.py            # 全绿
  python -u test_t3_pool.py --red 3    # 可红性自检：注入故障，断言 3 必须失败
"""
import os
import sys
import time
import threading

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import t3_rocm_client as cli  # noqa: E402

AUDIO_DIR = r"C:\Temp\t3audio"
# 单条基线（b41 / T3-SenseVoice-ROCm-GPU接入报告 实测；SenseVoice 有 ±1 字抖动）
BASELINE = {
    "BV1cuZwYZEZr.m4a": 822,   # 300.7s
    "BV15FZGYyEdc.m4a": 995,   # 195.2s
    "BV1U5um66END.m4a": 104,   #  88.0s
    "BV1sKZPYBEud.m4a": 7,     #  27.1s
}
TOL = 3

PASS, FAIL = [], []


def check(cond, label, detail=""):
    (PASS if cond else FAIL).append(label)
    mark = "✅" if cond else "❌"
    print(f"  {mark} {label}" + (f"  [{detail}]" if detail else ""), flush=True)
    return bool(cond)


def main():
    red = None
    if "--red" in sys.argv:
        i = sys.argv.index("--red")
        red = int(sys.argv[i + 1]) if len(sys.argv) > i + 1 else 1

    print("=" * 72, flush=True)
    print("T3 ROCm worker 池验收", flush=True)
    print("=" * 72, flush=True)

    # ---------------- 1. parse_pool_size 语义 ----------------
    print("\n[1] parse_pool_size 语义", flush=True)
    if red == 1:                      # 注入故障：故意给错期望值
        check(cli.parse_pool_size(None) == 99, "1a 默认 = 2")
    else:
        check(cli.parse_pool_size(None) == 2, "1a 默认 = 2")
    check(cli.parse_pool_size(1) == 1, "1b 显式 1 = 1")
    check(cli.parse_pool_size(4) == 4, "1c 显式 4 = 4")
    check(cli.parse_pool_size(99) == cli.POOL_MAX, "1d 超上限钳制到 POOL_MAX")
    check(cli.parse_pool_size("x") == 1, "1e 非法值回落 1")
    check(cli.parse_pool_size(0) == 1, "1f 0 钳制到 1")
    check(cli.parse_pool_size(-5) == 1, "1g 负数钳制到 1")

    # ---------------- 2. 池成员 ----------------
    print("\n[2] 池成员结构", flush=True)
    p3 = cli.WorkerPool(size=3)
    tags = [w.tag for w in p3._workers]
    logs = [os.path.basename(w._log_path) for w in p3._workers]
    check(p3.size == 3, "2a size 正确", f"{p3.size}")
    check(tags == [0, 1, 2], "2b tag 从 0 递增且唯一", f"{tags}")
    check(len(set(logs)) == 3, "2c 日志文件互不覆盖", f"{logs}")
    check(p3.first_time() is True, "2d 未启动时 first_time()=True")
    check(p3._free.qsize() == 3, "2e 空闲队列已填满 3")
    p3.close()

    # ---------------- 环境前置 ----------------
    py = cli.DEFAULT_ROCM_PYTHON
    audio = {k: os.path.join(AUDIO_DIR, k) for k in BASELINE}
    missing = [k for k, v in audio.items() if not os.path.exists(v)]
    if not os.path.exists(py) or missing:
        print(f"\n⏭  环境缺失（python={os.path.exists(py)}，缺音频={missing}），"
              f"跳过 3~7 的真实并发测试", flush=True)
        return report(red)
    check(True, "0a 前置：ROCm 解释器与 4 条音频齐备")

    # ---------------- 3. 真并发 + 防串话 ----------------
    print("\n[3] 池并发转录 4 条（size=2）", flush=True)
    pool = cli.WorkerPool(size=2)
    peak = [0]
    stop_mon = threading.Event()

    def monitor():
        while not stop_mon.is_set():
            live = sum(1 for w in pool._workers
                       if w.proc is not None and w.proc.poll() is None)
            peak[0] = max(peak[0], live)
            time.sleep(0.2)

    mon = threading.Thread(target=monitor, daemon=True)
    mon.start()

    results, t0 = {}, time.perf_counter()
    try:
        with __import__("concurrent.futures", fromlist=["x"]).ThreadPoolExecutor(
                max_workers=pool.size) as ex:
            futs = {ex.submit(pool.transcribe, path): name
                    for name, path in audio.items()}
            for fu in __import__("concurrent.futures", fromlist=["x"]).as_completed(futs):
                results[futs[fu]] = fu.result()
    except Exception as e:
        results["__error__"] = f"{type(e).__name__}: {e}"
    wall = time.perf_counter() - t0
    stop_mon.set()
    mon.join(timeout=2)

    check("__error__" not in results, "3a 并发过程无异常",
          str(results.get("__error__", ""))[:150])
    got = {k: (len(v) if v else 0) for k, v in results.items() if k in BASELINE}
    check(len(got) == 4 and all(v > 0 for v in got.values()),
          "3b 4/4 全部非空", f"{got}")
    wrong = {k: (got.get(k), BASELINE[k]) for k in BASELINE
             if abs(got.get(k, -999) - BASELINE[k]) > TOL}
    if red == 3:
        wrong = wrong or {"注入": (0, 1)}   # 注入故障：强制判串话
    check(not wrong, "3c 逐条字数对号入座（防串话）",
          f"不符={wrong}" if wrong else "4/4 命中基线 ±%d" % TOL)
    check(peak[0] >= 2, "3d 运行期同时存活进程峰值 ≥ 2（真并行）", f"peak={peak[0]}")
    print(f"     墙钟 {wall:.1f}s（4 条音频 611s）", flush=True)

    # ---------------- 4. 每个 worker 真的用 GPU ----------------
    print("\n[4] 设备与进程身份", flush=True)
    infos = [(w.tag, (w.info or {}).get("device"), w.proc.pid if w.proc else None)
             for w in pool._workers]
    started = [x for x in infos if x[2] is not None]
    check(len(started) == 2, "4a 两个 worker 都已拉起", f"{started}")
    check(all(d == "cuda" for _, d, _ in started), "4b 全部 device=cuda（非静默回落）",
          f"{[d for _, d, _ in started]}")
    pids = [pid for _, _, pid in started]
    check(len(set(pids)) == len(pids), "4c 进程 PID 互不相同", f"{pids}")

    # ---------------- 5. 关闭释放 ----------------
    print("\n[5] 关闭与释放", flush=True)
    if red == 5:
        check(False, "5a close() 后所有进程已退出", "注入故障")
    else:
        pool.close()
        alive = [w for w in pool._workers
                 if w.proc is not None and w.proc.poll() is None]
        check(not alive, "5a close() 后所有进程已退出",
              f"残留={[w.tag for w in alive]}")

    # ---------------- 6. 不可用时优雅降级 ----------------
    print("\n[6] 降级路径", flush=True)
    bad = cli.WorkerPool(size=1, python_exe=r"C:\nonexistent\python.exe")
    check(bad.available() is False, "6a 解释器缺失时 available()=False")
    try:
        r = bad.transcribe(audio["BV1sKZPYBEud.m4a"])
        ok6 = r is None
    except Exception as e:
        ok6 = False
        print(f"      异常 {type(e).__name__}: {e}", flush=True)
    check(ok6, "6b 不可用时 transcribe() 返回 None 且不抛异常")
    bad.close()

    # ---------------- 7. 合规：pace() 并发仍守最小间隔 ----------------
    print("\n[7] 合规：pace() 并发限速", flush=True)
    import fetch_subtitles as fs
    n = 4
    stamps = []
    lk = threading.Lock()

    def worker_pace():
        fs.pace()
        with lk:
            stamps.append(time.time())

    t1 = time.perf_counter()
    ths = [threading.Thread(target=worker_pace) for _ in range(n)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    span = time.perf_counter() - t1
    # 加锁后 4 次调用被串行化：间隔 ≥ MIN_INTERVAL 抖动，总跨度必然 ≥ (n-1)*MIN_INTERVAL
    floor = (n - 1) * fs.MIN_INTERVAL
    check(span >= floor, f"7a {n} 线程并发 pace() 总跨度 ≥ {floor:.1f}s（锁生效）",
          f"实测 {span:.2f}s")
    stamps.sort()
    gaps = [stamps[i + 1] - stamps[i] for i in range(len(stamps) - 1)]
    check(all(g >= fs.MIN_INTERVAL - 0.05 for g in gaps),
          "7b 相邻请求间隔均 ≥ MIN_INTERVAL",
          f"gaps={[round(g, 2) for g in gaps]}")

    return report(red)


def report(red):
    print("\n" + "=" * 72, flush=True)
    print(f"通过 {len(PASS)} / 失败 {len(FAIL)}", flush=True)
    if FAIL:
        for f in FAIL:
            print(f"  ❌ {f}", flush=True)
    if red is not None:
        if FAIL:
            print(f"可红性验证通过：注入故障 {red} 后断言确实失败", flush=True)
            return 0
        print(f"可红性验证失败：注入故障 {red} 后断言仍然全绿（测试无效）", flush=True)
        return 1
    print("✅ 全绿" if not FAIL else "❌ 有失败", flush=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
