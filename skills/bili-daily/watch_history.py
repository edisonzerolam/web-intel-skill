#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
watch_history: 观看历史拉取与聚合（TC-20260811-BILI5 V1/V8）
=============================================================
管线：拉页 → 内存按 bvid 去重 → 当次分类 → 仅落盘聚合 watch_ups.json；原始零落盘。

合规三层护栏（V8/P4）：
  1. 端点白名单：ALLOWED_API 断言，未白名单端点直接 AssertionError（禁反查 UP 资料）
  2. 字段投影：sanitize 仅留 bvid/title/owner_mid/owner_uname/view_at，stat/progress 断言丢弃
  3. 唯一写入口：dump_watch_ups 写前断言聚合字段集，模块内无原始写盘路径

用法：
  python watch_history.py --cookies cookies.txt [--pages 10]
"""
import argparse
import json
import math
import os
import re
import sys
from collections import Counter, defaultdict

import fetch_subtitles as fs

# ---------------- 合规护栏（V8/P4） ----------------
ALLOWED_API = {
    "x/web-interface/ranking/v2",
    "x/web-interface/search/type",
    "x/v2/history",
    "x/web-interface/view",
    "x/web-interface/popular",
    "x/relation/followings",
    "x/passport-login/web/qrcode/generate",
    "x/passport-login/web/qrcode/poll",
}
KEEP_FIELDS = {"bvid", "title", "owner_mid", "owner_uname", "view_at", "tname"}


def api_get(path, **kw):
    """端点白名单断言（P4 护栏 1）。"""
    assert path in ALLOWED_API, f"[合规] 未白名单端点被调用: {path}"
    return fs._session_get(f"https://api.bilibili.com/{path}", **kw)


def sanitize(item):
    """字段白名单投影（P4 护栏 2）：stat/progress 断言丢弃。"""
    owner = item.get("owner") or {}
    out = {
        "bvid": item.get("bvid", ""),
        "title": item.get("title", ""),
        "owner_mid": owner.get("mid", 0),
        "owner_uname": owner.get("name", ""),
        "view_at": item.get("view_at", 0),
        "tname": item.get("tname", ""),
    }
    assert "stat" not in out and "progress" not in out, "[合规] 细粒度字段泄漏"
    return out


# ---------------- 二级子域词典（V2，冷启动 v0.1） ----------------
SUBDOMAIN_LEXICON = {
    "科技": {"AI": ["大模型", "gpt", "人工智能", "ai", "提示词", "ai绘画", "智能体", "深度求索"],
            "数码": ["手机", "鸿蒙", "折叠屏", "骁龙", "评测", "耳机", "平板", "数码"],
            "硬件": ["gpu", "显卡", "cpu", "装机", "芯片", "硬件", "散热"],
            "软件": ["编程", "开源", "python", "代码", "开发", "linux"]},
    "知识": {"财经": ["股市", "股票", "基金", "投资", "a股", "港股", "美股", "宏观", "经济"],
            "法律": ["法律", "律师", "法考", "案例", "判例", "法庭"],
            "历史": ["历史", "朝代", "古代", "王朝", "考古", "战国", "三国"],
            "科学": ["物理", "量子", "相对论", "天文", "黑洞", "基因", "细胞", "化学", "生物"]},
    "游戏": {"手游": ["原神", "王者", "星穹铁道", "崩坏", "明日方舟", "绝区零"],
            "单机": ["塞尔达", "黑神话", "steam", "独立游戏", "主机"],
            "电竞": ["电竞", "比赛", "战队", "夺冠", "世界赛", "lol", "cs"]},
    "生活": {"美食": ["美食", "探店", "烹饪", "菜谱", "吃播", "厨房"],
            "旅行": ["旅行", "旅游", "vlog", "自驾", "攻略"],
            "日常": ["日常", "记录", "独居", "生活"]},
    "音乐": {"创作": ["编曲", "作曲", "混音", "音乐制作", "翻唱", "原创"],
            "演奏": ["钢琴", "吉他", "演奏", "弹唱", "乐队", "鼓"]},
    "影视": {"解说": ["解说", "影评", "解析", "盘点", "剧情"],
            "剪辑": ["剪辑", "混剪", "逐帧", "mad"]},
}


def subdomain_of(title, tname):
    """子域分类：词典命中打分（V2 简化：纯词典，先验待聚合计数回填）。"""
    if not title:
        return None
    t = title.lower()
    best, best_score = None, 0
    for domain, subs in SUBDOMAIN_LEXICON.items():
        if domain not in tname:
            continue
        for sub, words in subs.items():
            sc = sum(2 if w in t else 0 for w in words)
            if sc > best_score:
                best, best_score = sub, sc
    return best if best_score >= 2 else None  # 未定 → None（直出 tname）


# ---------------- 唯一写入口（P4 护栏 3） ----------------
def dump_watch_ups(agg, meta):
    path = "watch_ups.json"
    for v in agg.values():
        assert set(v.keys()) == {"uname", "n", "total", "first_view_at",
                                 "last_view_at", "zones", "topics"}, \
            f"[合规] 聚合字段集异常: {set(v.keys())}"
    payload = {
        "schema_version": "v1.0",
        "updated_at": meta.get("fetched_at"),
        "purpose": "个人分析",
        "ttl_review_days": 180,
        "window_days": meta.get("window_days", 0),
        "max_view_at": meta.get("max_view_at", 0),
        "covered_days": meta.get("covered_days", 0),
        "ups": agg,
    }
    json.dump(payload, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return path


def main():
    ap = argparse.ArgumentParser(description="观看历史拉取与聚合")
    ap.add_argument("--cookies", required=True)
    ap.add_argument("--pages", type=int, default=10, help="拉取页数（默认 10，顶 20）")
    args = ap.parse_args()
    cookies = fs.load_cookies(args.cookies)

    print(f"[1/2] 拉取观看历史（{args.pages} 页 × 50 条）...")
    seen_bvid = set()
    agg = defaultdict(lambda: {"uname": "", "n": 0, "total": 0, "first_view_at": 0,
                               "last_view_at": 0, "zones": Counter(), "topics": Counter()})
    min_at, max_at = 0, 0
    for pn in range(1, args.pages + 1):
        r = api_get("x/v2/history", params={"ps": 50, "pn": pn}, cookies=cookies)
        if r.status_code in (412, 429):
            print(f"[风控] HTTP {r.status_code}：当日停止。")
            break
        d = r.json()
        if d.get("code") != 0:
            print(f"[失败] code={d.get('code')}")
            break
        lst = d.get("data") or []
        if not lst:
            break
        for item in lst:
            s = sanitize(item)  # 护栏 2：字段投影
            mid = s["owner_mid"]
            if not mid:
                continue
            u = agg[mid]
            u["uname"] = s["owner_uname"] or u["uname"]
            u["total"] += 1
            if s["bvid"] not in seen_bvid:
                seen_bvid.add(s["bvid"])
                u["n"] += 1  # 去重视频数（V1 修正 2）
            u["first_view_at"] = min(u["first_view_at"], s["view_at"]) if u["first_view_at"] else s["view_at"]
            u["last_view_at"] = max(u["last_view_at"], s["view_at"])
            if s["tname"]:
                u["zones"][s["tname"]] += 1
            sub = subdomain_of(s["title"], s["tname"])
            if sub:
                u["topics"][sub] += 1
            min_at = min(min_at, s["view_at"]) if min_at else s["view_at"]
            max_at = max(max_at, s["view_at"])
        print(f"  第 {pn} 页：{len(lst)} 条（累计去重 {len(seen_bvid)} 视频）")
        if len(lst) < 50:
            break

    covered = max(1, (max_at - min_at) // 86400) if min_at else 0
    meta = {"fetched_at": __import__("datetime").date.today().isoformat(),
            "window_days": covered, "max_view_at": max_at, "covered_days": covered}
    out = dump_watch_ups(agg, meta)
    n_up = len(agg)
    print(f"[2/2] 完成：{out} ｜ UP {n_up} 个 ｜ 去重视频 {len(seen_bvid)} 条 ｜ "
          f"覆盖 {covered} 天 ｜ 原始响应零落盘（progress/stat 已丢弃）")
    top = sorted(agg.items(), key=lambda x: -x[1]["n"])[:8]
    print("观看 TOP8：")
    for mid, u in top:
        print(f"  {u['uname'][:16]}：{u['n']} 视频 / {u['total']} 次 ｜ 最近 "
              f"{__import__('datetime').datetime.fromtimestamp(u['last_view_at']).date()}")


if __name__ == "__main__":
    main()
