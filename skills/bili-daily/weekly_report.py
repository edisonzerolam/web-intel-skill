#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
weekly_report: B站每日洞察周报/月报渲染器（关键词趋势追踪）
=============================================================
按终审 TC-20260811-BILI3 V7 实现。纯本地聚合 corpus.jsonl（统计语料，无全文），零新增请求。

算法：
  - 词频双口径：绝对提及数（含词视频数）+ 相对频率（含词视频数/当日视频数）
  - 涨跌判定：当前窗口 vs 上一窗口的相对频率环比
  - 三榜分流：新词（本周 first_seen 且上周频率=0）/ 飙升（增幅 topN + 门槛）/ 消退（降幅 topN + 门槛）
  - 门槛：min_days=2（至少 2 天出现）、min_count=3（窗口内 ≥3 视频提及）；topN 按窗口缩放（周15/月30）
  - 月报：覆盖 ≥21 日保月报口径（标注覆盖 N 日），<21 日降级周报口径并标注
  - 缺口：断跑日 = 期望日期 - 实际日期；缺口 ≥ max(3, 0.2×days) 触发置信度降级提示
  - 水印双处：头部引用块 + 文件首行注释（个人研究·非商业·勿公开传播 + 生成日期 + 窗口截止日）

用法：
  python weekly_report.py --corpus corpus.jsonl --out weekly_2026-08-11.md [--days 7|30]
"""
import argparse
import json
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

WATERMARK = "个人学习研究·非商业用途·勿公开传播"


def load_corpus(path):
    """读取 corpus.jsonl → [{date,bvid,partition,title,keywords[],summary,sentiment,seg_ts}]。"""
    rows = []
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def daily_stats(rows):
    """按日聚合：{date: {total: n, words: Counter{word: 含词视频数}}}。"""
    by_day = defaultdict(lambda: {"total": 0, "words": Counter()})
    for r in rows:
        d = r.get("date", "")
        if not d:
            continue
        by_day[d]["total"] += 1
        for w in (r.get("keywords") or []):
            by_day[d]["words"][w] += 1
    return by_day


def window_agg(by_day, days):
    """滚动窗口聚合：近 days 天 vs 前 days 天，返回 (cur, prev, dates_cur, dates_prev)。"""
    dates = sorted(by_day.keys())
    end = dates[-1] if dates else None
    if not end:
        return None, None, [], []
    end_d = datetime.strptime(end, "%Y-%m-%d").date()
    cut = end_d - timedelta(days=days)
    cur_dates = [d for d in dates if d > cut.isoformat()]
    prev_dates = [d for d in dates if d <= cut.isoformat()][-days:]
    cur = {"total": sum(by_day[d]["total"] for d in cur_dates),
           "words": sum((by_day[d]["words"] for d in cur_dates), Counter())}
    prev = {"total": sum(by_day[d]["total"] for d in prev_dates),
            "words": sum((by_day[d]["words"] for d in prev_dates), Counter())}
    return cur, prev, cur_dates, prev_dates


def rel_freq(words, total):
    return {w: c / max(total, 1) for w, c in words.items()}


def compute_boards(cur, prev, top_n, min_days, min_count):
    """三榜：新词 / 飙升 / 消退。"""
    cur_f = rel_freq(cur["words"], cur["total"])
    prev_f = rel_freq(prev["words"], prev["total"])
    new_words, rising, falling = [], [], []
    for w, f in cur_f.items():
        c = cur["words"][w]
        if c < min_count:
            continue
        p = prev_f.get(w, 0.0)
        if p == 0.0:
            new_words.append((w, f, c))
        elif f > p:
            rising.append((w, f - p, f, p, c))
        elif p > 0 and p - f > 0:
            falling.append((w, p - f, f, p, c))
    new_words.sort(key=lambda x: -x[1])
    rising.sort(key=lambda x: -x[1])
    falling.sort(key=lambda x: -x[1])
    return new_words[:top_n], rising[:top_n], falling[:top_n]


def gap_info(all_dates, days):
    """断跑缺口：期望日期集合 vs 实际日期集合。"""
    if not all_dates:
        return [], 0
    end = datetime.strptime(all_dates[-1], "%Y-%m-%d").date()
    expected = {(end - timedelta(days=i)).isoformat() for i in range(days)}
    actual = set(all_dates[-days:])
    missing = sorted(expected - actual)
    return missing, len(missing)


def render_weekly(corpus_path, output_path, days=7, top_n=None, min_days=2, min_count=3,
                  watermark=True):
    """渲染周报（days=7）或月报（days=30）。"""
    rows = load_corpus(corpus_path)
    if not rows:
        print("[错误] corpus 为空", file=__import__("sys").stderr)
        return
    by_day = daily_stats(rows)
    top_n = top_n or (15 if days <= 7 else 30)
    cur, prev, cur_dates, prev_dates = window_agg(by_day, days)

    today = date.today().isoformat()
    end_d = datetime.strptime(sorted(by_day.keys())[-1], "%Y-%m-%d").date()
    missing, n_missing = gap_info(sorted(by_day.keys()), days)
    data_days = len(cur_dates)

    # 月报口径判定（V7）：≥21 日保月报口径，<21 日降级周报口径
    actual_days = data_days
    degrade = False
    if days == 30 and actual_days < 21:
        degrade = True
        days_eff = 7
        cur, prev, cur_dates, prev_dates = window_agg(by_day, 7)
        top_n = 15
    else:
        days_eff = days

    new_words, rising, falling = compute_boards(cur, prev, top_n, min_days, min_count)
    low_conf = n_missing >= max(3, int(0.2 * days))

    # 分区热力（窗口内）
    part = Counter(r.get("partition", "未知") for r in rows if r.get("date") in set(cur_dates))

    L = []
    if watermark:
        L.append(f"<!-- {WATERMARK} ｜ 生成 {today} ｜ 窗口截止 {end_d} -->")
    L += [f"# B站热门内容周报", "",
          f"> {WATERMARK} ｜ 生成 {today} ｜ 窗口截止 {end_d}",
          f"> 窗口：{days_eff} 日滚动（{cur_dates[0]} ~ {cur_dates[-1]}）｜ 实际有数据 {data_days} 天"
          + (f"｜ 缺 {n_missing} 天：{', '.join(missing[:6])}{'…' if len(missing) > 6 else ''}" if missing else ""),
          f"> 数据来源：corpus 统计语料（本地聚合，无全文）｜ {('⚠️ 数据缺口较大，趋势结论置信度降级' if low_conf else '')}",
          ""]

    if degrade:
        L += [f"> ⚠️ 月报数据不足 21 天（实际 {actual_days} 天），已降级为 {days_eff} 日周报口径。", ""]

    # 一、本周热词 TOP
    L += ["## 一、窗口热词 TOP", "", "| # | 关键词 | 提及视频数 | 相对频率 |", "|---|--------|-----------|---------|"]
    cur_f = rel_freq(cur["words"], cur["total"])
    for i, (w, f) in enumerate(sorted(cur_f.items(), key=lambda x: -x[1])[:top_n], 1):
        L.append(f"| {i} | {w} | {cur['words'][w]} | {f:.4f} |")
    L.append("")

    # 二、涨跌三榜
    L += ["## 二、趋势榜", ""]
    L += ["### 🆕 新词（本窗口首见）"]
    L += ["| 关键词 | 相对频率 | 提及视频数 |", "|--------|---------|-----------|"]
    for w, f, c in new_words[:min(10, top_n)]:
        L.append(f"| {w} | {f:.4f} | {c} |")
    L.append("")
    L += ["### 📈 飙升词（环比增幅）"]
    L += ["| 关键词 | 增幅 | 当前频率 | 提及视频数 |", "|--------|------|---------|-----------|"]
    for w, d, f, p, c in rising[:min(10, top_n)]:
        L.append(f"| {w} | {d:.4f} | {f:.4f} | {c} |")
    L.append("")
    L += ["### 📉 消退词（环比降幅）"]
    L += ["| 关键词 | 降幅 | 当前频率 | 提及视频数 |", "|--------|------|---------|-----------|"]
    for w, d, f, p, c in falling[:min(10, top_n)]:
        L.append(f"| {w} | {d:.4f} | {f:.4f} | {c} |")
    L.append("")

    # 三、分区热力
    L += ["## 三、分区热力（窗口内）", "", "| 分区 | 视频数 |", "|------|--------|"]
    for p, c in part.most_common(10):
        L.append(f"| {p} | {c} |")
    L.append("")

    # 四、趋势解读（规则模板+占位符，无 LLM）
    L += ["## 四、趋势解读", ""]
    if new_words:
        L.append(f"- 本窗口出现新热词：{'、'.join(w for w, _, _ in new_words[:5])}。")
    if rising:
        top = rising[0]
        L.append(f"- 飙升最快：『{top[0]}』（增幅 {top[1]:.4f}），关注相关话题扩散。")
    if falling:
        top = falling[0]
        L.append(f"- 明显消退：『{top[0]}』（降幅 {top[1]:.4f}），热度或已转移。")
    if not new_words and not rising and not falling:
        L.append("- 窗口内热词结构稳定，无明显趋势变化。")
    if low_conf:
        L.append(f"- ⚠️ 窗口内缺 {n_missing} 天数据，以上解读置信度有限。")
    L.append("")
    L.append("---")
    L.append(f"*{WATERMARK} ｜ 由 bili-daily weekly_report 生成（TC-20260811-BILI3 V7）*")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print(f"[完成] 周报已写入 {output_path}（窗口 {days_eff} 日，数据 {data_days} 天，"
          f"新词 {len(new_words)} / 飙升 {len(rising)} / 消退 {len(falling)}）")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="B站热门内容周报渲染器")
    ap.add_argument("--corpus", default="corpus.jsonl")
    ap.add_argument("--out", default="weekly.md")
    ap.add_argument("--days", type=int, default=7, choices=[7, 30])
    args = ap.parse_args()
    render_weekly(args.corpus, args.out, days=args.days)
