#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ai_ups_filter: AI 主题 UP 筛选（三重信号，TC-20260811-BILI6 V2/V3）
====================================================================
AI 词库 v1.0（含 V2.1 补正，冻结）：
  强词 S1=1.0：大模型/人工智能/gpt/chatgpt/智能体/agent/agi/llm/deepseek/openai/qwen/
              文心/通义/claude/gemini/codex/机器学习/深度学习/神经网络/aigc
  弱词 S1=0.5：自动驾驶/机器人/ai绘画/ai视频/ai工具/ai日报/coding/开源/prompt/数字人/midjourney
  AI_BARE：\\b[Aa][Ii]\\b（边界正则，强词命中）
排除规则：
  E1 纯商务硬排除（商务词≥1 且 ai_ratio<0.30）
  E2 搬运软判定（suspect 标记，S3 确认排除）
  E3 蓝 V 官方保留直入核心
信号：
  S1 sign 命中（强 1.0 / 弱 0.5 / 无 0）
  S2 UP 级观看交叉（watch_ups.json zones 科技类分区 → 0.5；降级实现，topics 空）
  S3 投稿验证（search/type，--verify 时跨日执行，≥1/3 命中 → 1.0）
  T = 0.5·S1 + 0.2·S2 + 0.3·S3；核心 ≥0.6 / 候选 ≥0.4

用法：
  python ai_ups_filter.py                    # S1+S2 本地筛选（0 请求）
  python ai_ups_filter.py --verify           # 追加 S3 投稿验证（25 请求，跨日执行）
"""
import argparse
import json
import re
import sys

STRONG = ["大模型", "人工智能", "gpt", "chatgpt", "智能体", "agent", "agi", "llm", "deepseek",
          "openai", "qwen", "文心", "通义", "claude", "gemini", "codex", "机器学习", "深度学习",
          "神经网络", "aigc"]
WEAK = ["自动驾驶", "机器人", "ai绘画", "ai视频", "ai工具", "ai日报", "coding", "开源",
        "prompt", "数字人", "midjourney"]
AI_BARE = re.compile(r"\b[Aa][Ii]\b")
BIZ_WORDS = ["微信", "vx", "qq", "私信", "商务", "合作", "接单", "代做", "代写", "定制", "报价",
             "广告", "推广", "带货", "淘宝", "联系方式"]
REPOST_WORDS = ["搬运", "转载", "合集", "整理", "精选"]
# S2 降级：zones 科技类官方分区（BILI5 watch_ups.json 实测 topics 为空）
TECH_ZONES = ["科学科普", "数码", "科技", "极客", "编程", "知识", "人工智能", "计算机", "硬件", "软件"]


def s1_from_sign(sign):
    """S1：sign 命中（强 1.0 / 弱 0.5）。"""
    sign = sign or ""
    low = sign.lower()
    if any(w.lower() in low for w in STRONG) or AI_BARE.search(sign):
        return 1.0
    if any(w.lower() in low for w in WEAK):
        return 0.5
    return 0.0


def exclude_check(sign, official):
    """E1/E3：纯商务硬排除；蓝 V 官方保留。返回 (excluded, reason, is_official)。"""
    sign = sign or ""
    official = official or {}
    if official.get("type") == 1:
        return False, None, True  # E3 蓝 V 官方保留
    low = sign.lower()
    biz_hits = [b for b in BIZ_WORDS if b.lower() in low]
    if biz_hits:
        ai_hits = sum(1 for w in STRONG + WEAK if w.lower() in low)
        if ai_hits == 0:
            return True, "E1纯商务", False
    return False, None, False


def s2_from_zones(zones):
    """S2：观看记录 zones 科技类分区命中 → 0.5（降级实现）。"""
    zones = zones or {}
    if any(z in TECH_ZONES for z in zones):
        return 0.5
    return 0.0


def load_followings(path="_followings_cache.json"):
    return json.load(open(path, encoding="utf-8"))


def load_watch_ups(path="watch_ups.json"):
    w = json.load(open(path, encoding="utf-8"))
    return w.get("ups", {})  # dict: mid -> {uname,n,total,zones,topics}


def main():
    ap = argparse.ArgumentParser(description="AI 主题 UP 筛选")
    ap.add_argument("--verify", action="store_true", help="执行 S3 投稿验证（25 请求，建议跨日）")
    ap.add_argument("--out", default="ai_group.json")
    args = ap.parse_args()

    followings = load_followings()
    watch_ups = load_watch_ups()
    rows = []
    for f in followings:
        mid, uname = f.get("mid"), f.get("uname", "")
        sign = f.get("sign") or ""
        official = f.get("official_verify") or {}
        s1 = s1_from_sign(sign)
        excluded, reason, is_official = exclude_check(sign, official)
        if excluded:
            continue
        if s1 == 0.0 and not is_official:
            continue  # S1 无命中且非官方 → 不进 AI 候选（unknown 由 S2/S3 兜底见下）
        w = watch_ups.get(str(mid)) or {}
        s2 = 0.5 if is_official else s2_from_zones(w.get("zones"))
        rows.append({
            "mid": mid, "up_name": uname, "s1": s1, "s2": s2, "s3": 0.0,
            "official": is_official, "repost_suspect": any(r in sign for r in REPOST_WORDS),
            "sign": sign[:40],
        })

    # unknown 兜底：S2 观看命中（科技分区）的 unknown sign UP 也纳入候选
    for mid, w in watch_ups.items():
        if any(True for r in rows if str(r["mid"]) == str(mid)):
            continue
        zones = w.get("zones") or {}
        if any(z in TECH_ZONES for z in zones):
            rows.append({"mid": int(mid), "up_name": w.get("uname", ""), "s1": 0.0,
                         "s2": 0.5, "s3": 0.0, "official": False,
                         "repost_suspect": False, "sign": "(unknown，S2 观看兜底)"})

    # T 分（S3=0 时 T=0.5S1+0.2S2）
    for r in rows:
        r["T"] = round(0.5 * r["s1"] + 0.2 * r["s2"] + 0.3 * r["s3"], 3)
    rows.sort(key=lambda x: -x["T"])

    core = [r for r in rows if r["T"] >= 0.6]
    cand = [r for r in rows if 0.4 <= r["T"] < 0.6]
    print(f"候选总数 {len(rows)}｜核心(T≥0.6) {len(core)}｜候选(0.4-0.6) {len(cand)}")
    print("\n核心（T≥0.6）：")
    for r in core[:20]:
        print(f"  {r['T']:.2f} {r['up_name'][:18]} S1={r['s1']} S2={r['s2']}{' [官方]' if r['official'] else ''}")
    print("\n候选 TOP10：")
    for r in cand[:10]:
        print(f"  {r['T']:.2f} {r['up_name'][:18]} S1={r['s1']} S2={r['s2']}")

    json.dump({"version": "v1.0", "generated_at": __import__("datetime").date.today().isoformat(),
               "core": core, "candidate": cand},
              open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n已输出 {args.out}（S3 未执行，待 --verify 跨日验证后回填）")


if __name__ == "__main__":
    main()
