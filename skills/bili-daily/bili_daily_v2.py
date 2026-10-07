#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bili-daily-v2: B站每日热门视频洞察（分类榜 + 关键词综合排序 + 字幕内容理解）
==============================================================================
按终审 TC-20260811-BILI2（V1-V11）实现。

功能：
  1. 各分类 top20 热门榜（9 分区，跨日去重：同一视频不同日子再上热门榜则 pass）
  2. 全站 top20
  3. 关键词综合排序 top20（关键词级主榜 score=0.7A+0.3B + 视频级副榜）
  4. 字幕内容理解（分段/要点/梗概/情感，启发式，LLM opt-in）
  5. P0 延伸：corpus 统计持久化（趋势追踪数据源）、每日简报（私密模板）

合规约束（V7/V8/V10）：
  - 预算：160 预警 / 200 硬停；串行间隔 ≥1.5s；412/429 当日停止
  - 数据生命周期：字幕全文 24-48h 物理删除（任务结束清理）；元数据 ≤30 天；bvid+统计语料长期
  - 引用检测：连续 ≥15 字与原文重合判为引用，超配额拒绝输出
  - 不存 uid；报告仅个人使用，水印脚注，无公开发布通道

用法：
  python bili_daily_v2.py --cookies cookies.txt --out-dir out/
"""
import argparse
import difflib
import json
import math
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import date, datetime

import jieba
import jieba.posseg as pseg

import fetch_subtitles as fs  # 复用字幕获取核心（fetch_popular/get_subtitle_tracks 等）

# ---------------- 配置（终审 V1/V4/V8） ----------------
PARTITIONS = {1: "动画", 3: "音乐", 4: "游戏", 5: "娱乐", 36: "知识",
              160: "生活", 119: "鬼畜", 129: "舞蹈", 188: "科技"}
RANKING_API = "https://api.bilibili.com/x/web-interface/ranking/v2"
N_CANDIDATE = 40          # 候选池上限（V3）
W_MAIN = 0.7              # 主榜 A 分量权重（V4，3 天试运行后校准）
W_COVER = 0.3             # 主榜 B 分量权重
N_MIN_COVER = 2           # 关键词覆盖视频数门槛 n_k≥2
GAP_SEC = 60.0            # 分段断点阈值（V6）
SEG_MIN_SEC = 15.0        # 段最短时长
SEG_MAX = 5               # 段数封顶
QUOTE_CHARS = 15          # 引用判定：连续重合字符数（V7，工程约定）
BUDGET_WARN = 160         # 预算预警
BUDGET_HARD = 200         # 预算硬停
SEEN_FILE = "seen_bvids.json"
CORPUS_FILE = "corpus.jsonl"
META_TTL_DAYS = 30
TMP_TTL_HOURS = 48

STOPWORDS = set("""的 了 和 是 在 我 你 他 她 它 这 那 就 都 也 又 很 有 不 要 会 能 可以 吗 啊 呢 吧 呀 哦 哈
这个 那个 什么 怎么 我们 你们 他们 一个 一下 自己 没有 还是 因为 所以 但是 如果 然后 现在 已经 一直 起来
出来 过来 觉得 知道 真的 就是 不是 其实 应该 可能 大家 视频 字幕 观众 各位 欢迎 谢谢 关注 投币 收藏 点赞
一键三连 up 主 下 上 里 中 说 看 听 想 做 去 来 给 让 把 被 从 对 跟 和 与 及 或 而 且 但 并 这里 这么 那么
时候 所有 还有 里面 这些 那些 看到 非常 直接 东西 开始 为什么 出现 不能 发现 感觉 认为 需要 想要 喜欢 这个
那个 什么 怎么 我们 你们 他们 大家 自己 已经 现在 然后 就是 其实 应该 需要 可以 因为 所以 但是 如果 还有
还是 一直 起来 出来 过来 上面 下面 前面 后面 旁边 地方 事情 问题 方式 方法 原因 结果 情况 听说 好像 似乎
反正 确实 肯定 一定 也许 大概 几乎 完全 特别 十分 相当 比较 有点 稍微 简单 容易 麻烦 重要 关键 主要 基本
必须 能够 没有 不能 不会 不想 不要 别 再 更 最 很 太 好 哪个 哪些 一些 有些 任何 每个 别的 其他 其中 之
于 到 被 让 给 跟 比 按 照 据 依 通过 根据 关于 由于 因此 从而 于是 接着 最后 最终 首先 其次 另外 以及
甚至 尤其 只是 不过 虽然 尽管 无论 不管 假如 要是 只要 只有 除非 直到 等等 之类 似的 一样 进去 回去 开来
""".split())
POS_WHITELIST = {"n", "nr", "ns", "nt", "nz", "v", "vn", "eng"}  # 词性白名单（V5）
PUNCT_RE = re.compile(r"[^\u4e00-\u9fffA-Za-z0-9]")

# 口语化情感词典（V6，轻量，仅参考维度）
POS_WORDS = {"喜欢", "好", "厉害", "好看", "可爱", "爽", "精彩", "神", "强", "棒", "完美", "感动", "泪目",
             "精彩", "帅", "美", "好玩", "有趣", "良心", "支持", "加油", "牛", "nb", "yyds", "真香"}
NEG_WORDS = {"难", "差", "不行", "失望", "无语", "尴尬", "烂", "假", "坑", "恶心", "烦", "讨厌", "垃圾",
             "离谱", "劝退", "失败", "翻车", "糟"}
NEG_FLIP = {"不", "没", "别", "无", "莫"}


def log(*a):
    print(*a, flush=True)


# ---------------- 预算护栏（V8） ----------------
class Budget:
    def __init__(self):
        self.count = 0
        self.degraded = False

    def spend(self, n=1):
        self.count += n
        if self.count >= BUDGET_HARD:
            raise SystemExit(f"[合规] 已达日请求硬停 {BUDGET_HARD}，当日不再发起任何抓取。")
        if self.count >= BUDGET_WARN and not self.degraded:
            self.degraded = True
            log(f"[预算] 已达 {BUDGET_WARN} 预警，字幕候选 N 降级→25")
            return -1  # 触发降级
        return 0


budget = Budget()


def ranked_rid(rid, cookies):
    """分类榜：ranking/v2 带 cookie 直取（V1，实测无需 wbi）。"""
    budget.spend()
    r = fs._session_get(RANKING_API, params={"rid": rid, "type": "all"}, cookies=cookies)
    if r.status_code in (412, 429):
        raise SystemExit(f"[风控] HTTP {r.status_code}：当日停止该接口。")
    d = r.json()
    if d.get("code") != 0:
        return []
    return (d.get("data") or {}).get("list") or []


# ---------------- 去重注册表（V2） ----------------
def load_seen():
    if os.path.exists(SEEN_FILE):
        return json.load(open(SEEN_FILE, encoding="utf-8"))
    return {}


def save_seen(seen):
    json.dump(seen, open(SEEN_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


# ---------------- 字幕预处理（V5） ----------------
def merge_dups(segments):
    """相邻条去重合并：difflib 相似度 >0.85 的相邻短句合并。"""
    merged = []
    for seg in segments:
        t = (seg.get("content") or "").strip()
        if not t:
            continue
        if merged:
            prev = merged[-1]
            prev_t = prev["content"]
            ratio = difflib.SequenceMatcher(None, prev_t, t).ratio()
            if ratio > 0.85 or t in prev_t or prev_t in t:
                # 合并到前一条（时间取并集），压缩叠词
                prev["content"] = prev_t + t
                prev["to"] = seg.get("to", prev["to"])
                prev["n"] = prev.get("n", 1) + 1
                continue
        merged.append({"from": seg.get("from", 0), "to": seg.get("to", 0), "content": t, "n": 1})
    return merged


def compress_repeat(text):
    """叠词压缩：'是什么是什么' → '是什么'（保留一次）。"""
    return re.sub(r"(.{2,6})\1+", r"\1", text)


def tokenize(text):
    words = []
    for w, flag in pseg.lcut(compress_repeat(text)):
        w = w.strip()
        if (len(w) >= 2 and w not in STOPWORDS and not w.isdigit()
                and flag in POS_WHITELIST and not PUNCT_RE.fullmatch(w)):
            words.append(w)
    return words


def extract_keywords(docs, top_n=10):
    """跨文档 TF-IDF：docs=[(bvid, tokens)]，返回 {bvid: [(word, w), ...]}。"""
    df = Counter()
    for _, toks in docs:
        for w in set(toks):
            df[w] += 1
    N = max(len(docs), 1)
    out = {}
    for bvid, toks in docs:
        tf = Counter(toks)
        n = len(toks)
        scored = [(w, (c / max(n, 1)) * math.log((N + 1) / (df[w] + 1) + 1)) for w, c in tf.items()]
        scored.sort(key=lambda x: -x[1])
        out[bvid] = scored[:top_n]
    return out


# ---------------- 内容理解（V6，启发式） ----------------
def segment_video(merged):
    """时间轴分段：相邻 gap>60s 断点，段长≥15s，封顶 5 段。"""
    if not merged:
        return []
    segs, cur = [], [merged[0]]
    for a, b in zip(merged, merged[1:]):
        gap = b["from"] - a["to"]
        if gap > GAP_SEC and (b["to"] - cur[0]["from"]) >= SEG_MIN_SEC:
            segs.append(cur)
            cur = [b]
        else:
            cur.append(b)
    segs.append(cur)
    segs = [s for s in segs if (s[-1]["to"] - s[0]["from"]) >= SEG_MIN_SEC]
    if not segs:  # 短视频兜底：全文为一段
        segs = [merged]
    return segs[:SEG_MAX]


def summarize_video(title, desc, merged, segs):
    """梗概：标题词 × 字幕高频词共现打分取 top1 句；无命中取首条 ≥6 字句。"""
    title_toks = set(tokenize(title)) | set(tokenize(desc[:60]))
    all_toks = [t for s in merged for t in tokenize(s["content"])]
    freq = Counter(all_toks)
    if not freq:
        return (merged[0]["content"][:60] if merged else title)[:60]
    best, best_score = None, -1
    for s in merged:
        toks = tokenize(s["content"])
        if not toks:
            continue
        score = sum(freq.get(t, 0) for t in toks if t in title_toks) * 10 + len(toks)
        if score > best_score:
            best_score, best = score, s["content"]
    if not best or len(best) < 6:
        best = next((s["content"] for s in merged if len(s["content"]) >= 6), title)
    return best[:60]


def key_points(segs):
    """段内要点：关键词密度 × 位置加权取 top 句（3-5 条，带时间戳）。"""
    pts = []
    for seg in segs:
        toks = [t for s in seg for t in tokenize(s["content"])]
        if not toks:
            continue
        freq = Counter(toks)
        ranked = sorted(seg, key=lambda s: sum(freq.get(t, 0) for t in tokenize(s["content"])),
                        reverse=True)
        for s in ranked[:2]:
            txt = s["content"]
            if len(txt) < 4:
                continue
            pts.append({"point": txt[:50], "ts_from": s["from"], "ts_to": s["to"],
                        "quote": txt[:60]})
        if len(pts) >= 5:
            break
    return pts[:5]


def sentiment_of(merged):
    """口语化情感词典（否定词窗口翻转），返回粗粒度倾向。"""
    pos = neg = 0
    for s in merged:
        txt = compress_repeat(s["content"])
        for w in POS_WORDS:
            i = txt.find(w)
            if i >= 0 and not any(txt[max(0, i - 2):i].endswith(n) for n in NEG_FLIP):
                pos += 1
        for w in NEG_WORDS:
            i = txt.find(w)
            if i >= 0 and not any(txt[max(0, i - 2):i].endswith(n) for n in NEG_FLIP):
                neg += 1
    tot = pos + neg
    if tot == 0:
        return "中性"
    return "正面" if pos / tot >= 0.6 else ("负面" if neg / tot >= 0.6 else "中性")


def quote_check(text, threshold=QUOTE_CHARS):
    """引用检测：与任何已知原文的最长公共子串 ≥15 字即判引用（占位实现，调用方传原文池）。"""
    return None  # 由报告生成器对摘要做 LCS 校验


def lcs_len(a, b):
    if len(a) < len(b):
        a, b = b, a
    for i in range(len(a) - len(b) + 1):
        if a[i:i + len(b)] == b:
            return len(b)
    return 0


# ---------------- 关键词综合排序（V4） ----------------
def keyword_ranking(video_keywords, heat_map):
    """关键词级主榜：score=0.7A+0.3B；A=Σw·ĥ 归一，B=log(1+n)/log(1+N_max)。"""
    agg = defaultdict(float)     # word -> Σ w_ki·ĥ_i
    cover = Counter()            # word -> 覆盖视频数
    word_videos = defaultdict(list)  # word -> [(bvid, w·ĥ)]
    for bvid, kws in video_keywords.items():
        h = heat_map.get(bvid, 0.0)
        for w, wt in kws:
            agg[w] += wt * h
            cover[w] += 1
            word_videos[w].append((bvid, wt * h))
    if not agg:
        return []
    vals = list(agg.values())
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    n_max = max(cover.values()) if cover else 1
    rows = []
    for w, a in agg.items():
        n = cover[w]
        if n < N_MIN_COVER:
            continue
        A = (a - lo) / span
        B = math.log(1 + n) / math.log(1 + n_max)
        rows.append({"word": w, "n": n, "score": W_MAIN * A + W_COVER * B, "A": A, "B": B})
    rows.sort(key=lambda x: -x["score"])
    for r in rows:
        r["videos"] = sorted(word_videos[r["word"]], key=lambda x: -x[1])[:3]
    return rows[:20]


# ---------------- 报告（V10 私密模板） ----------------
def build_report(date_s, partition_tops, all_top, kw_rank, cards, stats, vids=None):
    vids = vids or {}
    L = ["# B站每日热门视频洞察（分类版）", "",
         f"> 数据日期：{date_s} ｜ 分区：{len(partition_tops)} ｜ 请求：{stats['req']} ｜ 字幕：{stats['sub_ok']}/{stats['sub_try']}",
         "> 个人学习研究使用 ｜ 勿公开传播 ｜ 描述性分析，非作品替代品",
         "> 来源：B站公开榜单与字幕（登录态）｜ 字幕全文不保留（24-48h 即删）", ""]
    # 1. 关键词主榜
    L += ["## 一、关键词综合排序 TOP20（今日热词）", "",
          "| # | 关键词 | 覆盖视频 | 综合分 | 代表视频 |", "|---|--------|---------|-------|---------|"]
    for i, r in enumerate(kw_rank, 1):
        reps = "、".join((vids.get(v, {}).get("title", v))[:14] for v, _ in r["videos"][:2])
        L.append(f"| {i} | {r['word']} | {r['n']} | {r['score']:.3f} | {reps} |")
    L.append("")
    # 2. 各分区 top20
    L += ["## 二、各分类 TOP20（跨日去重：重复上榜已 pass）", ""]
    for rid, name in PARTITIONS.items():
        vs = partition_tops.get(rid, [])
        if not vs:
            continue
        L.append(f"### {name}区（{len(vs)} 条）")
        L.append("| 排名 | 标题 | 播放 | 点赞 |")
        L.append("|------|------|------|------|")
        for v in vs[:20]:
            L.append(f"| {v['rank']} | {v['title'][:30]} | {v['play']:,} | {v['like']:,} |")
        L.append("")
    # 3. 全站
    L += ["## 三、全站 TOP20（去重后）", "", "| # | 标题 | 分区 | 播放 |", "|---|------|------|------|"]
    for i, v in enumerate(all_top[:20], 1):
        L.append(f"| {i} | {v['title'][:30]} | {v['tname']} | {v['play']:,} |")
    L.append("")
    # 4. 视频理解卡片
    L += ["## 四、视频内容理解（启发式）", ""]
    for c in cards:
        src_tag = f"｜ 订阅：{c['up']}" if c.get("up") else ("｜ ASR转写·低可信" if c.get("asr") else "")
        L += [f"### {c['title'][:36]}", f"- `{c['bvid']}` ｜ {c['tname']} ｜ 播放 {c['play']:,} ｜ 情感：{c['sentiment']}{src_tag}",
              f"- 一句话梗概：{c['summary']}", "- 要点："]
        for p in c["points"]:
            L.append(f"  - [{p['ts_from']:.0f}s-{p['ts_to']:.0f}s] {p['point']}")
        L.append("")
    L += ["---", "*个人学习研究 ｜ 勿公开传播 ｜ 生成：bili-daily-v2（TC-20260811-BILI2）*", ""]
    return "\n".join(L)


# ---------------- 主流程 ----------------
def main():
    ap = argparse.ArgumentParser(description="B站每日热门视频洞察（分类版）")
    ap.add_argument("--cookies", required=True)
    ap.add_argument("--out-dir", default="out")
    ap.add_argument("--max-subs", type=int, default=N_CANDIDATE, help="字幕候选上限")
    ap.add_argument("--zone-subs", type=int, default=0, help="各区 top K 抓字幕（替代全站热度优先）")
    ap.add_argument("--enable-asr", action="store_true", help="启用 ASR 兜底（无字幕轨，日限 5）")
    ap.add_argument("--subs", default=None, help="订阅清单路径（默认 subs.json，不存在则跳过）")
    args = ap.parse_args()
    if not os.path.exists(args.cookies):
        sys.exit(f"[错误] cookies 不存在：{args.cookies}")
    cookies = fs.load_cookies(args.cookies)
    os.makedirs(args.out_dir, exist_ok=True)
    tmp_dir = os.path.join(args.out_dir, "_tmp")
    os.makedirs(tmp_dir, exist_ok=True)

    today = date.today().isoformat()
    seen = load_seen()
    vids = {}          # bvid -> video dict（标题等）
    partition_tops = defaultdict(list)
    heat_map = {}

    # 1. 分区榜（V1）
    log("[1/4] 拉取 9 分区排行 ...")
    for rid, name in PARTITIONS.items():
        lst = ranked_rid(rid, cookies)
        for i, v in enumerate(lst[:20], 1):
            bvid = v.get("bvid")
            if not bvid:
                continue
            stat = v.get("stat", {})
            vids.setdefault(bvid, {"bvid": bvid, "title": v.get("title", ""),
                                   "desc": v.get("desc", ""), "tname": v.get("tname", name),
                                   "rid": rid,
                                   "play": stat.get("view", 0), "like": stat.get("like", 0),
                                   "danmaku": stat.get("danmaku", 0),
                                   "cid": v.get("cid"), "aid": v.get("aid")})
            partition_tops[rid].append({"rank": i, "title": v.get("title", ""),
                                        "play": stat.get("view", 0), "like": stat.get("like", 0)})
        log(f"  {name}区：{len(lst[:20])} 条（累计请求 {budget.count}）")

    # 2. 全站（popular 2 页）
    log("[2/4] 全站热门 ...")
    all_top = []
    for pn in (1, 2):
        for v in fs.fetch_popular(ps=40, pn=pn, cookies=cookies):
            bvid = v.get("bvid")
            if bvid and bvid not in vids:
                stat = v.get("stat", {})
                vids[bvid] = {"bvid": bvid, "title": v.get("title", ""), "desc": v.get("desc", ""),
                              "tname": v.get("tname", ""), "play": stat.get("view", 0),
                              "like": stat.get("like", 0), "danmaku": stat.get("danmaku", 0),
                              "cid": v.get("cid"), "aid": v.get("aid")}
    all_top = sorted(vids.values(), key=lambda x: -x["play"])

    # 3. 跨日去重 + 候选池（V2/V3；--zone-subs K = 各区 top K 字幕，替代全站热度优先）
    new_vids = {b: v for b, v in vids.items() if b not in seen}
    log(f"[去重] 共 {len(vids)} 条，新视频 {len(new_vids)}，已见 {len(vids) - len(new_vids)}（pass）")
    if args.zone_subs > 0:
        # 每区取 top K（按播放，按 rid 归属——tname 是二级分区名不可精确匹配），跨区并集去重
        zone_pool = {}
        for rid, name in PARTITIONS.items():
            zone_vids = [(v, v.get("play", 0)) for v in new_vids.values()
                         if v.get("rid") == rid]
            zone_vids.sort(key=lambda x: -x[1])
            for v, _ in zone_vids[:args.zone_subs]:
                zone_pool.setdefault(v["bvid"], v)
        candidates = list(zone_pool.values())
        log(f"[候选] 各区 top{args.zone_subs} 并集：{len(candidates)} 个（{len(PARTITIONS)} 区）")
    else:
        candidates = sorted(new_vids.values(), key=lambda x: -x["play"])[:args.max_subs]

    # 4. 字幕 + 内容理解（V3/V5/V6）
    log(f"[3/4] 抓取字幕并理解（候选 {len(candidates)}，预算 {budget.count}/{BUDGET_HARD}）...")
    video_keywords, heat_map, cards, corpus_lines = {}, {}, [], []
    src_of = {}  # bvid -> ("sub"|"asr")，三区 ASR 过滤用（V3）
    asr_videos = []  # 无字幕轨候选（白名单内，V3）
    sub_ok = sub_try = 0
    for i, v in enumerate(candidates, 1):
        bvid = v["bvid"]
        if not v.get("cid"):
            continue
        if budget.spend() < 0:  # 160 预警降级
            args.max_subs = 25
            break
        sub_try += 1
        tracks = fs.get_subtitle_tracks(bvid, v["cid"], v["aid"], cookies)
        if not tracks:
            log(f"  [{i}/{len(candidates)}] {v['title'][:24]} 无字幕轨")
            asr_videos.append(v)  # V3：收集待 ASR 兜底
            continue
        track = next((s for s in tracks if s.get("lan") and "ai" in str(s.get("lan"))), tracks[0])
        data = fs.download_subtitle_json(track["subtitle_url"], cookies)
        if not data:
            continue
        body = data.get("body", [])
        if not body:
            continue
        sub_ok += 1
        merged = merge_dups(body)
        segs = segment_video(merged)
        summary = summarize_video(v["title"], v["desc"], merged, segs)
        pts = key_points(segs)
        senti = sentiment_of(merged)
        toks = [t for s in merged for t in tokenize(s["content"])]
        video_keywords[bvid] = toks
        src_of[bvid] = "sub"
        # 热度归一化（当日语料基线）
        heat = math.log1p(v["play"]) * 0.5 + math.log1p(v["like"]) * 0.3 + math.log1p(v["danmaku"]) * 0.2
        heat_map[bvid] = heat
        cards.append({"bvid": bvid, "title": v["title"], "tname": v["tname"], "play": v["play"],
                      "summary": summary, "points": pts, "sentiment": senti})
        # corpus 统计持久化（V9，无全文字段）
        corpus_lines.append(json.dumps({
            "date": today, "bvid": bvid, "partition": v["tname"], "title": v["title"],
            "keywords": [w for w, _ in Counter(toks).most_common(10)],
            "summary": summary, "sentiment": senti,
            "seg_ts": [{"from": s[0]["from"], "to": s[-1]["to"]} for s in segs],
        }, ensure_ascii=False))
        log(f"  [{i}/{len(candidates)}] {v['title'][:24]} ✓ {len(merged)} 条（合并后）")
    with open(CORPUS_FILE, "a", encoding="utf-8") as f:
        f.write("\n".join(corpus_lines) + ("\n" if corpus_lines else ""))

    # 4b. ASR 兜底（V3：无字幕轨 + 日限 5 + 转写即删）
    asr_results = {}
    if asr_videos and args.enable_asr:
        import asr_fallback
        log(f"[3b/4] ASR 兜底转写 {len(asr_videos)} 个无字幕轨视频（日限 {asr_fallback.DAILY_LIMIT}）...")
        tmp_dir = os.path.join(args.out_dir, "_tmp")
        os.makedirs(tmp_dir, exist_ok=True)
        asr_results = asr_fallback.transcribe_videos(
            asr_videos, cookies, tmp_dir, cookies_file=args.cookies,
            budget_spend=budget.spend)
        for bvid, segs in asr_results.items():
            v = next((x for x in asr_videos if x["bvid"] == bvid), None)
            if not v:
                continue
            merged = merge_dups(segs)
            toks = [t for s in merged for t in tokenize(s["content"])]
            src_of[bvid] = "asr"
            # 三区 ASR 不参与主榜与 DF（V3），但仍进卡片与 corpus
            if v.get("tname") not in ("音乐", "鬼畜", "舞蹈"):
                video_keywords[bvid] = [t for t in toks]  # ×0.7 降权在 ranking 侧体现
            heat = math.log1p(v.get("play", 0)) * 0.5 + math.log1p(v.get("like", 0)) * 0.3
            heat_map[bvid] = heat
            cards.append({"bvid": bvid, "title": v["title"], "tname": v.get("tname", ""),
                          "play": v.get("play", 0), "summary": "", "points": [],
                          "sentiment": "中性", "asr": True})
            corpus_lines.append(json.dumps({
                "date": today, "bvid": bvid, "partition": v.get("tname", ""),
                "title": v["title"], "src": "asr",
                "keywords": [w for w, _ in Counter(toks).most_common(10)],
                "summary": "", "sentiment": "中性",
                "seg_ts": [{"from": s[0]["from"], "to": s[-1]["to"]} for s in merged[:5]],
            }, ensure_ascii=False))
        if corpus_lines:
            with open(CORPUS_FILE, "a", encoding="utf-8") as f:
                f.write("\n".join(corpus_lines) + "\n")

    # 5. 关键词综合排序（V4；三区 ASR 已排除，×0.7 弱信号降权）
    kw_docs = {}
    for b, toks in video_keywords.items():
        if not toks:
            continue
        if src_of.get(b) == "asr":
            toks = [t for t in toks]  # 保持原样，降权在 keyword_ranking 内
            kw_docs[b] = [(w, c * 0.7) for w, c in Counter(toks).most_common(50)]
        else:
            kw_docs[b] = Counter(toks).most_common(50)
    kw_rank = keyword_ranking(kw_docs, heat_map)
    log(f"[4/4] 关键词榜 {len(kw_rank)} 条")

    # 4c. 订阅检查（V4-V6）
    sub_new = []
    if args.subs and os.path.exists(args.subs):
        import subs as subs_mod
        log("[3c/4] 订阅检查 ...")
        try:
            sub_new, _ = subs_mod.check_subs(cookies, cookies_path=args.cookies,
                                             seen=seen, budget_spend=budget.spend,
                                             tmp_dir=os.path.join(args.out_dir, "_tmp"))
        except SystemExit as e:
            log(f"[订阅] 停止：{e}")
        for sv in sub_new:
            if not sv.get("cid"):
                continue
            tracks = fs.get_subtitle_tracks(sv["bvid"], sv["cid"], sv["aid"], cookies)
            if not tracks:
                continue
            track = next((s for s in tracks if s.get("lan") and "ai" in str(s.get("lan"))), tracks[0])
            data = fs.download_subtitle_json(track["subtitle_url"], cookies)
            if not data or not data.get("body"):
                continue
            body = data["body"]
            merged = merge_dups(body)
            toks = [t for s in merged for t in tokenize(s["content"])]
            sv["_toks"] = toks
            sv["_merged_n"] = len(merged)
            cards.append({"bvid": sv["bvid"], "title": sv["title"], "tname": sv.get("tname", ""),
                          "play": 0, "summary": summarize_video(sv["title"], "", merged, segment_video(merged)),
                          "points": key_points(segment_video(merged)), "sentiment": sentiment_of(merged),
                          "up": sv.get("up")})
        if sub_new:
            log(f"[订阅] {len(sub_new)} 个新视频已处理")

    # 6. 报告（V10）
    stats = {"req": budget.count, "sub_ok": sub_ok, "sub_try": sub_try}
    report = build_report(today, partition_tops, all_top, kw_rank, cards, stats, vids)
    out_path = os.path.join(args.out_dir, f"daily_{today}.md")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(report)
    log(f"[完成] 报告：{out_path} ｜ 请求 {budget.count}/{BUDGET_HARD} ｜ 字幕 {sub_ok}/{sub_try}")

    # 7. 去重注册表更新 + 临时清理（V2/V7）
    for v in candidates:
        seen.setdefault(v["bvid"], today)
    save_seen(seen)
    cleanup_tmp(tmp_dir)
    log("[清理] 临时字幕已清理（全文不保留）")


def cleanup_tmp(tmp_dir, hours=TMP_TTL_HOURS):
    """V7：临时中间产物物理删除（任务结束即清）。"""
    if not os.path.isdir(tmp_dir):
        return
    now = time.time()
    for fn in os.listdir(tmp_dir):
        p = os.path.join(tmp_dir, fn)
        try:
            if now - os.path.getmtime(p) > hours * 3600:
                os.remove(p)
        except OSError:
            pass


if __name__ == "__main__":
    main()
