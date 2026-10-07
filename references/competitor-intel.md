# 竞品调研 SOP

> 何时读：竞品调研 / 竞品分析 / competitive intelligence 类任务。
> 吸收自 ClawHub 榜单：`/qqyule/seo-competitor-analysis`(11958dl)、`/aaron-he-zhu/competitor-analysis`(11392dl)、`/findhappy7/competitive-intelligence`(1980dl)、`/lingyu9495-source/competitor-analysis-7steps`；以及 GitHub `Socialpranker/deepdive` 的 claims-ledger 机制。
> 证据等级：竞品框架内容取官方 summary（★C），未取一手全文，落地时以本文件为准。

## 七步框架

| 步 | 动作 | 产出 | 常见坑 |
|---|---|---|---|
| 1 | **定范围** — 竞品是谁、比什么、为谁比、什么时点 | 一页 scope，含排除项 | 竞品名单凭印象圈定，漏掉真正的对手 |
| 2 | **设维度** — 3-6 个维度，每个维度有可观测指标 | 维度表 + 指标定义 | 维度不可观测（如"品牌力"不定义口径） |
| 3 | **分源采集** — 按 `source-dispatch.md` 分派 | 原始材料落 `_raw_*` | 全走搜索引擎，拿一堆二手转述 |
| 4 | **标准化对比表** — 同一指标同一口径同一时点 | `compare.csv` | 口径不一致硬比（如含税/不含税混用） |
| 5 | **交叉验证** — 按 `claims-ledger.md` 记台账 | `claims.csv` | 多源实为同源（转载链） |
| 6 | **商业洞察** — 差异点 → 对我方意味着什么 → 可选动作 | 洞察段 | 停在" describe "，没说"所以呢" |
| 7 | **成稿** — 结论前置 + 差异化话术 | 报告落 `deliverables/<topic>/` | 结论埋在末尾 |

## 对比表口径纪律

每个指标必须写清四件事，缺一不可比：

1. **定义**（这个数字是什么）
2. **口径**（含什么不含什么）
3. **时点**（as_of）
4. **来源**（条级 source_id）

口径不一致时**宁可留空**也不要硬填——留空是 `data-insufficient`，硬填是错误结论。

## 差异化话术（第 7 步产出）

每条话术三件套：

- **我方事实**（带 source_id + as_of）
- **对方事实**（带 source_id + as_of）
- **差异结论**（一句话，不给"看情况"这种废话）

## 反模式

- 只列功能对比表，没有商业含义 → 补第 6 步
- 竞品数据全来自对方官网/宣传稿 → caveat 标 `vendor`，置信上限 medium
- 拿 2 年前的数据当现状 → 强制 `as_of`
- 结论写"各有优劣" → 重写，必须给出适用条件或选择建议

## 长任务纪律

竞品调研常跨多轮：档位 ≥ medium 时写 `state.md`，**每轮重写不累积**（Known / Gaps / Next 三段，≤6KB），按它规划而不是翻历史对话。
