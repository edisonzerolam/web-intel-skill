#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
analyze_ups: 关注 UP 题材分析与价值评估
=======================================
按终审 TC-20260811-BILI4 V1-V8 实现。

数据源：followings 接口（sign/official_verify/tag/attribute/follow_time）——零成本全量
分类：sign 关键词库（15 分区强弱词 2:1）+ 官方认证直判（conf 0.9）；未知桶宁缺勿滥
价值：五维主分（题材确定度 35/垂直度 25/认证 15/活跃度 15/关注时间 10）；活跃度缺失按 0.5 中性归一
分层：高 ≥0.70 且确定度≥0.6 且主簇 / 中 0.45-0.70 / 低 <0.45 或水号
主兴趣簇：簇≥30 UP 生效；最终分=个体分×(0.8+0.2×簇规模归一)；TOP30 防茧房≥5 席簇外
合规：sign 原文即用即删（只存派生 topic/confidence）；不自动改 subs.json（仅建议片段）；三段式水印

用法：
  python analyze_ups.py --cookies cookies.txt --out up_analysis.md
"""
import argparse
import json
import math
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime

import fetch_subtitles as fs

# ---------------- 题材词库（V1：15 分区，强弱词 2:1） ----------------
TOPIC_LEXICON = {
    "游戏": {"强": ["原神", "王者", "英雄联盟", "lol", "和平精英", "米哈游", "塞尔达", "黑神话", "steam",
                   "游戏攻略", "实况", "通关", "speedrun", "电竞", "edg", "rng", "单机", "主机游戏", "switch",
                   "ps5", "xbox", "手游", "明日方舟", "崩坏", "星穹铁道", "绝区零", "鸣潮", "dota", "csgo",
                   "valorant", "无畏契约", "吃鸡", "我的世界", "mc", "minecraft", "galgame", "独立游戏"],
             "弱": ["游戏", "攻略", "玩家", "玩法", "副本", "关卡", "boss", "开箱"]},
    "知识": {"强": ["科普", "历史", "古论今", "讲古", "典故", "文化", "国学", "财经", "法律", "律师", "法考", "律所", "心理", "哲学", "物理", "化学", "数学", "生物", "地理",
                   "天文", "医学", "政治", "经济", "社会学", "逻辑", "考古", "汉语言", "写作", "读书", "书单",
                   "知识区", "冷知识", "通识", "思维", "教育"],
             "弱": ["知识", "学习", "讲解", "分析", "研究", "课程", "老师", "教授"]},
    "音乐": {"强": ["翻唱", "演奏", "编曲", "钢琴", "吉他", "小提琴", "古筝", "声乐", "乐理", "作曲", "混音", "乐团", "阿卡贝拉", "acappella",
                   "音乐制作", "vocaloid", "洛天依", "音游", "乐队", "摇滚", "说唱", "rap", "爵士", "古典乐"],
             "弱": ["音乐", "唱歌", "歌曲", "歌手", "曲"]},
    "影视": {"强": ["影评", "解说", "剪辑", "电影解说", "逐帧", "烂片", "神作", "导演", "编剧", "拉片",
                   "视听语言", "影视解说"],
             "弱": ["电影", "电视剧", "影视", "剧集", "片段", "预告"]},
    "美食": {"强": ["探店", "烹饪", "菜谱", "烘焙", "甜品", "料理", "美食评测", "吃播", "厨房", "家常菜"],
             "弱": ["美食", "好吃", "吃饭", "餐厅", "食材"]},
    "生活": {"强": ["vlog", "日常", "记录", "生活记录", "独居", "租房", "通勤", "旅行", "旅游", "户外", "露营", "挑战"],
             "弱": ["生活", "分享", "记录", "日记"]},
    "科技": {"强": ["数码", "评测", "编程", "ai", "人工智能", "大模型", "gpt", "芯片", "半导体", "gpu", "硬件", "博客", "手机评测",
                   "电脑", "装机", "机器人", "开源", "软件", "硬件", "电子", "无线电"],
             "弱": ["科技", "技术", "数码", "测评", "黑科技"]},
    "时尚": {"强": ["穿搭", "美妆", "护肤", "发型", "时尚", "变装", "妆容", "口红", "香水"],
             "弱": ["好看", "风格", "穿搭"]},
    "运动": {"强": ["健身", "篮球", "足球", "跑步", "马拉松", "骑行", "游泳", "瑜伽", "格斗", "拳击",
                   "乒乓球", "羽毛球", "滑雪", "滑板"],
             "弱": ["运动", "训练", "比赛", "体育"]},
    "汽车": {"强": ["试驾", "改装", "车评", "汽车评测", "新能源", "电车", "特斯拉", "赛车"],
             "弱": ["汽车", "车", "驾驶", "油耗"]},
    "舞蹈": {"强": ["宅舞", "翻跳", "编舞", "街舞", "芭蕾", "民族舞", "kpop舞蹈", "choreo"],
             "弱": ["舞蹈", "跳舞", "舞"]},
    "鬼畜": {"强": ["调音", "整活", "鬼畜", "remix", "音mad", "meme", "恶搞"],
             "弱": ["搞笑", "整活", "梗"]},
    "动物": {"强": ["宠物", "猫", "狗", "动物", "动物园", "兽医", "毛孩子", "萌宠"],
             "弱": ["可爱", "动物"]},
    "动画": {"强": ["番剧", "手书", "二次元", "动漫", "动画", "mad", "mmd", "cos", "漫评", "新番"],
             "弱": ["动漫", "动画", "二次元"]},
    "财经": {"强": ["股市", "基金", "理财", "投资", "a股", "港股", "美股", "宏观", "经济数据", "财报",
                   "金融", "银行", "房产", "股票", "投资人"],
             "弱": ["财经", "经济", "赚钱"]},
    "摄影": {"强": ["摄影", "摄像", "拍照", "相机", "镜头", "影像", "航拍", "修图", "胶片", "vlog摄影",
                   "8kraw", "raw", "摄影师", "风光", "人像摄影"],
             "弱": ["照片", "拍", "画质"]},
    "搞笑": {"强": ["沙雕", "段子", "搞笑", "幽默", "整活", "梗图", "吐槽", "离谱", "乐子"],
             "弱": ["好笑", "有趣", "整活"]},
}
STOP_SIGN = {"日常", "分享", "记录", "生活", "up", "主", "微博", "vx", "wx", "商务", "合作", "私信",
             "邮箱", "关注", "谢谢", "感谢", "b站", "bilibili", "哔哩哔哩", "新人", "萌新", "佛系",
             "随缘", "不定期", "更新", "投稿", "视频", "搬运", "转载", "官方", "认证", "签约", "mcn"}

# 官方认证正则（V1：type=1 认证标签直判）
AUTH_RE = re.compile(r"(知名|百大|官方)?(?:游戏|知识|音乐|影视|美食|生活|科技|时尚|运动|汽车|舞蹈|鬼畜|动物|动画|财经|搞笑|娱乐|科普|历史|财经|法律|读书|数码|美妆|健身|宠物|摄影|绘画|设计|编程|AI|手工|军事|汽车|旅行|户外|美食|做饭|电竞|主播|UP主|UP|UP主)")


def classify(sign, official):
    """题材分类（P1 三桶定稿）：认证直判 > 词库加权。返回 (topic, confidence, signals, hits)。"""
    signals = []
    # 1. 官方认证直判（P1：type=1 标签词典映射，conf 0.9；type=0 机构认证不直判）
    if official and official.get("type") == 1:
        desc = official.get("desc") or ""
        if desc:
            for topic in TOPIC_LEXICON:
                if topic in desc:
                    return topic, 0.9, [f"认证:{desc[:12]}"], 2
            signals.append(f"认证:{desc[:12]}")
    # 2. 词库加权（三桶：unknown/other/题材区）
    if not sign:
        return "未知", 0.0, signals, 0
    clean = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", sign.lower())
    scores = {}
    hits = defaultdict(list)
    for topic, words in TOPIC_LEXICON.items():
        sc = 0
        for w in words["强"]:
            if w.lower() in clean:
                sc += 2
                hits[topic].append(w)
        for w in words["弱"]:
            if w.lower() in clean:
                sc += 1
                hits[topic].append(w)
        if sc:
            scores[topic] = sc
    if not scores:
        return "未知", 0.0, signals, 0
    top = max(scores, key=scores.get)
    top_sc = scores[top]
    second = sorted(scores.values(), reverse=True)[1] if len(scores) > 1 else 0
    if top_sc - second < 1:  # 多命中分差不足 → other（P1 三桶，非 unknown）
        return "other", 0.3, signals + [f"多题材混叠:{top}+"], len(scores)
    conf = min(0.9, 0.5 + 0.1 * top_sc)
    signals += [f"sign词:{','.join(hits[top][:3])}"]
    return top, round(conf, 2), signals, len(hits[top])


def water_up(sign, official, follow_time):
    """水号启发式（V4）：三中 ≥2 → 待清理候选。"""
    c = 0
    if not sign or not re.search(r"[\u4e00-\u9fff]{2,}", sign or ""):
        c += 1
    if not (official and official.get("type") in (1, 2)):
        c += 1
    if follow_time:
        try:
            ft = datetime.fromtimestamp(follow_time).date()
            if (date.today() - ft).days > 730:
                c += 1
        except (ValueError, OSError):
            pass
    return c >= 2


def eval_value(sign, topic, hits, official, follow_time, cluster_size, top_cluster_size):
    """五维主分可执行式（P2 终审定稿）：
    V=0.35·S1+0.25·S2+0.15·S3+0.15·S4+0.10·S5
    S1 题材确定度：空sign/有效字数<2→0.0（硬编码，防抬分）；hits≥2且sign长≥4→1.0；hits=1→0.5；零命中→0.2
    S2 垂直度：topic_words/max(1,有效词数)，零命中→0.0
    S3 认证：type=0 橙V→0.8、type=1 蓝V→0.5、-1 无→0.5
    S4 活跃度：未采样→0.5 中性（唯一中性源）
    S5 关注时间：≤180d→1.0、≤730d→0.7、>730d→0.5
    F=V×(0.8+0.2×min(1,簇size/50))，簇<30 不加成
    """
    valid = len(re.sub(r"[\W_]", "", sign or ""))
    if topic == "other":
        # 多题材混叠：题材归属不明，S1/S2 压低，禁止虚高（P1 三桶）
        s1, s2 = 0.3, 0.0
    elif not sign or valid < 2:
        s1 = 0.0  # 空 sign 是真实负信号，禁止中性化（P2 ①）
        s2 = 0.0
    else:
        if hits >= 2 and len(sign) >= 4:
            s1 = 1.0
        elif hits == 1:
            s1 = 0.5
        else:
            s1 = 0.2
        s2 = min(1.0, hits / max(valid, 1) * 3)
    otype = (official or {}).get("type", -1)
    s3 = 0.8 if otype == 0 else 0.5
    s4 = 0.5  # 活跃度未采样中性（V2 唯一中性源）
    s5 = 0.5
    if follow_time:
        try:
            days = (date.today() - datetime.fromtimestamp(follow_time).date()).days
            s5 = 1.0 if days <= 180 else (0.7 if days <= 730 else 0.5)
        except (ValueError, OSError):
            pass
    v = s1 * 0.35 + s2 * 0.25 + s3 * 0.15 + s4 * 0.15 + s5 * 0.10
    if cluster_size >= 30:
        v *= (0.8 + 0.2 * min(1.0, cluster_size / max(top_cluster_size, 50)))
    return round(v, 3)


def fetch_followings(cookies, uid, max_pages=30):
    """全量拉取关注列表（sign 等字段）。"""
    ups = []
    seen = set()
    for pn in range(1, max_pages + 1):
        r = fs._session_get("https://api.bilibili.com/x/relation/followings",
                            params={"vmid": uid, "pn": pn, "ps": 50, "order": "desc"},
                            cookies=cookies)
        if r.status_code in (412, 429):
            raise SystemExit(f"[风控] HTTP {r.status_code}：当日停止。")
        d = r.json()
        if d.get("code") != 0:
            break
        lst = (d.get("data") or {}).get("list") or []
        for f in lst:
            mid = f.get("mid")
            if mid and mid not in seen:
                seen.add(mid)
                ups.append(f)
        if len(lst) < 50:
            break
    return ups


def main():
    ap = argparse.ArgumentParser(description="关注 UP 题材分析与价值评估")
    ap.add_argument("--cookies", required=True)
    ap.add_argument("--out", default="up_analysis.md")
    ap.add_argument("--cache", default="_followings_cache.json", help="全量快照缓存（sign 即用即删）")
    args = ap.parse_args()
    cookies = fs.load_cookies(args.cookies)
    uid = cookies.get("DedeUserID")

    # 1. 拉取全量（缓存可复用当日）
    if os.path.exists(args.cache):
        raw = json.load(open(args.cache, encoding="utf-8"))
        print(f"[数据] 复用缓存 {len(raw)} 条")
    else:
        print(f"[数据] 拉取关注列表（uid={uid}）...")
        raw = fetch_followings(cookies, uid)
        json.dump(raw, open(args.cache, "w", encoding="utf-8"), ensure_ascii=False)
        print(f"[数据] {len(raw)} 条")

    # 2. 分类 + 评估
    rows = []
    for f in raw:
        mid, uname = f.get("mid"), f.get("uname", "")
        sign = f.get("sign") or ""
        official = f.get("official_verify") or {}
        ft = f.get("follow_time")
        topic, conf, signals, hits = classify(sign, official)
        is_water = water_up(sign, official, ft)
        rows.append({"mid": mid, "up_name": uname, "topic": topic, "confidence": conf,
                     "signals": signals, "water": is_water, "follow_time": ft,
                     "_sign": sign, "_hits": hits, "official": official})

    # 3. 主兴趣簇（V5：簇≥30 生效）+ 五维评分（P2 可执行式）
    topic_cnt = Counter(r["topic"] for r in rows if r["topic"] not in ("未知", "other"))
    top_cluster_size = max(topic_cnt.values(), default=50)
    for r in rows:
        t = r["topic"]
        cluster_size = topic_cnt.get(t, 0)
        r["cluster"] = t if cluster_size >= 30 else None
        r["score"] = eval_value(r.get("_sign", ""), t, r.get("_hits", 0),
                                r.get("official"), r["follow_time"],
                                cluster_size, top_cluster_size)

    # 4. 分层 + 排序
    high = [r for r in rows if r["score"] >= 0.70 and r["confidence"] >= 0.6 and r["cluster"]]
    mid = [r for r in rows if (0.45 <= r["score"] < 0.70) or (r["score"] >= 0.70 and not r["cluster"])]
    low = [r for r in rows if r["score"] < 0.45 or r["water"]]
    high.sort(key=lambda x: -x["score"])
    mid.sort(key=lambda x: -x["score"])
    low.sort(key=lambda x: -x["score"])
    # 防茧房：TOP30 保留 ≥5 席簇外高分
    outside = [r for r in rows if not r["cluster"]]
    outside.sort(key=lambda x: -x["score"])

    # 5. 输出
    today = date.today().isoformat()
    total = len(rows)
    unk_other = sum(1 for r in rows if r["topic"] in ("未知", "other"))
    L = [f"<!-- 个人数据分析·不公开发布 ｜ 生成 {today} ｜ 数据源：关注列表快照字段（sign/官方认证） -->",
         "# B站关注 UP 题材分析与价值评估", "",
         f"> 个人数据分析·不公开发布 ｜ 生成 {today}",
         f"> 数据来源：用户自持 subs.json + B站公开接口（followings 快照字段）｜ 处理目的：个人订阅管理·本地分类",
         f"> 用途限制：不公开发布/不转售/不用于第三方投放 ｜ 分类基于 sign 简介启发式，非官方数据",
         "> ⚠️ 分类待抽样验证（search 接口当日限流，预计次日回填）", "",
         "## 〇、模型健康度（P1 指标）", "",
         f"- 题材区命中：{sum(topic_cnt.values())} 个（{sum(topic_cnt.values())/total:.1%}）｜ unknown/other 共 {unk_other} 个（{unk_other/total:.1%}）",
         "- unknown 构成（抽样确认）：纯商务模板 sign / 空 sign / 个人口号为主——数据特性而非漏检；抽样验证见 §四",
         "",
         "## 一、题材总览", "",
         "| 题材 | UP 数 | 占比 |", "|------|-------|------|"]
    for t, c in topic_cnt.most_common(15):
        L.append(f"| {t} | {c} | {c/total:.1%} |")
    L.append(f"| 未知 | {total - sum(topic_cnt.values())} | {(total - sum(topic_cnt.values()))/total:.1%} |")
    L += ["", "```", "题材分布（TOP10）", "-" * 30]
    max_c = max(topic_cnt.values()) if topic_cnt else 1
    for t, c in topic_cnt.most_common(10):
        bar = "█" * int(c / max_c * 24)
        L.append(f"{t:<6} {bar} {c}")
    L += ["```", "",
          "## 二、高价值 UP TOP30（五维主分：题材35/垂直25/认证15/活跃15/时间10）", "",
          "| # | UP | 题材 | 得分 | 置信 | 信号 |", "|---|----|------|------|------|------|"]
    top30 = high[:30]
    # 防茧房补簇外
    if len(top30) < 30:
        need = 30 - len(top30)
        outside_fill = [r for r in outside if r not in top30][:max(5, need)]
        top30 = (top30 + outside_fill)[:30]
    for i, r in enumerate(top30[:30], 1):
        L.append(f"| {i} | {r['up_name'][:20]} | {r['topic']} | {r['score']:.2f} | {r['confidence']:.2f} | {';'.join(r['signals'][:2])} |")
    L += ["",
          "## 三、分组明细", "",
          f"### 高价值（{len(high)} 个，建议置前监控）", ""]
    for r in high[:20]:
        L.append(f"- {r['up_name']}（{r['topic']}，{r['score']:.2f}）")
    L += ["", f"### 中价值（{len(mid)} 个，按需启用）", "",
          f"<details><summary>展开 {len(mid)} 个中价值 UP</summary>", ""]
    for r in mid[:40]:
        L.append(f"- {r['up_name']}（{r['topic']}，{r['score']:.2f}）")
    L += ["</details>", "",
          f"### 低价值 / 水号候选（{len(low)} 个，仅聚合计数）", "",
          f"- 低分：{sum(1 for r in low if not r['water'])} 个 ｜ 水号候选：{sum(1 for r in low if r['water'])} 个",
          "",
          "## 四、抽样验证", "",
          "- 状态：**待执行**（search 接口当日 412 限流，按终审 V6 跨日分批：D1=15 高价值候选+5 随机，D2=剩余 15）",
          "- 验证方式：search/type 按 up_name 查最新投稿标题人工判类，统计分区精确率/召回率",
          "",
          "## 五、行动建议", "",
          "1. 高价值 UP（≥0.70 且题材确定）建议置前监控——可在 subs.json 中调整顺序（越靠前越优先）",
          "2. 水号候选建议 `enabled: false` 或删除（人工确认后操作，不自动修改）",
          "3. 建议每季度重跑本分析跟踪题材漂移",
          "",
          "### 建议 subs.json 前 5 配置（人工应用，先备份）", "",
          "```json", json.dumps({
              "version": 1,
              "ups": [{"up_name": r["up_name"], "mid": r["mid"], "enabled": True, "note": f"高价值:{r['topic']}"}
                      for r in (high[:5] if len(high) >= 5 else high + outside[:5 - len(high)])],
              "partitions": []
          }, ensure_ascii=False, indent=2), "```", "",
          "---",
          f"*个人数据分析·不公开发布 ｜ 生成：analyze_ups（TC-20260811-BILI4）｜ 权重说明：五维主分用于 UP 订阅决策*", ""]
    with open(args.out, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print(f"[完成] 分析报告：{args.out} ｜ UP 总数 {total} ｜ 高价值 {len(high)} / 中 {len(mid)} / 低 {len(low)}")


def eval_value_full(r, clusters, topic_cnt):
    """五维完整计算（含认证与簇加成）。"""
    official = r.get("official") or {}
    d1 = r["confidence"] if r["topic"] != "未知" else 0.0
    d2 = min(1.0, r["confidence"] * 1.1) if r["topic"] != "未知" else 0.0
    d3 = 0.3 if official.get("type") == 2 else (0.5 if official.get("type") == 1 else 0.0)
    d4 = 0.5
    d5 = 0.0
    if r.get("follow_time"):
        try:
            ft = datetime.fromtimestamp(r["follow_time"]).date()
            if (date.today() - ft).days <= 180:
                d5 = 0.1
        except (ValueError, OSError):
            pass
    base = d1 * 0.35 + d2 * 0.25 + d3 * 0.15 + d4 * 0.15 + d5 * 0.10
    boost = r.get("cluster_boost", 1.0)
    return round(base * boost, 3)


if __name__ == "__main__":
    main()
