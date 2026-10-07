#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bilibili-daily-insight: B站每日热门视频字幕抓取（字幕专注版）
=============================================================
只做一件事：抓取 B站每日热门视频的**字幕**（srt/txt/json），
不涉及弹幕、评论、内容分析。

依赖 fetch_subtitles.py 的字幕获取核心（/x/player/wbi/v2 + 登录 cookie）。

技术事实（2026-08 实测）：
  - 字幕列表对游客隐藏，必须带登录态 cookie（SESSDATA）
  - 榜单 popular 游客零签名直取，条目自带 cid/aid
  - subtitle_url 带 auth_key 时效签名，须获取后立即下载

用法：
  python crawl_bilibili_daily.py --ps 20 --cookies cookies.txt --out-dir subs/
"""
import argparse
import json as _json
import os
import sys

import fetch_subtitles as fs

SAFE = "\\/:*?\"<>|"


def main():
    ap = argparse.ArgumentParser(description="B站每日热门视频字幕抓取（字幕专注版）")
    ap.add_argument("--ps", type=int, default=20, help="热门榜条数（默认 20）")
    ap.add_argument("--cookies", required=True, help="Netscape cookies.txt（登录态，必需）")
    ap.add_argument("--out-dir", default="subs", help="字幕输出目录")
    ap.add_argument("--format", choices=["srt", "txt", "json", "all"], default="srt")
    args = ap.parse_args()

    if not os.path.exists(args.cookies):
        print(f"[错误] cookies 文件不存在：{args.cookies}", file=sys.stderr)
        print("  导出方法：浏览器登录 B站（小号）→ 扩展如 Get cookies.txt LOCALLY 导出", file=sys.stderr)
        sys.exit(1)
    cookies = fs.load_cookies(args.cookies)
    if not cookies.get("SESSDATA"):
        print("[警告] cookies 中未找到 SESSDATA（字幕必需）。请确认已登录 B站后导出。", file=sys.stderr)
    os.makedirs(args.out_dir, exist_ok=True)

    print(f"[1/3] 获取热门榜单 ps={args.ps} ...")
    lst = fs.fetch_popular(ps=args.ps)
    if not lst:
        sys.exit(1)
    print(f"[榜单] {len(lst)} 条（请求计数 {fs._request_count}）")

    ok, no_sub, fail = [], [], []
    for i, v in enumerate(lst, 1):
        bvid, cid, aid = v.get("bvid"), v.get("cid"), v.get("aid")
        title = v.get("title", "")
        print(f"  [{i}/{len(lst)}] {title[:32]}（{bvid}）")
        if not cid:
            fail.append((bvid, "无 cid"))
            continue
        tracks = fs.get_subtitle_tracks(bvid, cid, aid, cookies)
        if not tracks:
            no_sub.append((bvid, title))
            print("    无字幕轨（该视频没有 AI 字幕/CC 字幕）")
            continue
        track = next((s for s in tracks if s.get("lan") and "ai" in str(s.get("lan"))), tracks[0])
        print(f"    字幕轨: {track.get('lan_doc', track.get('lan'))} / {track.get('lan')}")
        data = fs.download_subtitle_json(track["subtitle_url"], cookies)
        if not data:
            fail.append((bvid, "字幕下载失败"))
            continue
        body = data.get("body", [])
        if not body:
            no_sub.append((bvid, title))
            continue
        safe = "".join(c if c not in SAFE else "_" for c in title)[:60]
        base = os.path.join(args.out_dir, f"{safe}_{bvid}")
        fmts = {"srt": fs.body_to_srt, "txt": fs.body_to_txt,
                "json": lambda b: _json.dumps(data, ensure_ascii=False, indent=2)}
        if args.format == "all":
            for f, fn in fmts.items():
                with open(f"{base}.{f}", "w", encoding="utf-8") as fh:
                    fh.write(fn(body))
        else:
            with open(f"{base}.{args.format}", "w", encoding="utf-8") as fh:
                fh.write(fmts[args.format](body))
        ok.append((bvid, title, len(body)))
        print(f"    ✓ {len(body)} 条字幕 → {base}.{args.format}")

    print(f"\n[完成] 成功 {len(ok)} / 无字幕轨 {len(no_sub)} / 失败 {len(fail)}"
          f"（请求计数 {fs._request_count}）")
    for bvid, title in no_sub:
        print(f"  无字幕轨: {title[:40]}（{bvid}）")
    for bvid, why in fail:
        print(f"  失败: {bvid}：{why}")
    print(f"输出目录：{os.path.abspath(args.out_dir)}")


if __name__ == "__main__":
    main()
