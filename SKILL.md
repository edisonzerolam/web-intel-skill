---
name: web-intel
description: 网络情报超级技能——实时搜索、网页抓取与转换、视频内容洞察、深度调研与情报分析。覆盖：anysearch 实时搜索（通用/垂直域/1-5 并行批量/整页 extract，key 已配）、CDP 真浏览器自动化（过反爬/带登录态/点击填写截图）、任意 URL 转 Markdown（X/HN/YouTube 适配器）、YouTube 字幕与封面下载、B站字幕抓取与每日热门洞察（分类 top20/关键词榜/UP 订阅/ASR 兜底/观看实证/爱奇艺元数据）、深度调研引擎 deep-research（多源聚合/行业分析/竞品研究/事实核查/前瞻预测/策略红队）、deep-research-pipeline（七阶段自包含研究管道，内置 web_search/web_fetch 无需 key）。当用户提到搜索、anysearch、实时搜索、批量搜索、垂直搜索、搜一下、查资料、抓取、抓这个网址、网页转 markdown、字幕下载、B站/哔哩哔哩/视频字幕/热榜/关键词榜/UP 订阅/视频内容理解、网页监控、深度调研、研究报告、竞品调研、竞品分析、情报分析、事实核查、预测推演、红队压测方案等请求时触发——先读本文件路由。抓取类操作遵守本机 host 白名单与私网拒绝基线。注意：不使用 Firecrawl（收费 API，已归档），抓取走 browser-cdp / baoyu-url-to-markdown。
metadata: {version: 3.1.0, tier: super, subskills: 7, refactor: 2026-10-02, absorbed: [bili-daily, deepdive-claims-ledger, cti-expert-AEAD, useosint-layering], strengthened: [heuer-ACH, berkeley-protocol-digital-evidence]}
version: 3.1.0
---

# web-intel — 网络情报

一句话：**取得到（Acquire）→ 读得懂（Enrich）→ 判得准（Assess）→ 交得出（Deliver）**。

> 抓取后端基线（2026-08-31）：Firecrawl 全家桶已归档禁用（收费 API，用户裁决），不要调用或建议安装。默认：静态页用内置 fetch；反爬/登录态走 `browser-cdp`；站点适配转换走 `baoyu-url-to-markdown`。

## 0 懒加载协议（先看这里）

本文件是**路由层**，不是操作手册。规则：

1. **只加载需要的那一个子技能**。定位到 `skills/<name>/` 后 `Read` 它的 `SKILL.md`，按它执行——不要把多个子技能一起读进上下文。
2. **references/ 按需读**，主文件不复制其内容。触发条件写在 §4 表格「何时读」列。
3. **深度调研类**：默认先读 `references/research-discipline.md`（强制）。档位 ≥ medium 再叠加 `claims-ledger.md` + `source-dispatch.md`。
4. 子技能内部的 `references/` 由该子技能自己声明何时读，主文件不代劳。

## 1 主骨架：AEAD 四阶段

| 阶段 | 干什么 | 用什么 |
|---|---|---|
| **A 获取** | 找到并取回原始材料 | `anysearch` / `browser-cdp` / `baoyu-url-to-markdown` / `bili-daily` |
| **E 富化** | 转格式、抽正文、转字幕、做笔记 | `baoyu-url-to-markdown` / `baoyu-youtube-transcript` / `bili-daily` 的 `enrich_note.py` |
| **As 评估** | 分级、交叉验证、对抗审查、多解释取舍 | `deep-research` / `deep-research-pipeline` + `references/claims-ledger.md` + `references/ach.md` |
| **D 交付** | 成报告 / 简报 / 数据集（涉证据时过取证规范） | `deep-research*` 报告模板 + `references/digital-evidence.md`，产物落 `deliverables/<topic>/` |

简单任务不必走满四阶段——一条搜索就答完的，别上管道。

## 2 子技能路由（两层：workflow 点名跑 / technique 自动调用）

### workflow 层（端到端流程，通常用户一句话 = 跑一个）

| 场景 | 子技能 |
|---|---|
| 通用/垂直域实时搜索、1-5 并行批量搜索、整页正文 extract | `skills/anysearch/` |
| CDP 真 Chrome（登录态/过反爬/点击/表单/截图/搜索结果页） | `skills/browser-cdp/` |
| 任意 URL → Markdown（X/HackerNews/YouTube 适配器） | `skills/baoyu-url-to-markdown/` |
| YouTube 字幕/封面下载（多语言/翻译/章节/说话人） | `skills/baoyu-youtube-transcript/` |
| **B站/视频平台洞察**：字幕抓取、分类 top20、关键词榜、UP 订阅、观看实证、趋势周报、ASR 兜底、爱奇艺元数据 | `skills/bili-daily/`（2026-10-02 并入，原独立技能） |
| 深度调研/行业分析/竞品研究/事实核查/预测推演/策略红队（多子代理重型） | `skills/deep-research/` |
| 单会话标准调研报告（七阶段，内置 web_search/web_fetch，**无需 key**） | `skills/deep-research-pipeline/` |

### technique 层（被 workflow 内部调用的原子能力，一般不单独点名）

| 能力 | 位置 |
|---|---|
| 请求预算与限流（160 预警/200 硬停/412 当日停） | `skills/bili-daily/req_budget.py` |
| 登录态获取（B站扫码） | `skills/bili-daily/bilibili_login.py` |
| 视频转录（字幕→yt-dlp→SenseVoice→whisper 四级降级） | `skills/bili-daily/transcribe_video.py` |
| 源可信度打分 / Admiralty 编码 | `skills/deep-research/references/admiralty-scale.md` |

## 3 调研档位（开工先声明）

| 档 | 源数量 | 子代理 | 何时 | 强制动作 |
|---|---|---|---|---|
| **shallow** | 5–7 | 0 | 导航性、熟悉领域、低风险 | — |
| **medium** | 12–18 | 2–3 | 非平凡、要落决策 | plan-review gate + claims-ledger |
| **deep** | 25–35+ | 4–5 | 高风险/战略级 | 上述 + 五角色红队 + 三角验证 |

声明档位时给一句理由。deep 档跑长任务时写 `state.md`（**每轮重写，不累积**），避免上下文被历史轮撑爆。

## 4 references 懒加载索引

| 文件 | 何时读（触发条件） |
|---|---|
| `references/research-discipline.md` | **任何深度调研/报告任务，开工前必读**（D1-D7，含 D7 锚定派单） |
| `references/channel-notes.md` | **抓取失败/选通道时先查**——本机一手实测的通道边界表（SPA 页/TLS 阻断/重定向/政府站 extract 失败/ego 稳定性/B站字幕坑/站点黑名单/清洗模型幻觉表）；踩到新坑顺手更新本表 + `self_evolve.py log` |
| `references/claims-ledger.md` | 档位 ≥ medium，或需要交叉验证/事实核查时 |
| `references/source-dispatch.md` | 需要决定「这个子问题该走哪个通道」时 |
| `references/pivot.md` | 竞品/实体型调研，要从已知标识符扩展关系网时 |
| `references/competitor-intel.md` | 竞品调研/竞品分析类任务 |
| `references/ach.md` | **存在多个合理解释**时（归因、定性、"为什么"、真伪判定）——反确认偏误 |
| `references/digital-evidence.md` | 材料要**作为证据引用/涉及自然人/可能对外**时——保全与验证规范 |

## 5 工作流链（典型组合）

- **快速情报**：`anysearch` search/batch_search → 要全文再 `anysearch extract` 或 `baoyu-url-to-markdown`
- **被反爬**：静态失败 → 查 `references/channel-notes.md` 按失败特征选通道（真浏览器 / python urllib+UA / curl 直连）→ 仍不行 `browser-cdp`
- **竞品深调**：`anysearch` 并行铺面 → `browser-cdp`/`baoyu-url-to-markdown` 抓多源 → `deep-research` 交叉验证
- **标准报告（零 key）**：`web_search`/`web_fetch`（或 `anysearch` 提速）→ `deep-research-pipeline`
- **事实核查**：`deep-research` 问答集核查模式（fact-verification/foresight 已并入其 `_deprecated/`）
- **归因/定性**：列 ≥3 个假设 → `references/ach.md` 判诊断性 → 报全部相对可能性
- **证据引用**：抓取时同时存原始文件+时间戳+URL → `references/digital-evidence.md` 过保全与验证
- **视频内容理解**：`bili-daily`（B站/国内）或 `baoyu-youtube-transcript`（YouTube）
- **每日热门洞察**：`bili-daily` 完整管线 → `weekly_report.py` 出趋势周报

## 6 边界

- ⚠️ **调研纪律强制**：深度调研/报告先读 `references/research-discipline.md`（**v1.1**）。核心：D1 一手材料全文分段读（≤800 字符切段，禁 read 截断漏读）；D2 存疑≠证伪、无官方源≠不存在；D3 矛盾以本地一手原文裁决；D4 子代理输出过同等 Admiralty 门禁；D5 来源四档★A-D、数字带编号、URL 用 curl/anysearch/ego 判存活（禁本地 PowerShell HTTP 误报 404）；D7 **禁无锚派单**（派单给①已核实锚材料②来源域白名单③来源形态要求）。
- ⚠️ **多解释不单选**：存在多个合理解释时读 `references/ach.md`，按 ACH 八步设法**证伪**，报告全部假设的相对可能性——禁止只报最可能的那个。
- ⚠️ **证据升格**：材料将作为结论依据／涉及自然人／可能对外时，读 `references/digital-evidence.md`（保全+验证+伦理最小化）。普通检索不套取证规范。
- `anysearch` 为默认实时搜索后端（仅访问 `api.anysearch.com`）；其 `extract` 只支持 HTML/文本/JSON/MD，PDF/图片等二进制不走 extract。
- 本地文件解析（PDF/DOCX/XLSX→文本）：用 Python（pymupdf/python-docx/pandas）直接解析。
- `bili-daily` 的合规红线：日请求 160 预警/200 硬停；字幕全文 24-48h 物理删除；不存 uid、不破解验证码、不碰 DRM、不商用、不公开发布（水印「个人学习研究·勿公开传播」）。
- 福利吧资源导航 → 独立技能 `fuliba-nav`；逆向 API 类（X 转 md danger 系列）在 `~/.agents/skills-archive/`，仅用户显式点名时手动恢复。
- `deep-research-pipeline` 阶段 1 用 `ask_user_question` 问 2-3 个选择题（可跳过）；报告落 `reports/`。

## 7 自进化（每次用完顺手记一条）

本技能带自进化回路：记录每一次「选错通道 / 抓取失败 / 来源被否」，累积后自动提示改进。

```bash
# 记一条（意图短语 + 走的子技能 + 结果，脱敏，不写隐私）
python scripts/self_evolve.py log --intent "<老板原话短语>" --subskill "<子技能名>" \
  --outcome <ok|wrong-route|fetch-failed|source-rejected> [--note "<一句话>"]

# 看学到了什么 / 体检 / 生成改进建议
python scripts/self_evolve.py report
python scripts/self_evolve.py doctor
python scripts/self_evolve.py suggest        # 输出可直接执行的改进清单
```

- **学习只做有界重排**（±30% 封顶，14 天半衰期）：不自动改 SKILL.md、不自动装技能、不覆盖显式点名。
- `suggest` 给出的是**候选改进**（如「browser-cdp 连续 3 次被反爬拦截 → 考虑升级为默认」），**须老板点头才落地**。
- 可审可撤：`report` 看、`prune <subskill>` 剔、`reset` 全清。
- 与 `skill-use-router` 的 `learn.py` 分工：那边学「该用哪个技能」，这边学「这个技能内部该走哪条通道」。
