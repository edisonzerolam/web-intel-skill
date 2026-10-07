#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
apply_subs: 安全应用订阅配置（TC-20260811-BILI6 V7）
====================================================
四条件：① 与管线物理隔离（本脚本独立，管线零写路径）
        ② dry-run 默认 + --apply + 交互确认
        ③ 应用前基线比对（当前 subs.json ≠ 推荐基线则拒绝，--force 显式覆盖）
        ④ 自动备份 subs.json.bak.<时间戳>（滚动 7 份）+ os.replace 原子替换 + 自检回滚

用法：
  python apply_subs.py subs.recommended.json            # dry-run：输出变化摘要
  python apply_subs.py subs.recommended.json --apply    # 正式应用（交互确认）
"""
import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from datetime import datetime

GROUP_WHITELIST = {"ai", "tech", "news", "industry", "politics", "military"}
SENSITIVE_DEFAULT = {"politics", "military"}
BACKUP_KEEP = 7


def sha256_file(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def load_subs(path):
    if not os.path.exists(path):
        return {"version": 1, "ups": [], "partitions": []}
    return json.load(open(path, encoding="utf-8"))


def diff_subs(current, recommended):
    """逐条 diff：added / removed / enabled 翻转 / group 变更（按 up_name 定位）。"""
    cur_map = {u.get("up_name"): u for u in current.get("ups", []) if u.get("up_name")}
    rec_map = {u.get("up_name"): u for u in recommended.get("ups", []) if u.get("up_name")}
    lines = []
    for name, u in rec_map.items():
        if name not in cur_map:
            lines.append(f"  + added: {name}")
        else:
            c = cur_map[name]
            if c.get("group") != u.get("group"):
                lines.append(f"  ~ group: {name}  {c.get('group', '-')} → {u.get('group', '-')}")
            if c.get("enabled") != u.get("enabled"):
                lines.append(f"  ~ enabled: {name}  {c.get('enabled')} → {u.get('enabled')}")
    for name in cur_map:
        if name not in rec_map:
            lines.append(f"  - removed: {name}")
    return lines


def main():
    ap = argparse.ArgumentParser(description="安全应用订阅配置")
    ap.add_argument("recommended", help="推荐配置路径（subs.recommended.json）")
    ap.add_argument("--target", default="subs.json", help="目标 subs.json")
    ap.add_argument("--apply", action="store_true", help="正式应用（默认 dry-run）")
    ap.add_argument("--force", action="store_true", help="基线不符时强制覆盖（先备份）")
    args = ap.parse_args()

    if not os.path.exists(args.recommended):
        sys.exit(f"[错误] 推荐配置不存在：{args.recommended}")
    rec = load_subs(args.recommended)
    cur = load_subs(args.target)

    # 校验推荐配置（group 白名单 + mid 唯一）
    mids = [u.get("mid") for u in rec.get("ups", [])]
    if len(mids) != len(set(mids)):
        sys.exit("[错误] 推荐配置 mid 重复")
    for u in rec.get("ups", []):
        g = u.get("group")
        if g and g not in GROUP_WHITELIST:
            sys.exit(f"[错误] 非法 group 值：{g}（白名单：{','.join(sorted(GROUP_WHITELIST))}）")

    # 基线比对（V7 ③）
    baseline = rec.get("_baseline") or {}
    if baseline.get("sha256"):
        cur_hash = sha256_file(args.target)
        if cur_hash != baseline["sha256"] and not args.force:
            print(f"[基线] 当前 {args.target} 与推荐基线不符（{baseline.get('generated_at', '?')} 生成）")
            print("[基线] 变化摘要：")
            for line in diff_subs(cur, rec):
                print(line)
            print("[基线] 中止。如需覆盖（先备份）：--force")
            sys.exit(2)

    # 变化摘要
    changes = diff_subs(cur, rec)
    print(f"[dry-run] 推荐 {len(rec.get('ups', []))} 个 UP（当前 {len(cur.get('ups', []))}）")
    if not changes:
        print("[dry-run] 无变化")
    for line in changes:
        print(line)

    if not args.apply:
        print("\n[提示] 未应用（dry-run）。确认后运行：--apply")
        return

    # ④ 备份 + 原子替换（V7）
    bak_dir = os.path.join(os.path.dirname(args.target) or ".", "backup")
    os.makedirs(bak_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    bak = os.path.join(bak_dir, f"subs.json.bak.{ts}")
    shutil.copy2(args.target, bak)
    print(f"[备份] {bak}")
    baks = sorted(f for f in os.listdir(bak_dir) if f.startswith("subs.json.bak."))
    for old in baks[:-BACKUP_KEEP]:
        os.remove(os.path.join(bak_dir, old))
        print(f"[备份] 清理过期 {old}")

    if not args.force:
        confirm = input("[确认] 应用推荐配置到 subs.json？输入 yes 继续：").strip()
        if confirm != "yes":
            print("已取消")
            return

    # 原子替换 + 自检回滚
    tmp = args.target + ".tmp"
    json.dump(rec, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    try:
        os.replace(tmp, args.target)
        check = load_subs(args.target)
        if len(check.get("ups", [])) != len(rec.get("ups", [])):
            raise RuntimeError("自检失败：UP 数不符")
        print(f"[完成] 已应用（{len(rec.get('ups', []))} 个 UP）")
    except Exception as e:
        shutil.copy2(bak, args.target)
        print(f"[回滚] 应用失败，已从备份恢复：{e}")
        sys.exit(3)


if __name__ == "__main__":
    main()
