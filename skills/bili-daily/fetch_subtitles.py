#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bilibili-subtitle-fetch: B站视频字幕抓取（专注版）
==================================================
只做一件事：获取 B站视频的**字幕**（srt/txt/json），不涉及弹幕、评论、内容分析。

技术事实（2026-08 实测）：
  - 字幕列表接口 /x/player/wbi/v2 对**游客隐藏**（need_login_subtitle），
    必须携带登录态 cookie（SESSDATA）才能拿到字幕轨列表
  - subtitle_url 带 auth_key 时效签名，须获取后立即下载（过期 403）
  - 榜单 popular 接口游客零签名直取，条目自带 cid/aid

用法：
  # 1. 先准备 cookies.txt（Netscape 格式，从浏览器导出，建议小号）
  # 2. 抓取每日热门榜前 20 的视频字幕：
  python fetch_subtitles.py --ps 20 --cookies cookies.txt --out-dir subs/

  # 或抓取指定视频：
  python fetch_subtitles.py --bvid BV1xx411c7mD --cookies cookies.txt --out-dir subs/

合规约束（个人研究）：
  - 登录态须使用自有小号，低频串行，遇 412/429 当日停止
  - 字幕仅个人学习使用，不发布不商用；原始数据用后即删
  - 不采集/不存储 uid 等个人信息
"""
import argparse
import json
import os
import re
import sys
import threading
import time

# 本机 locale=cp936：重配置 stdout/stderr 为 UTF-8，避免 emoji/中文打印 GBK 崩溃
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Referer": "https://www.bilibili.com/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
}
POPULAR_API = "https://api.bilibili.com/x/web-interface/popular"
PLAYER_API = "https://api.bilibili.com/x/player/wbi/v2"

# 合规参数（保守默认值，无官方依据，遇风控立即降速）
MIN_INTERVAL = 1.5   # 请求最小间隔（秒）
_request_count = 0
_last_request_at = 0.0
_pace_lock = threading.Lock()   # 并发下保证全局最小间隔（见 pace() 注释）


def pace():
    """合规限速（BILI7 落地）：1.5-2.5s 均匀抖动 + 每 10 请求插 5-10s 冷却（防周期命中滑动窗口）。

    2026-10-07：加互斥锁。批量并行转录（transcribe_video --t3-workers）下会有多个
    线程同时调 pace()；无锁时各线程都先读「距上次请求已够久」再各自放行，
    全局最小间隔 MIN_INTERVAL 会失效 → 直接违反 B站限速合规红线。
    加锁后语义不变，只是把「判定 + 睡眠 + 记时」变成原子操作。
    """
    import random
    global _request_count, _last_request_at
    with _pace_lock:
        _request_count += 1
        interval = MIN_INTERVAL + random.uniform(0, 1.0)  # 1.5-2.5s 抖动
        if _request_count % 10 == 0:
            interval += random.uniform(5, 10)  # 周期性长冷却
        elapsed = time.time() - _last_request_at
        if elapsed < interval:
            time.sleep(interval - elapsed)
        _last_request_at = time.time()


def _session_get(url, params=None, cookies=None, timeout=15):
    """带限速与浏览器头的 GET（供本模块与 bili_daily_v2 共用）。"""
    pace()
    return requests.get(url, params=params, headers=HEADERS, cookies=cookies, timeout=timeout)


def fetch_popular(ps=20, pn=1, cookies=None):
    """热门榜单：游客零签名直取，条目自带 cid/aid。"""
    r = _session_get(POPULAR_API, params={"ps": ps, "pn": pn}, cookies=cookies)
    if r.status_code in (412, 429):
        raise SystemExit(f"[风控] HTTP {r.status_code}：当日停止该接口，次日再试。")
    d = r.json()
    if d.get("code") != 0:
        print(f"[榜单失败] code={d.get('code')}", file=sys.stderr)
        return []
    return d.get("data", {}).get("list", [])


def load_cookies(path):
    """解析 Netscape cookies.txt → dict（仅保留 B站域）。"""
    ck = {}
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line.startswith("#") or not line:
            continue
        parts = line.split("\t")
        if len(parts) >= 7 and ".bilibili.com" in parts[0]:
            ck[parts[5]] = parts[6]
    return ck


def get_subtitle_tracks(bvid, cid, aid, cookies):
    """字幕轨列表：/x/player/wbi/v2（带登录 cookie）。返回 [{lang, lan_doc, subtitle_url}]。"""
    pace()
    params = {"bvid": bvid, "cid": cid}
    if aid:
        params["aid"] = aid
    r = requests.get(PLAYER_API, params=params, headers=HEADERS, cookies=cookies, timeout=15)
    if r.status_code in (412, 429):
        raise SystemExit(f"[风控] HTTP {r.status_code}：当日停止该接口，次日再试。")
    d = r.json()
    if d.get("code") != 0:
        print(f"  [字幕列表失败] code={d.get('code')}", file=sys.stderr)
        return []
    subs = (d.get("data", {}) or {}).get("subtitle", {}).get("subtitles") or []
    return [s for s in subs if s.get("subtitle_url")]


def download_subtitle_json(url, cookies):
    """subtitle_url 直接 GET 字幕 JSON（auth_key 时效，须立即使用）。"""
    pace()
    r = requests.get("https:" + url if url.startswith("//") else url,
                     headers=HEADERS, cookies=cookies, timeout=15)
    if r.status_code == 403:
        print("  [字幕下载失败] auth_key 过期（403），须重新获取字幕列表", file=sys.stderr)
        return None
    if r.status_code in (412, 429):
        raise SystemExit(f"[风控] HTTP {r.status_code}：当日停止该接口。")
    if r.status_code != 200:
        print(f"  [字幕下载失败] HTTP {r.status_code}", file=sys.stderr)
        return None
    try:
        return r.json()
    except ValueError:
        print("  [字幕下载失败] 非 JSON 响应", file=sys.stderr)
        return None


def body_to_srt(body):
    """字幕 JSON body[{from,to,content}] → SRT。"""
    out = []
    for i, seg in enumerate(body, 1):
        f = seg.get("from", 0)
        t = seg.get("to", 0)
        txt = (seg.get("content") or "").strip()
        out.append(f"{i}\n{_fmt(f)} --> {_fmt(t)}\n{txt}")
    return "\n\n".join(out)


def _fmt(sec):
    ms = int(round((sec - int(sec)) * 1000))
    s = int(sec)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d},{ms:03d}"


def body_to_txt(body):
    """字幕 JSON → 纯文本（去时间戳）。"""
    return "\n".join((seg.get("content") or "").strip() for seg in body if seg.get("content"))


def main():
    ap = argparse.ArgumentParser(description="B站视频字幕抓取（专注版）")
    ap.add_argument("--ps", type=int, default=20, help="热门榜条数（默认 20）")
    ap.add_argument("--bvid", default=None, help="指定抓取单个视频（替代榜单）")
    ap.add_argument("--cookies", required=True, help="Netscape cookies.txt（登录态，必需）")
    ap.add_argument("--out-dir", default="subs", help="字幕输出目录")
    ap.add_argument("--format", choices=["srt", "txt", "json", "all"], default="srt",
                    help="输出格式（默认 srt）")
    args = ap.parse_args()

    if not os.path.exists(args.cookies):
        print(f"[错误] cookies 文件不存在：{args.cookies}", file=sys.stderr)
        print("  导出方法：浏览器登录 B站（小号）→ 扩展如 Get cookies.txt LOCALLY 导出", file=sys.stderr)
        sys.exit(1)
    cookies = load_cookies(args.cookies)
    if not cookies.get("SESSDATA"):
        print("[警告] cookies 中未找到 SESSDATA（字幕必需）。请确认已登录 B站后导出。", file=sys.stderr)
    os.makedirs(args.out_dir, exist_ok=True)

    # 1. 确定目标视频
    if args.bvid:
        targets = [{"bvid": args.bvid, "cid": None, "aid": None, "title": args.bvid}]
    else:
        print(f"[1/3] 获取热门榜单 ps={args.ps} ...")
        lst = fetch_popular(ps=args.ps)
        if not lst:
            sys.exit(1)
        targets = [{"bvid": v.get("bvid"), "cid": v.get("cid"), "aid": v.get("aid"),
                    "title": v.get("title", "")} for v in lst]
        print(f"[榜单] {len(targets)} 条")

    # 2. 逐条取字幕（串行、限速）
    ok, no_sub, fail = [], [], []
    for i, t in enumerate(targets, 1):
        bvid = t["bvid"]
        print(f"  [{i}/{len(targets)}] {t['title'][:32]}（{bvid}）")
        if not t["cid"]:
            fail.append((bvid, "无 cid"))
            continue
        tracks = get_subtitle_tracks(bvid, t["cid"], t["aid"], cookies)
        if not tracks:
            no_sub.append((bvid, t["title"]))
            print("    无字幕轨（视频没有 AI 字幕/CC 字幕）")
            continue
        track = next((s for s in tracks if s.get("lan") and "ai" in str(s.get("lan"))), tracks[0])
        print(f"    字幕轨: {track.get('lan_doc', track.get('lan'))} / {track.get('lan')}")
        data = download_subtitle_json(track["subtitle_url"], cookies)
        if not data:
            fail.append((bvid, "下载失败"))
            continue
        body = data.get("body", [])
        if not body:
            no_sub.append((bvid, t["title"]))
            continue
        safe = re.sub(r"[\\/:*?\"<>|]", "_", t["title"])[:60]
        base = os.path.join(args.out_dir, f"{safe}_{bvid}")
        fmts = {"srt": ("srt", body_to_srt), "txt": ("txt", body_to_txt),
                "json": ("json", lambda b: json.dumps(data, ensure_ascii=False, indent=2))}
        if args.format == "all":
            for f, fn in fmts.items():
                with open(f"{base}.{f}", "w", encoding="utf-8") as fh:
                    fh.write(fn(body))
        else:
            f, fn = fmts[args.format]
            with open(f"{base}.{f}", "w", encoding="utf-8") as fh:
                fh.write(fn(body))
        ok.append((bvid, t["title"], len(body)))
        print(f"    ✓ 已保存 {len(body)} 条字幕 → {base}.{args.format if args.format != 'all' else 'srt'}")

    # 3. 汇总
    print(f"\n[完成] 成功 {len(ok)} / 无字幕 {len(no_sub)} / 失败 {len(fail)}"
          f"（请求计数 {_request_count}）")
    if no_sub:
        print("无字幕轨：")
        for bvid, title in no_sub:
            print(f"  - {title[:40]}（{bvid}）")
    if fail:
        print("失败：")
        for bvid, why in fail:
            print(f"  - {bvid}：{why}")
    print(f"输出目录：{os.path.abspath(args.out_dir)}")


if __name__ == "__main__":
    main()
