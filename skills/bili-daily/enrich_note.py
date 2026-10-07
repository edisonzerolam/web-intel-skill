#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
enrich_note: 转录产物加工（摘要 + 要点 + 标签，写回 frontmatter）
==================================================================
融合自 chubbyskills v0.13.0 content-enrich skill 的输出协议（frontmatter 注入
summary/domain/auto_tags + 正文顶部摘要块 + enriched:true 幂等标记；MIT），
加工逻辑保留 bili-daily 的启发式（零外发默认）与 LLM opt-in 设计（默认关）。

用法：
  python enrich_note.py out/transcripts/xxx_BVxxx.md            # 启发式加工（零外发）
  python enrich_note.py ./out/transcripts/                      # 批量目录
  python enrich_note.py xxx.md --llm                            # LLM 加工（需 DEEPSEEK_API_KEY，内容外发）
  python enrich_note.py xxx.md --force                          # 已加工的重做
"""
import argparse
import json
import os
import re
import sys

# 本机 locale=cp936：重配置 stdout/stderr 为 UTF-8，避免 emoji/中文打印 GBK 崩溃
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

import jieba
import jieba.posseg as pseg

# 词性白名单（与 bili_daily_v2 口径一致）：名词/动词/英文词
POS_WHITELIST = {"n", "nr", "ns", "nt", "nz", "v", "vn", "eng"}


# ---------------------------------------------------------------- frontmatter 工具

def parse_frontmatter(path):
    """读取 (frontmatter dict, body str)。无 frontmatter 时返回 ({}, 全文)。"""
    with open(path, encoding="utf-8") as f:
        raw = f.read()
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", raw, re.DOTALL)
    if not m:
        return {}, raw
    fm = {}
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        k = k.strip()
        v = v.strip()
        try:
            fm[k] = json.loads(v)
        except (ValueError, json.JSONDecodeError):
            fm[k] = v
    return fm, m.group(2)


def dump_value(v):
    return json.dumps(v, ensure_ascii=False)


# ---------------------------------------------------------------- 启发式加工（零外发）

def tokenize(text):
    return [w.word for w in pseg.cut(text)
            if w.flag in POS_WHITELIST and len(w.word.strip()) >= 2]


def split_sentences(text):
    """按句号与换行切句（B站字幕常为逐行一句且无句号）。"""
    out = []
    for line in (text or "").splitlines():
        for s in re.split(r"[。！？!?]+", line):
            s = s.strip().strip("，,；; ")
            if s:
                out.append(s)
    return out


def heuristic_summary(body: str) -> str:
    """TL;DR：取正文第一个完整句；无则取前 40 字。"""
    for s in split_sentences(body):
        if len(s) >= 12:
            return s[:60]
    return (body.strip() or "")[:40]


def heuristic_points(body: str, top_n=4) -> list:
    """要点：按关键词密度给句子排序，取 top-N 整句。"""
    words = tokenize(body)
    if not words:
        return []
    from collections import Counter
    hot = {w for w, _ in Counter(words).most_common(12)}
    sents = [s for s in split_sentences(body) if len(s) >= 10]
    scored = sorted(
        ((sum(1 for w in tokenize(s) if w in hot), len(s), s) for s in sents),
        key=lambda t: (-t[0], -t[1]),
    )
    out = []
    for _, _, s in scored:
        if s not in out:
            out.append(s)
        if len(out) >= top_n:
            break
    return out


def heuristic_tags(body: str, title: str = "", top_n=5) -> list:
    """标签：词频 top 关键词（>=2 次才入选），标题词加权。"""
    words = tokenize(title + " " + body)
    from collections import Counter
    cnt = Counter(words)
    title_words = set(tokenize(title))
    ranked = sorted(cnt.items(), key=lambda kv: (-kv[1], -len(kv[0])))
    out = []
    for w, n in ranked:
        if n < 2 and w not in title_words:
            continue
        if w not in out:
            out.append(w)
        if len(out) >= top_n:
            break
    return out


def heuristic_domain(tags) -> str:
    """领域粗判：tags 前缀，兜底 general。"""
    for w in tags:
        if re.search(r"(基金|股票|财经|金融|投资)", w):
            return "finance"
        if re.search(r"(科技|AI|人工智能|编程|数码)", w):
            return "tech"
        if re.search(r"(知识|历史|文化|教育)", w):
            return "knowledge"
    return "general"


# ---------------------------------------------------------------- LLM 加工（opt-in）

def llm_enrich(title: str, body: str) -> dict:
    """调 DeepSeek 兼容 chat/completions，返回 {summary, domain, auto_tags, points}。"""
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        raise RuntimeError("--llm 需要环境变量 DEEPSEEK_API_KEY")
    import requests
    prompt = (
        "你是内容加工助手。给定转录稿，输出 JSON（仅 JSON，不要 markdown 代码块）：\n"
        '{"summary": "一句话总结(<=60字)", "domain": "领域标签(英文/中文均可)", '
        '"auto_tags": ["3~5个标签"], "points": ["3~5条要点"]}\n\n'
        "忠实度铁律：只允许压缩与摘录原文已有信息，禁止改写为分析文风、禁止补充原文没有的"
        "实体/因果/背景（例如原文未提\"大厂\"就不得出现\"大厂\"）。\n\n"
        f"标题：{title}\n\n转录稿：\n{body[:6000]}"
    )
    r = requests.post(
        "https://api.deepseek.com/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"model": "deepseek-chat", "messages": [{"role": "user", "content": prompt}],
              "temperature": 0.3, "max_tokens": 800},
        timeout=60,
    )
    r.raise_for_status()
    raw = r.json()["choices"][0]["message"]["content"]
    # 容错解析（借鉴 chubby_common/llm.parse_json_response）
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", raw, re.DOTALL)
    if m:
        raw = m.group(1)
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end > start:
        raw = raw[start:end + 1]
    data = json.loads(raw)
    return {
        "summary": str(data.get("summary", ""))[:80],
        "domain": str(data.get("domain", "general"))[:40],
        "auto_tags": [str(t)[:20] for t in (data.get("auto_tags") or [])][:6],
        "points": [str(p)[:80] for p in (data.get("points") or [])][:6],
    }


# ---------------------------------------------------------------- 写回

def build_summary_block(enr: dict) -> str:
    lines = ["## 📝 摘要", ""]
    if enr.get("summary"):
        lines.append(f"- **TL;DR**：{enr['summary']}")
    pts = enr.get("points") or []
    if pts:
        lines.append("- **要点**：")
        lines += [f"  - {p}" for p in pts]
    tags = enr.get("auto_tags") or []
    if tags:
        lines.append(f"- **标签**：{', '.join(tags)}")
        lines.append(f"- **领域**：{enr.get('domain', 'general')}")
    lines += ["", "---", ""]
    return "\n".join(lines)


def enrich_file(path, use_llm, force):
    fm, body = parse_frontmatter(path)
    if fm.get("enriched") and not force:
        print(f"  ⏭️  {os.path.basename(path)} 已加工（--force 重做）")
        return False

    title = str(fm.get("title") or os.path.basename(path))
    # 摘要块只插在正文内容前（跳过可能已存在的旧摘要块）
    pure = re.sub(r"^## 📝 摘要.*?(?=^#|\Z)", "", body, flags=re.DOTALL | re.MULTILINE).strip()
    # 内容主体：剔除标题/元信息/水印行，供加工（避免把 frontmatter 行当正文）
    content = "\n".join(
        ln for ln in pure.splitlines()
        if not ln.lstrip().startswith(("#", "- ", "> ", "---"))
    ).strip()

    if use_llm:
        enr = llm_enrich(title, content)
    else:
        tags = heuristic_tags(content, title)
        enr = {
            "summary": heuristic_summary(content),
            "domain": heuristic_domain(tags),
            "auto_tags": tags,
            "points": heuristic_points(content),
        }

    fm["summary"] = enr["summary"]
    fm["domain"] = enr["domain"]
    fm["auto_tags"] = enr["auto_tags"]
    fm["enriched"] = True

    fm_lines = ["---"]
    for k, v in fm.items():
        fm_lines.append(f"{k}: {dump_value(v)}")
    fm_lines.append("---")
    new_body = "\n".join(fm_lines) + "\n\n" + build_summary_block(enr) + "\n" + pure + "\n"

    with open(path, "w", encoding="utf-8") as f:
        f.write(new_body)
    src = "LLM" if use_llm else "启发式"
    print(f"  ✅ {os.path.basename(path)}（{src}）summary+{len(enr.get('auto_tags') or [])}标签+{len(enr.get('points') or [])}要点")
    return True


def main():
    ap = argparse.ArgumentParser(
        description="转录产物加工：摘要+要点+标签写回 frontmatter（融合 chubbyskills content-enrich 协议）")
    ap.add_argument("target", help="转录产物 .md 或目录")
    ap.add_argument("--llm", action="store_true", help="LLM 加工（需 DEEPSEEK_API_KEY；内容外发，默认关）")
    ap.add_argument("--force", action="store_true", help="已加工（enriched:true）的重做")
    args = ap.parse_args()

    if os.path.isdir(args.target):
        files = sorted(f for f in os.listdir(args.target) if f.endswith(".md"))
        targets = [os.path.join(args.target, f) for f in files]
    else:
        targets = [args.target]

    if not targets:
        print("[错误] 未找到 .md 文件", file=sys.stderr)
        sys.exit(1)

    ok = 0
    for t in targets:
        try:
            if enrich_file(t, args.llm, args.force):
                ok += 1
        except Exception as e:
            print(f"  ❌ {os.path.basename(t)}：{str(e)[:120]}", file=sys.stderr)
    print(f"\n[完成] 加工 {ok}/{len(targets)}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()