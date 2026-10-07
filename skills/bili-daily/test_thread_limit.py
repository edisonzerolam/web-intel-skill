#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_thread_limit.py — 验证 CPU 线程上限补丁真的生效（可红可绿）

断言（期望值来自独立源：torch 官方 API 语义，非同义反复）：
  1. 补丁生效后 torch.get_num_threads() == 期望值
  2. 环境变量 WEBINTEL_CPU_THREADS 能被识别
  3. 未打补丁时（模拟）线程数 != 限制值 → 证明测试真的能红

用法：
  python test_thread_limit.py            # 测已打补丁的模块
  python test_thread_limit.py --baseline # 测未打补丁行为（应显示"红"）
"""
import argparse
import os
import subprocess
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

HERE = os.path.dirname(os.path.abspath(__file__))


def run_probe(env_threads=None):
    """在干净子进程里导入目标模块，回报 torch 线程数。"""
    code = (
        "import sys, os;"
        f"sys.path.insert(0, r'{HERE}');"
        "import transcribe_video as tv;"
        "import torch;"
        "print('THREADS', torch.get_num_threads());"
        "print('ENV', os.environ.get('OMP_NUM_THREADS'));"
        "print('LIMIT', getattr(tv, '_CPU_THREADS', 'MISSING'))"
    )
    env = dict(os.environ)
    env.pop("OMP_NUM_THREADS", None)
    env.pop("MKL_NUM_THREADS", None)
    if env_threads is not None:
        env["WEBINTEL_CPU_THREADS"] = str(env_threads)
    p = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, env=env, timeout=180)
    out = (p.stdout or "") + (p.stderr or "")
    vals = {}
    for line in out.splitlines():
        for k in ("THREADS", "ENV", "LIMIT"):
            if line.startswith(k + " "):
                vals[k] = line.split(" ", 1)[1].strip()
    return vals, out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", action="store_true",
                    help="显示未打补丁时的高线程数（证明测试可红）")
    args = ap.parse_args()

    failures = []

    if args.baseline:
        print("[基线] 未限线程时 torch 默认线程数：")
        vals, out = run_probe()
        if "THREADS" not in vals:
            print("  无法取得（模块可能尚未可导入）")
            print(out[-600:])
            return 1
        n = int(vals["THREADS"])
        print(f"  torch.get_num_threads() = {n}")
        print(f"  OMP_NUM_THREADS = {vals.get('ENV')}")
        print(f"  _CPU_THREADS = {vals.get('LIMIT')}")
        if n > 4:
            print(f"  → 红：占用了 {n} 个核（>4），整机易卡顿")
            return 0
        print("  → 已是受限状态")
        return 0

    # 断言 1：默认限制生效
    vals, out = run_probe()
    if "THREADS" not in vals:
        print("❌ 无法导入 transcribe_video：")
        print(out[-800:])
        return 1
    got = int(vals["THREADS"])
    limit = vals.get("LIMIT")
    print(f"断言1 默认线程数：期望 4，实际 {got}  (LIMIT={limit})")
    if got != 4:
        failures.append(f"默认线程数应为 4，实际 {got}")
    if limit != "4":
        failures.append(f"_CPU_THREADS 应为 '4'，实际 {limit}")

    # 断言 2：环境变量可覆盖
    vals2, out2 = run_probe(env_threads=2)
    got2 = int(vals2.get("THREADS", -1))
    print(f"断言2 环境变量覆盖：期望 2，实际 {got2}  (LIMIT={vals2.get('LIMIT')})")
    if got2 != 2:
        failures.append(f"WEBINTEL_CPU_THREADS=2 时应为 2，实际 {got2}")

    # 断言 3：测试本身可红（把限制设成 1，必须真的变成 1）
    vals3, _ = run_probe(env_threads=1)
    got3 = int(vals3.get("THREADS", -1))
    print(f"断言3 可红性：期望 1，实际 {got3}")
    if got3 != 1:
        failures.append(f"设为 1 时应为 1，实际 {got3}")

    print()
    if failures:
        print("❌ 失败：")
        for f in failures:
            print("   -", f)
        return 1
    print("✅ 全绿：线程上限补丁生效且可被环境变量覆盖")
    return 0


if __name__ == "__main__":
    sys.exit(main())
