#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
subs: UP 主 / 分区订阅监控
==========================
按终审 TC-20260811-BILI3 V4-V6 实现。

查新路径：/x/web-interface/search/type?search_type=video&keyword={up_name}&order=pubdate
  （实测 /x/space/arc/search -799 不可用；search/type 带 cookie 免 wbi code=0）
合规：subs.json 为用户自持配置（up_name 必需 + mid 锚点），禁止自动回写任何接口字段
  （清单哈希不变断言）；日志脱敏（仅 up_name/bvid）；固定前 5 enabled UP；池 ≤20/日；K≤3

subs.json schema:
  {"version": 1,
   "ups": [{"up_name": "...", "mid": 123456, "enabled": true, "note": ""}],
   "partitions": [{"rid": 4, "name": "游戏", "enabled": true}]}
"""
import hashlib
import json
import os
import sys

SEARCH_API = "https://api.bilibili.com/x/web-interface/search/type"
VIEW_API = "https://api.bilibili.com/x/web-interface/view"
POOL_LIMIT = 20      # 订阅池日请求上限
K_DEFAULT = 3        # 每 UP 新视频处理数上限
MAX_UPS = 5          # 固定前 N 个 enabled UP


def load_subs(path="subs.json"):
    if not os.path.exists(path):
        print(f"[订阅] 无 subs.json（跳过订阅），创建示例：{path}", file=sys.stderr)
        sample = {"version": 1, "ups": [], "partitions": []}
        json.dump(sample, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        return sample, None
    st = json.load(open(path, encoding="utf-8"))
    # 哈希断言基准（禁回写校验用）
    h = hashlib.sha256(open(path, "rb").read()).hexdigest()
    return st, h


def subs_hash_unchanged(path, base_hash):
    """清单哈希不变断言（V5：禁自动回写任何接口字段）。"""
    h = hashlib.sha256(open(path, "rb").read()).hexdigest()
    return h == base_hash


def sanitize(s):
    """日志脱敏（V5 单一入口）：仅输出 up_name 与 bvid。"""
    import re
    s = re.sub(r"mid[\"':= ]+\d+", "mid=***", str(s))
    return s[:120]


def search_up(up_name, cookies, budget_spend=None, pool_counter=None):
    """按 up_name 搜索最新视频（实测唯一可用路径）。"""
    from fetch_subtitles import _session_get
    if pool_counter:
        pool_counter()
    if budget_spend:
        budget_spend()
    r = _session_get(SEARCH_API, params={
        "search_type": "video", "keyword": up_name, "order": "pubdate", "page": 1,
    }, cookies=cookies)
    if r.status_code in (412, 429):
        raise SystemExit(f"[风控] HTTP {r.status_code}：当日停止订阅接口。")
    d = r.json()
    if d.get("code") != 0:
        print(f"  [订阅] search code={d.get('code')}", file=sys.stderr)
        return []
    result = (d.get("data") or {}).get("result") or []
    # author 精确匹配（V4）
    return [v for v in result if v.get("author") == up_name]


def get_video_cid(bvid, cookies, budget_spend=None, pool_counter=None):
    """view 接口补 cid（search 结果无 cid）。"""
    from fetch_subtitles import _session_get
    if pool_counter:
        pool_counter()
    if budget_spend:
        budget_spend()
    r = _session_get(VIEW_API, params={"bvid": bvid}, cookies=cookies)
    if r.status_code in (412, 429):
        raise SystemExit(f"[风控] HTTP {r.status_code}：当日停止。")
    d = r.json()
    if d.get("code") != 0:
        return None
    return (d.get("data") or {}).get("cid")


def check_subs(cookies, cookies_path=None, seen=None, budget_spend=None,
               tmp_dir="out/_tmp", asr_callback=None, max_subs=K_DEFAULT):
    """订阅检查：返回 (新视频列表, 订阅视频字幕结果)。

    seen: bvid→首见日期 dict（跨模块共享去重）
    asr_callback: 无字幕轨时触发 ASR 兜底（与主线共享日限）
    """
    st, base_hash = load_subs()
    if not st.get("ups"):
        return [], {}
    ups = [u for u in st["ups"] if u.get("enabled")][:MAX_UPS]
    seen = seen or {}
    new_videos, sub_results = [], {}
    pool_count = 0
    zero_match = {}

    def pool():
        nonlocal pool_count
        pool_count += 1
        if pool_count > POOL_LIMIT:
            raise SystemExit(f"[订阅] 订阅池达日上限 {POOL_LIMIT}，停止。")

    for u in ups:
        name = u.get("up_name", "")
        if not name:
            continue
        try:
            hits = search_up(name, cookies, budget_spend=budget_spend, pool_counter=pool)
        except SystemExit:
            raise
        matched = [v for v in hits if v.get("bvid") and v.get("bvid") not in seen]
        if not matched:
            zero_match[name] = zero_match.get(name, 0) + 1
            continue
        for v in matched[:max_subs]:
            bvid = v["bvid"]
            seen[bvid] = __import__("datetime").date.today().isoformat()
            cid = get_video_cid(bvid, cookies, budget_spend=budget_spend, pool_counter=pool)
            new_videos.append({"bvid": bvid, "cid": cid, "aid": v.get("aid"),
                               "title": v.get("title", ""), "tname": v.get("typeid"),
                               "up": name})
        # 7 日零匹配提示（V4，不自动改配置）
        if zero_match.get(name, 0) >= 7:
            print(f"  [订阅] ⚠️ {name} 连续 7 日无新视频，疑似改名/注销，请人工核对 subs.json")
    if new_videos:
        print(f"[订阅] 检出 {len(new_videos)} 个新视频（UP：{len(ups)}，池 {pool_count}/{POOL_LIMIT}）")
    if not subs_hash_unchanged("subs.json", base_hash):
        raise SystemExit("[订阅] 断言失败：subs.json 被修改（禁止自动回写）")
    return new_videos, sub_results
