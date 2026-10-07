#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
analyze_watch: 观看记录实证分析（TC-20260811-BILI5 V2-V7）
==========================================================
输入：watch_ups.json（观看聚合）+ subs.json（关注）+ _followings_cache.json（sign 快照）
输出：实证题材分布 + 关注×观看交叉 + 实证/综合双榜 + 建议前 5 配置

S4 实证化（V3）：S4_emp=0.35·min(1,n/10)+0.25·min(1,d/8)+0.40·e^(−Δd/60)；n=0→0.30 标「窗口内未观测」
晋级（V4）：n≥10 直接高价值；5≤n<10 且 sign 空 → S1_beh（zones 最大分区占比 ≥3 条）
sign 融合（V2）：观看≥3 次 → 题材取观看 zones 众数
窗口质量门（P3）：W<30 天 → S4 参数冻结、仅降级披露

用法：
  python analyze_watch.py --out watch_analysis.md
"""
import argparse
import json
import math
import os
import sys
from collections import Counter
from datetime import date, datetime

# 窗口质量门（P3）：W<30 天参数冻结
FROZEN_WINDOW = 30


def load_json(p, default=None):
    if os.path.exists(p):
        return json.load(open(p, encoding="utf-8"))
    return default if default is not None else {}


def s4_emp(n, d, delta_d, frozen):
    """S4_emp 三要素（V3）。frozen=短窗口截断时不调参仅披露。"""
    if n == 0:
        return 0.30, "窗口内未观测"
    v = 0.35 * min(1, n / 10) + 0.25 * min(1, d / 8) + 0.40 * math.exp(-delta_d / 60)
    return round(v, 3), ("短窗口截断" if frozen else "实证")


def main():
    ap = argparse.ArgumentParser(description="观看记录实证分析")
    ap.add_argument("--out", default="watch_analysis.md")
    ap.add_argument("--watch", default="watch_ups.json")
    ap.add_argument("--subs", default="subs.json")
    args = ap.parse_args()

    watch = load_json(args.watch)
    subs = load_json(args.subs)
    cache = load_json("_followings_cache.json", [])
    today = date.today().isoformat()

    ups_watch = watch.get("ups", {})
    window_days = watch.get("window_days", 0)
    frozen = window_days < FROZEN_WINDOW
    now = datetime.now().timestamp()

    # 关注 UP 的 sign 快照
    sign_of = {f.get("mid"): (f.get("sign") or "", (f.get("official_verify") or {}).get("type", -1))
               for f in cache}
    follow_mids = {u.get("mid") for u in subs.get("ups", [])}

    # 1. 实证聚合：观看的 UP（含关注与非关注）
    rows = []
    for mid, u in ups_watch.items():
        mid = int(mid)
        n, total = u.get("n", 0), u.get("total", 0)
        first, last = u.get("first_view_at", 0), u.get("last_view_at", 0)
        d = max(1, (last - first) // 86400) if first and last else 1
        delta_d = max(0, (now - last) // 86400) if last else 999
        zones = u.get("zones", {})
        top_zone = max(zones, key=zones.get) if zones else "未知"
        s4, tag = s4_emp(n, d, delta_d, frozen)
        rows.append({"mid": mid, "uname": u.get("uname", ""), "n": n, "total": total,
                     "d": d, "delta_d": delta_d, "s4": s4, "s4_tag": tag,
                     "top_zone": top_zone, "zones": zones, "last_view_at": last,
                     "followed": mid in follow_mids,
                     "sign": sign_of.get(mid, ("", -1))[0],
                     "auth": sign_of.get(mid, ("", -1))[1]})

    # 2. 题材（V2 融合）：观看≥3 次用 zones 众数；否则用 sign 分类
    import analyze_ups as au
    for r in rows:
        if r["n"] >= 3 and r["zones"]:
            r["topic"] = r["top_zone"]
            r["topic_src"] = "观看实证"
        else:
            t, conf, sig, hits = au.classify(r["sign"], {"type": r["auth"]})
            r["topic"] = t
            r["topic_src"] = "sign推断"

    # 3. 综合分（V3：五维，S4 用实证值）
    for r in rows:
        total_z = sum(r["zones"].values()) or 1
        if r["n"] >= 10:
            s1 = min(1.0, r["n"] / 10)
            v = 0.35 * s1 + 0.25 * s1 + 0.15 * (0.5 if r["auth"] == 0 else 0.5) \
                + 0.15 * r["s4"] + 0.10 * (1.0 if r["delta_d"] <= 180 else 0.5)
            r["score"] = round(v, 3)
            r["level"] = "高价值"
            r["level_reason"] = f"实证晋级（{r['n']} 次观看）"
        elif r["n"] >= 5 and r["topic_src"] == "sign推断" and r["topic"] in ("未知", "other"):
            # S1_beh：zones 最大分区占比（≥3 条）
            s1_beh = r["zones"].get(r["top_zone"], 0) / total_z if r["n"] >= 3 else 0.0
            v = 0.35 * s1_beh + 0.25 * s1_beh + 0.15 * (0.5 if r["auth"] == 0 else 0.5) \
                + 0.15 * r["s4"] + 0.10 * (1.0 if r["delta_d"] <= 180 else 0.5)
            r["score"] = round(v, 3)
            r["level"] = "高价值" if r["score"] >= 0.70 else ("中价值" if r["score"] >= 0.45 else "低价值")
            r["level_reason"] = f"行为题材推断 S1_beh={s1_beh:.2f}"
        else:
            s1 = 0.0 if r["topic"] in ("未知", "other") else min(1.0, r["n"] / 5)
            s2 = s1
            s3 = 0.8 if r["auth"] == 0 else 0.5
            s5 = 1.0 if r["delta_d"] <= 180 else (0.7 if r["delta_d"] <= 730 else 0.5)
            v = 0.35 * s1 + 0.25 * s2 + 0.15 * s3 + 0.15 * r["s4"] + 0.10 * s5
            r["score"] = round(v, 3)
            r["level"] = "高价值" if (r["score"] >= 0.70 and r["topic_src"] == "观看实证") \
                else ("中价值" if r["score"] >= 0.45 else "低价值")
            r["level_reason"] = r["s4_tag"]

    # 4. 排序 + 双榜
    rows.sort(key=lambda x: (-x["n"], x["delta_d"]))
    empirical = [r for r in rows if r["n"] >= 3]
    comprehensive = sorted([r for r in rows if r["followed"]],
                           key=lambda x: -x["score"])
    high_emp = [r for r in empirical if r["n"] >= 10 or r["level"] == "高价值"]

    # 5. 关注×观看交叉
    watched_followed = [r for r in rows if r["followed"]]
    watched_not_followed = [r for r in rows if not r["followed"]]
    follow_not_watched = len(follow_mids) - len({r["mid"] for r in watched_followed})

    # 6. 报告
    L = [f"<!-- 个人数据分析·不公开发布 ｜ 生成 {today} ｜ 数据源：观看历史聚合+关注列表 -->",
         "# B站观看记录实证分析（UP 生效价值）", "",
         f"> 个人数据分析·不公开发布 ｜ 生成 {today}",
         f"> 数据来源：本人观看历史（聚合 {len(rows)} UP / {sum(r['n'] for r in rows)} 视频）+ 关注列表",
         "> 用途限制：不公开发布/不转售/不用于第三方投放",
         f"> ⚠️ 窗口：{window_days} 天"
         + ("（**短窗口截断，S4 参数冻结仅披露**）" if frozen else "（正常）")
         + f" ｜ 观看 UP {len(rows)} 个 / 关注 {len(follow_mids)} 个",
         "",
         "## 一、实证观看总览", "",
         f"- 窗口内观看：{sum(r['total'] for r in rows)} 次 / {sum(r['n'] for r in rows)} 个去重视频 / {len(rows)} 个 UP",
         f"- 关注×观看交叉：关注且观看 {len(watched_followed)} ｜ 观看但未关注 {len(watched_not_followed)} ｜ 关注但窗口内未观测 {follow_not_watched}",
         "",
         "### 观看题材分布（zones 官方分区聚合）", "",
         "| 分区 | 视频数 |", "|------|--------|"]
    zone_cnt = Counter()
    for r in rows:
        for z, c in r["zones"].items():
            zone_cnt[z] += c
    for z, c in zone_cnt.most_common(12):
        L.append(f"| {z} | {c} |")
    L += ["", "## 二、实证高价值 UP（观看排序）", "",
          "| # | UP | 观看 | 去重 | 最近观看 | 题材(来源) | 关注? |", "|---|----|------|------|---------|-----------|-------|"]
    for i, r in enumerate(high_emp[:20], 1):
        recent = datetime.fromtimestamp(r["last_view_at"]).date() if r.get("last_view_at") else "-"
        L.append(f"| {i} | {r['uname'][:18]} | {r['total']} | {r['n']} | {recent} | "
                 f"{r['topic']}({r['topic_src']}) | {'✅' if r['followed'] else '—'} |")
    L += ["", "## 三、关注且常看（综合分排序 TOP20）", "",
          "| # | UP | 题材 | 综合分 | 观看 | 理由 |", "|---|----|------|--------|------|------|"]
    for i, r in enumerate(comprehensive[:20], 1):
        L.append(f"| {i} | {r['uname'][:18]} | {r['topic']}({r['topic_src']}) | {r['score']:.2f} | "
                 f"{r['n']} | {r['level_reason']} |")
    L += ["", "## 四、观看但未关注（潜在新关注）", "",
          "| UP | 观看 | 题材 |", "|----|------|------|"]
    for r in sorted(watched_not_followed, key=lambda x: -x["n"])[:10]:
        L.append(f"| {r['uname'][:18]} | {r['n']} | {r['topic']}({r['topic_src']}) |")
    L += ["", "## 五、行动建议", "",
          "1. 实证高价值 UP（观看≥3 次）建议置入 subs.json 监控（含未关注的：潜在新关注）",
          "2. 关注但窗口内未观测的 UP：保留关注但排后（关注意图保留，不自动取关）",
          "3. 校准状态："
          + ("参数冻结（窗口<30 天），14 天后金标准回测调参" if frozen else "参数生效中"),
          "",
          "---",
          f"*个人数据分析·不公开发布 ｜ 生成：analyze_watch（TC-20260811-BILI5）｜ S4_emp 参数冻结状态：{'是' if frozen else '否'}*", ""]
    with open(args.out, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print(f"[完成] {args.out} ｜ 观看 UP {len(rows)} ｜ 高价值 {len(high_emp)} ｜ 关注常看 {len(comprehensive)}"
          f" ｜ 窗口 {window_days} 天{'（冻结）' if frozen else ''}")


if __name__ == "__main__":
    main()
