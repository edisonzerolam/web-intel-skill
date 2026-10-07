#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T1 字幕命中率实测（P0b：接入预算记账 + 完整版回退 + 分批跑）
================================================================

口径（与 transcribe_video.t1_api_subtitle 完全一致）：
  命中 = get_subtitle_tracks 返回非空 且 download_subtitle_json 的正文 ≥50 字

2026-10-07 P0b 改动（三处）：
  1) **接入 req_budget 记账**（合规修复）。此前本脚本直接调 fetch_subtitles，
     未走预算，实测跑 --n 120 实际发出 240+ 次请求，已越过 200 硬停线而未被拦截。
     现在每一次网络请求都 spend；硬停时抛 SystemExit，主循环捕获后**保存已完成的部分**。
  2) **完整版回退**（--full-version-fallback）：未命中原因是「无字幕轨」时，
     按标题检索同内容候选（同作者、时长明显更长、非本片），对其重试 T1。
     依据：channel-notes §3 记录「同一内容常有精简版/完整版两条，精简版无字幕轨」。
  3) **分批跑**：--batch-size 限制单轮样本量；--resume 跳过已测过的 BV。

用法：
  python measure_t1_hit.py --n 30                        # 前 30 个（旧语义）
  python measure_t1_hit.py --n 120 --seed 7 --batch-size 40 --resume
  python measure_t1_hit.py --n 40 --seed 7 --full-version-fallback --batch-size 40

预算：默认日硬停 200（req_budget.HARD）。一次「view + 字幕轨 + 下载」约 3 次请求，
     故单轮 batch-size 建议 ≤50。
"""
import argparse
import json
import os
import random
import re
import sys
import time
from collections import Counter

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SKILL_DIR)

import fetch_subtitles as fs  # noqa: E402
from req_budget import budget  # noqa: E402

VIEW_API = "https://api.bilibili.com/x/web-interface/view"
SEARCH_API = "https://api.bilibili.com/x/web-interface/search/type"
CHANNEL = "t1_measure"
FALLBACK_HINTS = ("完整", "全集", "完整版", "无删减", "full")


def view_info(bvid, cookies):
    """取 title / cid / duration / author。与 transcribe_video.view_info 同口径。"""
    budget.spend(CHANNEL)
    r = fs._session_get(VIEW_API, params={"bvid": bvid}, cookies=cookies)
    if r.status_code in (412, 429):
        budget.block()
        raise SystemExit(f"[风控] HTTP {r.status_code}：已标记当日全停。")
    d = r.json()
    if d.get("code") != 0:
        return None
    data = d.get("data") or {}
    return {
        "title": data.get("title") or bvid,
        "cid": data.get("cid"),
        "duration": int(data.get("duration") or 0),
        "author": ((data.get("owner") or {}).get("name") or ""),
    }


def t1_hit(bvid, cid, cookies):
    """返回 (命中bool, 字数, 语言标签, 说明)。"""
    budget.spend(CHANNEL)
    try:
        tracks = fs.get_subtitle_tracks(bvid, cid, None, cookies)
    except SystemExit:
        raise
    except Exception as e:
        return False, 0, "", f"tracks异常:{str(e)[:60]}"
    if not tracks:
        return False, 0, "", "无字幕轨"
    track = next((s for s in tracks if "ai" in str(s.get("lan", ""))), tracks[0])
    budget.spend(CHANNEL)
    try:
        data = fs.download_subtitle_json(track["subtitle_url"], cookies)
    except Exception as e:
        return False, 0, "", f"下载异常:{str(e)[:60]}"
    if not data or not data.get("body"):
        return False, 0, "", "body为空"
    text = "\n".join((seg.get("content") or "").strip()
                     for seg in data["body"] if seg.get("content"))
    if len(text) < 50:
        return False, len(text), "", f"正文过短({len(text)}字)"
    return True, len(text), str(track.get("lan", "")), "命中"


def t2_hit(bvid, cookies_file):
    """T2：yt-dlp 字幕命中统计（P0b 改法②）。返回 (命中bool, 字数, 语言, 说明)。

    复用 transcribe_video.t2_ytdlp_subtitle 的同一实现，保证口径与生产管道一致。
    yt-dlp 自身会访问 B 站，故同样记 1 次预算。
    """
    budget.spend(CHANNEL)
    try:
        from transcribe_video import t2_ytdlp_subtitle
    except Exception as e:
        return False, 0, "", f"T2导入失败:{str(e)[:40]}"
    try:
        res = t2_ytdlp_subtitle(bvid, cookies_file)
    except Exception as e:
        return False, 0, "", f"T2异常:{str(e)[:40]}"
    if not res:
        return False, 0, "", "T2未命中"
    text, lang = res
    return True, len(text), str(lang), "T2命中"


def _dur_sec(s):
    """'MM:SS' / 'HH:MM:SS' → 秒。"""
    try:
        parts = [int(x) for x in str(s).split(":")]
    except (ValueError, AttributeError):
        return 0
    sec = 0
    for x in parts:
        sec = sec * 60 + x
    return sec


def _title_key(title):
    """标题 → 检索关键词：去掉方括号/标点，取最长 CJK+字母数字 连续段，截 20 字。"""
    cleaned = re.sub(r"[【】\[\]（）()《》<>「」『』|/\\_\-—–:：,，.。!！?？\"'“”‘’~～+*#@$%^&=]+",
                     " ", str(title))
    runs = re.findall(r"[0-9A-Za-z\u4e00-\u9fff]{4,}", cleaned)
    if not runs:
        return str(title)[:20].strip()
    return max(runs, key=len)[:20]


def search_siblings(title, author, cookies, cur_bvid, cur_sec, max_n):
    """按标题检索同内容候选：同作者 + 时长明显更长 + 非本片。

    返回 [{bvid, title, duration_sec, note}]，按时长降序，最多 max_n 个。
    局限（已知）：只取搜索第 1 页；完整版若标题差异过大可能检索不到。
    """
    kw = _title_key(title)
    if not kw:
        return []
    budget.spend(CHANNEL)
    try:
        r = fs._session_get(SEARCH_API, params={
            "search_type": "video", "keyword": kw, "order": "click", "page": 1,
        }, cookies=cookies)
    except SystemExit:
        raise
    except Exception as e:
        print(f"    [回退] 检索异常：{str(e)[:60]}", file=sys.stderr)
        return []
    if r.status_code in (412, 429):
        budget.block()
        raise SystemExit(f"[风控] HTTP {r.status_code}：已标记当日全停。")
    try:
        d = r.json()
    except Exception:
        return []
    if d.get("code") != 0:
        print(f"    [回退] 检索 code={d.get('code')}", file=sys.stderr)
        return []
    out = []
    for v in ((d.get("data") or {}).get("result") or []):
        bv = v.get("bvid")
        if not bv or bv == cur_bvid:
            continue
        if author and v.get("author") != author:
            continue
        sec = _dur_sec(v.get("duration"))
        if sec <= cur_sec + 30:          # 必须明显更长（>30s），否则不是「完整版」
            continue
        t = re.sub(r"<[^>]+>", "", str(v.get("title") or ""))
        out.append({"bvid": bv, "title": t, "duration_sec": sec,
                    "hinted": any(h in t for h in FALLBACK_HINTS)})
    out.sort(key=lambda x: (x["hinted"], x["duration_sec"]), reverse=True)
    return out[:max_n]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=30, help="样本量")
    ap.add_argument("--seed", type=int, default=None, help="随机种子（给定则随机抽样）")
    ap.add_argument("--bvids", help="逗号分隔的指定 BV")
    ap.add_argument("--cookies", default=os.path.join(SKILL_DIR, "cookies.txt"))
    ap.add_argument("--out", default="t1_hit_result.json")
    ap.add_argument("--full-version-fallback", action="store_true",
                    help="无字幕轨时按标题检索完整版候选并重试 T1")
    ap.add_argument("--t2", action="store_true",
                    help="T1 未命中时补测 T2（yt-dlp）字幕命中率（P0b 改法②）")
    ap.add_argument("--fallback-max", type=int, default=2, help="每个未命中最多试几个候选")
    ap.add_argument("--batch-size", type=int, default=0, help="单轮最多测几个（0=不限）")
    ap.add_argument("--resume", action="store_true", help="跳过 --out 里已测过的 BV")
    args = ap.parse_args()

    cookies = fs.load_cookies(args.cookies)
    if not cookies.get("SESSDATA"):
        print("[错误] 无 SESSDATA，T1 必失败", file=sys.stderr)
        sys.exit(1)

    out_path = os.path.join(SKILL_DIR, args.out)
    prev_rows, done = [], set()
    if args.resume and os.path.exists(out_path):
        try:
            prev = json.load(open(out_path, encoding="utf-8"))
            prev_rows = prev.get("rows") or []
            done = {r["bvid"] for r in prev_rows}
        except Exception as e:
            print(f"[警告] --resume 读取失败，忽略：{e}", file=sys.stderr)

    if args.bvids:
        bvids = [b.strip() for b in args.bvids.split(",") if b.strip()]
    else:
        pool = list(json.load(open(os.path.join(SKILL_DIR, "seen_bvids.json"),
                                   encoding="utf-8")).keys())
        if args.seed is not None:
            random.seed(args.seed)
            bvids = random.sample(pool, min(args.n, len(pool)))
        else:
            bvids = pool[:args.n]

    if done:
        bvids = [b for b in bvids if b not in done]
    if args.batch_size and len(bvids) > args.batch_size:
        bvids = bvids[:args.batch_size]

    print(f"本轮样本 {len(bvids)} 个（预算 {budget.summary()}），"
          f"完整版回退={'开' if args.full_version_fallback else '关'}，开始实测...\n",
          file=sys.stderr)

    rows, hit, hit_pre = [], 0, 0
    hit_t2 = 0
    fb_tried = fb_saved = 0
    stopped = False
    for i, bvid in enumerate(bvids, 1):
        t0 = time.time()
        try:
            vi = view_info(bvid, cookies)
        except SystemExit as e:
            print(f"\n{e}", file=sys.stderr)
            stopped = True
            break
        if not vi or not vi.get("cid"):
            rows.append({"bvid": bvid, "hit": False, "chars": 0, "lang": "",
                         "note": "view失败/无cid", "title": ""})
            print(f"[{i}/{len(bvids)}] {bvid}  ❌ view失败", file=sys.stderr)
            continue

        try:
            ok, n, lang, note = t1_hit(bvid, vi["cid"], cookies)
        except SystemExit as e:
            print(f"\n{e}", file=sys.stderr)
            stopped = True
            break
        hit_pre += bool(ok)
        row = {"bvid": bvid, "hit": ok, "chars": n, "lang": lang,
               "note": note, "title": vi["title"][:50],
               "duration": vi["duration"], "author": vi["author"]}

        # ---- T2（yt-dlp）兜底统计：P0b 改法② ----
        if not ok and args.t2:
            t2_ok, t2_n, t2_lang, t2_note = t2_hit(bvid, args.cookies)
            row["t2_hit"], row["t2_chars"], row["t2_note"] = t2_ok, t2_n, t2_note
            if t2_ok:
                hit_t2 += 1
                row.update({"hit": True, "chars": t2_n, "lang": t2_lang,
                            "note": "T2命中"})

        # ---- 完整版回退：T2 也未命中且原因为「无字幕轨」时才试 ----
        if not row["hit"] and args.full_version_fallback and note == "无字幕轨":
            fb_tried += 1
            try:
                cands = search_siblings(vi["title"], vi["author"], cookies,
                                        bvid, vi["duration"], args.fallback_max)
            except SystemExit as e:
                print(f"\n{e}", file=sys.stderr)
                stopped = True
                rows.append(row)
                break
            for c in cands:
                try:
                    vi2 = view_info(c["bvid"], cookies)
                    if not vi2 or not vi2.get("cid"):
                        continue
                    ok2, n2, lang2, note2 = t1_hit(c["bvid"], vi2["cid"], cookies)
                except SystemExit as e:
                    print(f"\n{e}", file=sys.stderr)
                    stopped = True
                    break
                if ok2:
                    row.update({"hit": True, "chars": n2, "lang": lang2,
                                "note": "完整版回退命中", "fallback_bvid": c["bvid"],
                                "fallback_title": c["title"][:60],
                                "fallback_note": note2,
                                "fallback_duration": c["duration_sec"]})
                    fb_saved += 1
                    break
            if not row["hit"]:
                row["fallback_candidates"] = len(cands)
            if stopped:
                rows.append(row)
                break

        hit += bool(row["hit"])
        rows.append(row)
        flag = "✅" if row["hit"] else "❌"
        extra = f" ← {row['note']}" if row.get("fallback_bvid") else ""
        print(f"[{i}/{len(bvids)}] {flag} {bvid} {row['lang'] or '-':6s} "
              f"{row['chars']:6d}字 {row['note']:14s} {vi['title'][:26]}"
              f" ({time.time()-t0:.1f}s){extra}", file=sys.stderr)

    all_rows = prev_rows + rows
    total = len(all_rows)
    hit_all = sum(1 for r in all_rows if r["hit"])
    print(f"\n{'='*62}", file=sys.stderr)
    if stopped:
        print("⚠️ 本轮因预算/风控提前停止，已保存部分结果。", file=sys.stderr)
    print(f"本轮：命中 {hit}/{len(rows)}"
          f"（T1 {hit_pre}，T2 救回 {hit_t2}，完整版回退救回 {fb_saved}）",
          file=sys.stderr)
    t1_only = sum(1 for r in all_rows if r.get("note") != "T2命中"
                  and not r.get("fallback_bvid") and r["hit"])
    print(f"累计：命中 {hit_all}/{total} = {100*hit_all/total:.1f}%"
          f"（仅 T1：{t1_only}/{total} = {100*t1_only/total:.1f}%）" if total else "无样本",
          file=sys.stderr)
    miss = Counter(r["note"] for r in all_rows if not r["hit"])
    if miss:
        print("未命中原因分布（累计）：", file=sys.stderr)
        for k, v in miss.most_common():
            print(f"  {k}: {v}", file=sys.stderr)
    hitlangs = Counter(r["lang"] for r in all_rows if r["hit"])
    if hitlangs:
        print("命中语言分布（累计）：", file=sys.stderr)
        for k, v in hitlangs.most_common():
            print(f"  {k}: {v}", file=sys.stderr)
    print(f"预算：{budget.summary()}", file=sys.stderr)
    print(f"{'='*62}", file=sys.stderr)

    json.dump({"total": total, "hit": hit_all,
               "rate": hit_all / total if total else 0,
               "t1_only": t1_only, "t1_rate": t1_only / total if total else 0,
               "t2_enabled": bool(args.t2), "t2_saved": hit_t2,
               "fallback_enabled": bool(args.full_version_fallback),
               "fallback_tried": fb_tried, "fallback_saved": fb_saved,
               "rows": all_rows},
              open(out_path, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(f"明细已存：{out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
