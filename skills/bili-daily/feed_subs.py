#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
feed_subs: 关注动态流订阅（当天有更新的都关注，知识区重点）
=============================================================
基于关注动态 feed 接口（实测可用）：1 请求/页（~20 条/页）覆盖全部关注 UP 的最新投稿，
替代逐 UP 搜索（1103 请求不可行）。

流程：
  1. 拉关注动态 feed（默认 3 页，按 pub_ts 过滤当天）
  2. 过滤视频投稿（major.archive）→ 与 seen_bvids 去重（已见 pass）
  3. 知识区重点：本地 sign 分类（BILI4 词库，0 请求）标记 UP 题材 == 知识 → 优先排序
  4. 对知识区新视频 + 其余前 N 个抓字幕（复用 fetch_subtitles 链路）
  5. 输出当日新投稿清单（Markdown）+ corpus 追加

合规：up_mid 仅内存匹配 subs.json 用，不落盘；bvid/title 即用即删（仅卡片/corpus 派生字段）；
预算：feed ≤3 请求 + 字幕 ≤40 请求（知识区优先分配）。

用法：
  python feed_subs.py --cookies cookies.txt --out-dir out/
"""
import argparse
import json
import os
import sys
from collections import Counter
from datetime import date, datetime

import fetch_subtitles as fs

FEED_API = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/all"
PAGES = 3          # feed 页数（每页 ~20 条）
MAX_SUBS = 40      # 新视频字幕上限（知识区优先）
TOPIC_LEXICON = None  # 懒加载（BILI4 词库）


def parse_count(s):
    """'8.8万' → 88000；'3437' → 3437。"""
    s = str(s or "").strip()
    if not s:
        return 0
    if s.endswith("万"):
        try:
            return int(float(s[:-1]) * 10000)
        except ValueError:
            return 0
    try:
        return int(s)
    except ValueError:
        return 0


def fetch_feed(cookies, pages=PAGES):
    """拉关注动态流，返回 [{bvid, title, up_name, up_mid, pub_ts, play, danmaku}]（视频投稿）。"""
    vids, offset, cursor = [], None, None
    for _ in range(pages):
        params = {"type": "video", "page": 1, "features": "itemOpusStyle"}
        if offset:
            params["offset"] = offset
        r = fs._session_get(FEED_API, params=params, cookies=cookies)
        if r.status_code in (412, 429):
            raise SystemExit(f"[风控] HTTP {r.status_code}：当日停止订阅 feed。")
        d = r.json()
        if d.get("code") != 0:
            break
        data = d.get("data") or {}
        items = data.get("items") or []
        for it in items:
            if it.get("type") != "DYNAMIC_TYPE_AV":
                continue
            mod = it.get("modules", {})
            author = (mod.get("module_author") or {})
            archive = ((mod.get("module_dynamic") or {}).get("major") or {}).get("archive") or {}
            bvid = archive.get("bvid")
            if not bvid:
                continue
            stat = archive.get("stat") or {}
            vids.append({
                "bvid": bvid, "title": archive.get("title", ""),
                "up_name": author.get("name", ""), "up_mid": author.get("mid"),
                "pub_ts": it.get("pub_ts", 0),
                "play": parse_count(stat.get("play")), "danmaku": parse_count(stat.get("danmaku")),
            })
        offset = data.get("offset")
        if not data.get("has_more"):
            break
    return vids


def load_knowledge_ups():
    """本地 sign 分类（BILI4 词库）→ 知识区 UP mid 集合（0 请求）。"""
    global TOPIC_LEXICON
    if TOPIC_LEXICON is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("au", os.path.join(os.path.dirname(os.path.abspath(__file__)), "analyze_ups.py"))
        au = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(au)
            TOPIC_LEXICON = au.TOPIC_LEXICON
        except Exception:
            TOPIC_LEXICON = {}
    k = set()
    cache = "_followings_cache.json"
    if os.path.exists(cache):
        for f in json.load(open(cache, encoding="utf-8")):
            sign = f.get("sign") or ""
            low = sign.lower()
            sc = sum(2 if w.lower() in low else 0 for w in TOPIC_LEXICON.get("知识", {}).get("强", []))
            sc += sum(1 if w.lower() in low else 0 for w in TOPIC_LEXICON.get("知识", {}).get("弱", []))
            if sc >= 2:
                k.add(f.get("mid"))
    return k


def run_feed(cookies, out_dir="out", pages=PAGES, max_subs=MAX_SUBS):
    """供 bili_daily_v2 --feed 集成调用：返回 (feed_cards, new_vids, k_count)。"""
    os.makedirs(out_dir, exist_ok=True)
    seen = {}
    if os.path.exists("seen_bvids.json"):
        seen = json.load(open("seen_bvids.json", encoding="utf-8"))
    today = date.today().isoformat()
    vids = fetch_feed(cookies, pages=pages)
    new_vids = []
    for v in vids:
        if v["bvid"] in seen:
            continue
        pub_d = datetime.fromtimestamp(v["pub_ts"]).date().isoformat() if v["pub_ts"] else today
        if pub_d == today or v["pub_ts"] == 0:
            new_vids.append(v)
    knowledge = load_knowledge_ups()
    for v in new_vids:
        v["knowledge"] = v["up_mid"] in knowledge
    new_vids.sort(key=lambda x: (not x["knowledge"], -x["play"]))
    k_count = sum(1 for v in new_vids if v["knowledge"])
    cards = _deep_process([v for v in new_vids if v["knowledge"]][:max_subs], cookies)
    for v in new_vids:
        seen.setdefault(v["bvid"], today)
    json.dump(seen, open("seen_bvids.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return cards, new_vids, k_count


def _deep_process(targets, cookies):
    """知识区字幕深度处理（补 cid + 字幕 + 内容理解）。"""
    import subs as subs_mod
    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location("v2", os.path.join(os.path.dirname(os.path.abspath(__file__)), "bili_daily_v2.py"))
    v2 = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(v2)
    cards, corpus_lines = [], []
    for v in targets:
        cid = subs_mod.get_video_cid(v["bvid"], cookies)
        if not cid:
            continue
        tracks = fs.get_subtitle_tracks(v["bvid"], cid, None, cookies)
        if not tracks:
            continue
        track = next((s for s in tracks if s.get("lan") and "ai" in str(s.get("lan"))), tracks[0])
        data = fs.download_subtitle_json(track["subtitle_url"], cookies)
        if not data or not data.get("body"):
            continue
        merged = v2.merge_dups(data["body"])
        segs = v2.segment_video(merged)
        summary = v2.summarize_video(v["title"], "", merged, segs)
        pts = v2.key_points(segs)
        senti = v2.sentiment_of(merged)
        toks = [t for s in merged for t in v2.tokenize(s["content"])]
        cards.append({"bvid": v["bvid"], "title": v["title"][:50], "up": v["up_name"],
                      "tag": "知识区", "play": v["play"], "summary": summary,
                      "points": pts, "sentiment": senti})
        corpus_lines.append(json.dumps({
            "date": date.today().isoformat(), "bvid": v["bvid"], "partition": "关注动态",
            "title": v["title"], "src": "feed",
            "keywords": [w for w, _ in Counter(toks).most_common(10)],
            "summary": summary, "sentiment": senti,
            "seg_ts": [{"from": s[0]["from"], "to": s[-1]["to"]} for s in segs],
            "up_name": v["up_name"],
        }, ensure_ascii=False))
        print(f"  [知识区] {v['title'][:28]} ✓ {len(merged)} 条字幕")
    if corpus_lines:
        with open("corpus.jsonl", "a", encoding="utf-8") as f:
            f.write("
".join(corpus_lines) + "
")


def main():
    ap = argparse.ArgumentParser(description="关注动态流订阅")
    ap.add_argument("--cookies", required=True)
    ap.add_argument("--out-dir", default="out")
    ap.add_argument("--pages", type=int, default=PAGES)
    ap.add_argument("--max-subs", type=int, default=MAX_SUBS)
    args = ap.parse_args()
    cookies = fs.load_cookies(args.cookies)
    os.makedirs(args.out_dir, exist_ok=True)
    seen = {}
    if os.path.exists("seen_bvids.json"):
        seen = json.load(open("seen_bvids.json", encoding="utf-8"))
    today = date.today().isoformat()

    print(f"[1/4] 拉取关注动态 feed（{args.pages} 页）...")
    vids = fetch_feed(cookies, pages=args.pages)
    print(f"[feed] {len(vids)} 条视频投稿")

    # 当天过滤 + 去重
    new_vids = []
    for v in vids:
        if v["bvid"] in seen:
            continue
        pub_d = datetime.fromtimestamp(v["pub_ts"]).date().isoformat() if v["pub_ts"] else today
        if pub_d == today or v["pub_ts"] == 0:
            new_vids.append(v)
    print(f"[去重] 当天新投稿 {len(new_vids)}（已见 {len(vids) - len(new_vids)} pass）")

    # 知识区重点
    knowledge = load_knowledge_ups()
    for v in new_vids:
        v["knowledge"] = v["up_mid"] in knowledge
    new_vids.sort(key=lambda x: (not x["knowledge"], -x["play"]))
    k_count = sum(1 for v in new_vids if v["knowledge"])
    print(f"[知识区] {k_count} 个知识区 UP 投稿（优先处理）")

    # 抓字幕：知识区全部深度处理（补 cid + 字幕 + 内容理解），其余仅清单可见
    import subs as subs_mod
    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location("v2", os.path.join(os.path.dirname(os.path.abspath(__file__)), "bili_daily_v2.py"))
    v2 = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(v2)
    cards, corpus_lines = [], []
    sub_ok = 0
    targets = [v for v in new_vids if v["knowledge"]][:args.max_subs]
    for i, v in enumerate(targets, 1):
        cid = subs_mod.get_video_cid(v["bvid"], cookies)
        if not cid:
            continue
        tracks = fs.get_subtitle_tracks(v["bvid"], cid, None, cookies)
        if not tracks:
            continue
        track = next((s for s in tracks if s.get("lan") and "ai" in str(s.get("lan"))), tracks[0])
        data = fs.download_subtitle_json(track["subtitle_url"], cookies)
        if not data or not data.get("body"):
            continue
        sub_ok += 1
        # 内容理解（复用 bili_daily_v2 管线：合并/分段/梗概/要点/情感）
        merged = v2.merge_dups(data["body"])
        segs = v2.segment_video(merged)
        summary = v2.summarize_video(v["title"], "", merged, segs)
        pts = v2.key_points(segs)
        senti = v2.sentiment_of(merged)
        toks = [t for s in merged for t in v2.tokenize(s["content"])]
        cards.append({"bvid": v["bvid"], "title": v["title"][:50], "up": v["up_name"],
                      "tag": "知识区", "play": v["play"], "summary": summary,
                      "points": pts, "sentiment": senti})
        corpus_lines.append(json.dumps({
            "date": today, "bvid": v["bvid"], "partition": "关注动态", "title": v["title"],
            "src": "feed", "keywords": [w for w, _ in Counter(toks).most_common(10)],
            "summary": summary, "sentiment": senti,
            "seg_ts": [{"from": s[0]["from"], "to": s[-1]["to"]} for s in segs],
            "up_name": v["up_name"],
        }, ensure_ascii=False))
        print(f"  [{i}/{len(targets)}] [知识区] {v['title'][:28]} ✓ {len(merged)} 条字幕")

    # seen 更新 + corpus 追加
    for v in new_vids:
        seen.setdefault(v["bvid"], today)
    json.dump(seen, open("seen_bvids.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    if corpus_lines:
        with open("corpus.jsonl", "a", encoding="utf-8") as f:
        f.write("
".join(corpus_lines) + "
")

    # 输出当日新投稿清单 + 知识区内容理解卡片
    L = [f"# 关注动态订阅 · {today}", "",
         f"> 个人学习研究·勿公开传播 ｜ 来源：关注动态 feed（{len(vids)} 条，新 {len(new_vids)}，已见 pass {len(vids) - len(new_vids)}）",
         f"> 知识区 UP：{k_count} 个 ｜ 字幕理解：{sub_ok} 个", "",
         "## 当日新投稿（知识区优先）", "",
         "| # | 类型 | UP | 标题 | 播放 |", "|---|------|----|------|------|"]
    for i, v in enumerate(new_vids[:50], 1):
        L.append(f"| {i} | {'⭐知识' if v['knowledge'] else '关注'} | {v['up_name'][:12]} | {v['title'][:36]} | {v['play']:,} |")
    L += ["", "## 知识区内容理解（字幕深度处理）", ""]
    for c in cards:
        L += [f"### {c['title'][:36]}", f"- `{c['bvid']}` ｜ {c['up']} ｜ 播放 {c['play']:,} ｜ 情感：{c['sentiment']}",
              f"- 一句话梗概：{c['summary']}", "- 要点："]
        for p in c["points"]:
            L.append(f"  - [{p['ts_from']:.0f}s-{p['ts_to']:.0f}s] {p['point']}")
        L.append("")
    L += ["---", "*个人学习研究·勿公开传播 ｜ feed_subs（TC-20260811-BILI6）*", ""]
    out = os.path.join(args.out_dir, f"feed_{today}.md")
    open(out, "w", encoding="utf-8").write("\n".join(L))
    print(f"\n[完成] 清单：{out} ｜ 新投稿 {len(new_vids)}（知识区 {k_count}）｜ 字幕理解 {sub_ok}")


if __name__ == "__main__":
    main()
