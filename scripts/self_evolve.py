#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
web-intel 自进化引擎 —— 记录通道选择结果，有界重排，输出改进候选。

设计原则（与 skill-use-router/learn.py 对齐，避免重复造轮子）：
  * 分工：learn.py 学「该用哪个技能」；本脚本学「web-intel 内部该走哪条通道」。
  * 有界：调整幅度 ±30% 封顶，永不覆盖显式点名，永不自动改 SKILL.md。
  * 衰减：14 天半衰期，老经验自然淡出。
  * 可审可撤：report / prune / reset 一键纠偏，不静默漂移。
  * 编码：全部 UTF-8 无 BOM 显式读写。

存储：.state/web-intel-evolve.jsonl（事件流）+ .state/evolve_stats.json（聚合）
"""

import argparse
import json
import math
import os
import sys
from datetime import datetime, timezone

# 本机 locale=cp936，裸 print 非 ASCII 会炸；强制 stdout/stderr 走 UTF-8
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

# 标记一律 ASCII，避免任何终端编码问题
OK_MARK = "[OK]"
FAIL_MARK = "[FAIL]"

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(HERE)
STATE_DIR = os.path.join(SKILL_DIR, ".state")
EVENTS = os.path.join(STATE_DIR, "web-intel-evolve.jsonl")
STATS = os.path.join(STATE_DIR, "evolve_stats.json")

HALF_LIFE_DAYS = 14.0
MAX_ADJUST = 0.30          # 有界重排上限 ±30%
VALID_OUTCOMES = ("ok", "wrong-route", "fetch-failed", "source-rejected")

# outcome -> 单条事件贡献值（-1 .. +1）
WEIGHTS = {
    "ok": 1.0,
    "wrong-route": -1.0,
    "fetch-failed": -0.6,
    "source-rejected": -0.8,
}

KNOWN_SUBSKILLS = (
    "anysearch", "browser-cdp", "baoyu-url-to-markdown",
    "baoyu-youtube-transcript", "bili-daily",
    "deep-research", "deep-research-pipeline",
)


# ---------- 存储层 ----------

def _ensure_state():
    os.makedirs(STATE_DIR, exist_ok=True)
    for p in (EVENTS, STATS):
        if not os.path.exists(p):
            with open(p, "w", encoding="utf-8") as f:
                if p == STATS:
                    json.dump({"version": 1, "skills": {}}, f, ensure_ascii=False, indent=2)
                else:
                    f.write("")


def read_events():
    if not os.path.exists(EVENTS):
        return []
    out = []
    with open(EVENTS, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # 坏行跳过，不炸整体
    return out


def write_event(ev):
    with open(EVENTS, "a", encoding="utf-8") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")


def write_stats(stats):
    with open(STATS, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)


def read_stats():
    if not os.path.exists(STATS):
        return {"version": 1, "skills": {}}
    try:
        with open(STATS, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"version": 1, "skills": {}}


# ---------- 学习算法 ----------

def decay(ts_iso, now=None):
    """14 天半衰期时间衰减因子 ∈ (0, 1]。"""
    now = now or datetime.now(timezone.utc)
    try:
        then = datetime.fromisoformat(ts_iso)
    except (ValueError, TypeError):
        return 1.0
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    days = max(0.0, (now - then).total_seconds() / 86400.0)
    return math.pow(0.5, days / HALF_LIFE_DAYS)


def compute_scores(events, now=None):
    """返回 {subskill: {score, n, weighted, breakdown}}，score ∈ (-1, 1)。"""
    acc = {}
    for ev in events:
        sk = ev.get("subskill", "")
        oc = ev.get("outcome", "")
        if not sk or oc not in WEIGHTS:
            continue
        d = decay(ev.get("ts", ""), now)
        w = WEIGHTS[oc] * d
        a = acc.setdefault(sk, {"weighted": 0.0, "abs": 0.0, "n": 0, "outcomes": {}})
        a["weighted"] += w
        a["abs"] += abs(w)
        a["n"] += 1
        a["outcomes"][oc] = a["outcomes"].get(oc, 0) + 1

    out = {}
    for sk, a in acc.items():
        raw = a["weighted"] / a["abs"] if a["abs"] > 0 else 0.0
        out[sk] = {
            "score": round(max(-1.0, min(1.0, raw)), 4),
            "adjust": round(max(-MAX_ADJUST, min(MAX_ADJUST, raw * MAX_ADJUST)), 4),
            "raw_weighted": round(a["weighted"], 6),
            "n": a["n"],
            "outcomes": a["outcomes"],
        }
    return out


# ---------- 命令 ----------

def cmd_log(args):
    _ensure_state()
    if args.outcome not in VALID_OUTCOMES:
        print("错误：outcome 必须是 %s 之一" % ", ".join(VALID_OUTCOMES))
        return 2
    if args.subskill not in KNOWN_SUBSKILLS:
        print("警告：%s 不在已知子技能列表内（%s），仍已记录。"
              % (args.subskill, "/".join(KNOWN_SUBSKILLS)))
    ev = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "intent": (args.intent or "")[:200],
        "subskill": args.subskill,
        "outcome": args.outcome,
        "note": (args.note or "")[:300],
    }
    write_event(ev)
    print("已记录：%s <- %s (%s)" % (args.subskill, args.outcome, ev["ts"]))
    return 0


def cmd_report(args):
    _ensure_state()
    events = read_events()
    if not events:
        print("尚无事件。先跑 log 记一条。")
        return 0
    scores = compute_scores(events)
    print("== web-intel 通道学习（%d 条事件，14 天半衰期）==" % len(events))
    print("%-26s %8s %6s %6s  %s" % ("子技能", "score", "调整", "样本", "结果分布"))
    for sk in sorted(scores, key=lambda s: -scores[s]["score"]):
        s = scores[sk]
        dist = " ".join("%s×%d" % (k, v) for k, v in sorted(s["outcomes"].items()))
        print("%-26s %8.3f %+6.1f%% %6d  %s"
              % (sk, s["score"], s["adjust"] * 100, s["n"], dist))
    neg = [sk for sk in scores if scores[sk]["score"] < -0.3 and scores[sk]["n"] >= 3]
    if neg:
        print("\n⚠ 持续负面（score<-0.3 且样本≥3）：%s" % ", ".join(neg))
    return 0


def cmd_rerank(args):
    """给定候选子技能列表，输出有界重排后的顺序。"""
    _ensure_state()
    scores = compute_scores(read_events())
    cands = [c.strip() for c in args.candidates.split(",") if c.strip()]
    ranked = sorted(cands, key=lambda c: -scores.get(c, {}).get("adjust", 0.0))
    print("原始： %s" % " > ".join(cands))
    print("重排： %s" % " > ".join(ranked))
    for c in ranked:
        s = scores.get(c)
        if s:
            print("  %-26s %+6.1f%% (n=%d)" % (c, s["adjust"] * 100, s["n"]))
    return 0


def cmd_suggest(args):
    """输出改进候选——只提示，不改文件。落地须人工。"""
    _ensure_state()
    events = read_events()
    scores = compute_scores(events)
    sug = []

    for sk, s in sorted(scores.items(), key=lambda kv: kv[1]["score"]):
        o = s["outcomes"]
        if s["score"] < -0.3 and s["n"] >= 3:
            sug.append("[高] %s 持续负面（score=%.2f, n=%d）→ 复核其触发条件或降级为兜底通道"
                       % (sk, s["score"], s["n"]))
        if o.get("fetch-failed", 0) >= 3:
            sug.append("[中] %s 抓取失败 %d 次 → 检查其后端可用性/凭据，或把兜底通道提前"
                       % (sk, o["fetch-failed"]))
        if o.get("source-rejected", 0) >= 3:
            sug.append("[中] %s 来源被否 %d 次 → 收紧其派单白名单，补锚材料要求"
                       % (sk, o["source-rejected"]))
        if o.get("wrong-route", 0) >= 2:
            sug.append("[中] %s 误路由 %d 次 → 在 SKILL.md 路由表补消歧说明"
                       % (sk, o["wrong-route"]))

    # 从未被记过的子技能：不是问题，只是无数据
    cold = [k for k in KNOWN_SUBSKILLS if k not in scores]
    if cold:
        sug.append("[低] 尚无数据的子技能：%s（非缺陷，仅样本不足）" % ", ".join(cold))

    if not sug:
        print("无改进候选。继续积累事件即可。")
        return 0
    print("== 改进候选（须人工确认后才落地，本脚本不自动改文件）==")
    for i, s in enumerate(sug, 1):
        print("%2d. %s" % (i, s))
    return 0


def cmd_prune(args):
    _ensure_state()
    events = read_events()
    kept = [e for e in events if e.get("subskill") != args.subskill]
    removed = len(events) - len(kept)
    with open(EVENTS, "w", encoding="utf-8") as f:
        for e in kept:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    print("已剔除 %s 的 %d 条事件（剩 %d）。" % (args.subskill, removed, len(kept)))
    return 0


def cmd_reset(args):
    _ensure_state()
    with open(EVENTS, "w", encoding="utf-8") as f:
        f.write("")
    write_stats({"version": 1, "skills": {}})
    print("已清空全部自进化数据。")
    return 0


def cmd_doctor(args):
    _ensure_state()
    ok = True
    events = read_events()
    print("状态目录      : %s" % STATE_DIR)
    print("事件文件      : %s (%d 条)" % (EVENTS, len(events)))
    bad = 0
    for e in events:
        if e.get("outcome") not in VALID_OUTCOMES or not e.get("subskill"):
            bad += 1
    print("坏事件        : %d" % bad)
    if bad:
        ok = False
    # 编码自检：无 BOM
    for p in (EVENTS, STATS):
        if os.path.exists(p):
            with open(p, "rb") as f:
                head = f.read(3)
            if head == b"\xef\xbb\xbf":
                print("编码          : %s 含 BOM %s" % (os.path.basename(p), FAIL_MARK))
                ok = False
    print(("编码          : UTF-8 无 BOM " + OK_MARK) if ok
          else ("编码          : 存在问题 " + FAIL_MARK))
    scores = compute_scores(events)
    print("已学到子技能  : %d" % len(scores))
    print("结果          : %s" % ("OK" if ok else "有问题"))
    return 0 if ok else 1


def cmd_selftest(args):
    """红/绿自检：断言算法行为，不依赖外部网络。"""
    _ensure_state()
    fails = []

    def check(name, cond, detail=""):
        if cond:
            print("  %s %s" % (OK_MARK, name))
        else:
            print("  %s %s  %s" % (FAIL_MARK, name, detail))
            fails.append(name)

    now = datetime(2026, 10, 2, tzinfo=timezone.utc)

    # 1. 衰减：0 天 = 1.0
    check("decay(今天)=1.0", abs(decay("2026-10-02T00:00:00+00:00", now) - 1.0) < 0.01)
    # 2. 衰减：14 天 ≈ 0.5
    d14 = decay("2026-09-18T00:00:00+00:00", now)
    check("decay(14天前)≈0.5", abs(d14 - 0.5) < 0.02, "实得 %.4f" % d14)
    # 3. 全 ok -> score = +1
    evs = [{"ts": "2026-10-02T00:00:00+00:00", "subskill": "anysearch",
            "outcome": "ok"}] * 5
    s = compute_scores(evs, now)
    check("全 ok → score=+1", abs(s["anysearch"]["score"] - 1.0) < 0.01,
          "实得 %s" % s["anysearch"]["score"])
    # 4. 全 wrong-route -> score = -1
    evs = [{"ts": "2026-10-02T00:00:00+00:00", "subskill": "browser-cdp",
            "outcome": "wrong-route"}] * 5
    s = compute_scores(evs, now)
    check("全 wrong-route → score=-1", abs(s["browser-cdp"]["score"] + 1.0) < 0.01,
          "实得 %s" % s["browser-cdp"]["score"])
    # 5. 有界：调整幅度不超过 ±30%
    evs = [{"ts": "2026-10-02T00:00:00+00:00", "subskill": "bili-daily",
            "outcome": "ok"}] * 50
    s = compute_scores(evs, now)
    check("调整有界 ≤30%", abs(s["bili-daily"]["adjust"]) <= MAX_ADJUST + 1e-9,
          "实得 %s" % s["bili-daily"]["adjust"])
    # 6. 混合：ok 占优 -> score > 0
    evs = ([{"ts": "2026-10-02T00:00:00+00:00", "subskill": "x", "outcome": "ok"}] * 3
           + [{"ts": "2026-10-02T00:00:00+00:00", "subskill": "x", "outcome": "source-rejected"}] * 1)
    s = compute_scores(evs, now)
    check("3 ok + 1 rejected → score>0", s["x"]["score"] > 0, "实得 %s" % s["x"]["score"])
    # 7. 老事件加权值低于新事件（衰减真的生效）
    #    注意：单样本归一化后 score 恒为 ±1，必须比较未归一化的 raw_weighted，
    #    否则这条断言是同义反复（永远为真，测不出任何东西）。
    old = compute_scores([{"ts": "2025-01-01T00:00:00+00:00", "subskill": "y",
                           "outcome": "wrong-route"}], now)
    new = compute_scores([{"ts": "2026-10-02T00:00:00+00:00", "subskill": "y",
                           "outcome": "wrong-route"}], now)
    check("老事件被衰减(raw_weighted)", abs(old["y"]["raw_weighted"]) < abs(new["y"]["raw_weighted"]),
          "old=%s new=%s" % (old["y"]["raw_weighted"], new["y"]["raw_weighted"]))
    # 7b. 衰减后老事件几乎不剩权重（2025-01-01 距 2026-10-02 约 640 天 >> 14 天半衰期）
    check("老事件衰减至近零", abs(old["y"]["raw_weighted"]) < 0.01,
          "实得 %s" % old["y"]["raw_weighted"])
    # 8. 非法 outcome 被忽略
    s = compute_scores([{"ts": "2026-10-02T00:00:00+00:00", "subskill": "z",
                         "outcome": "bogus"}], now)
    check("非法 outcome 被忽略", "z" not in s)
    # 9. 空事件不炸
    check("空事件安全", compute_scores([], now) == {})

    print("\n自检结果：%s" % ("全绿" if not fails else "有 %d 项红" % len(fails)))
    return 0 if not fails else 1


# ---------- CLI ----------

def main():
    ap = argparse.ArgumentParser(
        prog="self_evolve",
        description="web-intel 自进化引擎：通道选择日志 → 有界重排 → 改进候选")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("log", help="记一条通道选择结果")
    p.add_argument("--intent", required=True, help="老板原话短语（脱敏）")
    p.add_argument("--subskill", required=True, help="实际走的子技能名")
    p.add_argument("--outcome", required=True, choices=VALID_OUTCOMES)
    p.add_argument("--note", default="", help="一句话备注")
    p.set_defaults(func=cmd_log)

    p = sub.add_parser("report", help="查看学到了什么")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("rerank", help="对候选子技能做有界重排")
    p.add_argument("--candidates", required=True, help="逗号分隔，如 anysearch,browser-cdp")
    p.set_defaults(func=cmd_rerank)

    p = sub.add_parser("suggest", help="输出改进候选（不自动改文件）")
    p.set_defaults(func=cmd_suggest)

    p = sub.add_parser("prune", help="剔除某子技能的学习数据")
    p.add_argument("subskill")
    p.set_defaults(func=cmd_prune)

    p = sub.add_parser("reset", help="清空全部学习数据")
    p.set_defaults(func=cmd_reset)

    p = sub.add_parser("doctor", help="体检（文件/编码/坏事件）")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("selftest", help="红/绿自检算法行为")
    p.set_defaults(func=cmd_selftest)

    args = ap.parse_args()
    sys.exit(args.func(args) or 0)


if __name__ == "__main__":
    main()
