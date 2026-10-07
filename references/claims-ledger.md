# Claims Ledger — 主张台账与三角验证

> 何时读：调研档位 ≥ medium，或需要交叉验证/事实核查时。吸收自 `Socialpranker/deepdive` §5（MIT），与本技能 research-discipline 的★A-D 四档并存互补。

## 为什么需要

研报里最常见的失效不是"没查到"，而是**一条断言看起来有多个来源，其实都源自同一篇稿子**。台账把每条断言独立成行，强制暴露来源的真实结构。

## 台账格式（CSV 或表格，长调研落 `claims.csv`）

| 列 | 填什么 |
|---|---|
| `claim_id` | C001, C002… |
| `claim` | 一句可证伪的断言（不写"可能""大概"） |
| `source_ids` | s01, s07, s12（对应 sources 索引） |
| `types` | 来源类型：official / media / academic / vendor / community |
| `roots` | 根域（去重后，如 `gov.cn`、`xinhuanet.com`） |
| `paths` | 发现路径：search / citation / pivot / api |
| `confidence` | high / medium / low（由下方规则定，不凭感觉） |
| `caveat` | `vendor` / `self-reported` / `disputed:sNN` / `-` |
| `as_of` | 该事实的时点（数字类必需） |

## 三角验证规则

`triangulated`（可取 high）需**同时**满足：

- ≥3 个来源 **且**
- ≥2 种类型 **且**
- ≥2 个根域 **且**
- ≥2 条发现路径

否则降级：

| 不满足 | 判定 | 置信上限 |
|---|---|---|
| 仅一种类型 | `single-type` | medium |
| 仅一个根域 | `single-root` | medium |
| 仅一条发现路径 | `single-path` | medium |
| 无一手/官方源 | — | medium |
| 带 `vendor`/`self-reported` caveat | — | medium |
| `disputed` 且无仲裁者 | `contested` | low |

## 少数派保护

未平息的 `dissent`（异议）若来自 Primary 源或可信度 ≥4 的源，**无论多数派如何都标 `contested`**，报告中两种 position 都要写出来。多数派压倒少数派是研报最隐蔽的失真来源。

## 数字专项

所有进入正文的数字另落 `numbers.csv`：

- `verbatim` — 原文照抄，附 source_id
- `derived` — 推算得出，**必须写 formula + inputs**
- `share` — 占比类，注明分母

没有 `as_of` 的数字不进 memo/结论段。

## Gap 补搜

对非 `triangulated` 的断言发起定向补搜，**最多 2 轮**；仍不足则标 `data-insufficient` 并在 Open Questions 里写明——不要靠措辞软化来掩盖证据不足。

## 与★A-D 四档的关系

四档解决"这个源靠不靠谱"，台账解决"这些源加起来够不够"。两者都要过：★A 单源仍是 `single-source`，不能取 high。
