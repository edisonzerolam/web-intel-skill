#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
platforms/iqiyi: 爱奇艺元数据文本信号（TC-20260811-BILI8 V2 第一档）
====================================================================
pcw-api playervideoinfo 免登录实测 A00000（零签名零成本）：
返回 vn(标题)/an(短标题)/info(简介全文)/tg(标签)/vipType/barrage/operation_language_base(多语言标题)
用途：内容理解文本信号（标题+简介+标签 → 既有词法管线），无字幕轨道。

用法：
  from platforms.iqiyi import fetch_meta
  meta = fetch_meta("350289100")   # tvid
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fetch_subtitles import _session_get

API = "https://pcw-api.iqiyi.com/video/video/playervideoinfo"


def fetch_meta(tvid):
    """免登录拉取爱奇艺视频元数据（标题/简介/标签）。返回 dict 或 None。"""
    r = _session_get(API, params={"tvid": tvid})
    d = r.json()
    if d.get("code") != "A00000":
        return None
    data = d.get("data") or {}
    return {
        "tvid": tvid,
        "title": data.get("vn") or data.get("an", ""),
        "desc": (data.get("info") or "")[:200],
        "tags": data.get("tg") or [],
        "vip": data.get("vipType"),
        "album_id": data.get("albumId"),
        "lang_base": data.get("operation_language_base"),
    }


if __name__ == "__main__":
    import json
    for tvid in sys.argv[1:] or ["350289100"]:
        m = fetch_meta(tvid)
        print(json.dumps(m, ensure_ascii=False, indent=1) if m else f"{tvid}: 不可达")
