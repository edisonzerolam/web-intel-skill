# channel-notes — 抓取通道边界知识表（本机一手实测）

> **用途**：抓取/搜索前先查本表，**第一次就走对通道**，不再靠 2-3 轮试错。
> **维护纪律**：本表条目全部来自真实踩坑（来源会话与日期见各行）；**站点边界会随时间漂移**——使用时以最近一次实测为准，踩到新坑/发现旧行为变了，**顺手更新本表并 `scripts/self_evolve.py log` 一条**。
> **格式**：`站点/模式 → 正确通道 → 失败特征 → 验证命令`
> 建表：2026-10-04（调研报告《web-intel 改进方向》P0-1，覆盖 12 条一手踩坑）

---

## 1. 通用抓取通道选择（按失败特征查）

| 失败特征 | 根因 | 正确通道 | 验证命令（实证） |
|---|---|---|---|
| `web_fetch` 只返回页面标题、正文为空 | JS/SPA 渲染页 | ego 浏览器：`ego_navigate` → `ego_js` 取 `document.body.innerText` | workbuddy.ai / xiaohuanxiong.com / lobsterai.youdao.com（2026-10-03） |
| SPA 页 ego_http 报 `TypeError: Failed to fetch`、Invoke-WebRequest 报「基础连接已经关闭」、**curl 输出 0 字节** | 站点 TLS/前端门槛，浏览器直连也不行 | **python urllib.request + User-Agent: Mozilla/5.0** | trae.cn/pricing 成功取 134,379 B HTML（2026-10-03） |
| `web_fetch` 报 `cross-origin redirect` | 不自动跟随跨域重定向 | **对重定向后的目标域直接重发**（.io→.cn、http→https） | agent.minimax.cn→platform.minimax.cn（2026-10-03） |
| `anysearch extract` 报 `Unable to extract content from the URL` | 服务端抽取器对该站失效 | gitbash `curl -s -L -A Mozilla --max-time 45` 直连后正则去 script/style | dg.gov.cn 政府公开页（2026-10-03 踩坑、**2026-10-04 复现**） |
| 搜索摘要 URL 打开后内容对不上 | **摘要 URL 会失效/被顶掉**（站点改版） | 摘要 URL 必须实际抓取验证；失效则标「未核验」不采信 | meitachain.com 摘要页变公司页（2026-10-03） |
| z.ai 定价页空 | cookie 弹窗遮挡 | 真浏览器先点 Accept all | 2026-10-04 实测 anysearch extract 已能直接取到（边界漂移例） |
| `anysearch extract` 返回 103 字节（纯 `API Error: Unable to extract content`） | 服务端抽取器对该站失效 | **gitbash `curl -s -L -A "Mozilla/5.0 (Windows NT 10.0; Win64; x64)" --max-time 45` 直连** | bmj.com / zhihu.com/question / nhc.gov.cn / gov.cn / nhsa.gov.cn 均命中（2026-10-06）；nhsa/gov 直连后需 `python -X utf8` 显式解码读中文 |
| `site:xiaohongshu.com` 检索全部返回「No information is available for this page」 | **站点不支持外部 site: 检索**，不代表站内不可抓 | ⚠️ **修正（2026-10-06）**：改用 ego 真浏览器打开**站内搜索页** `https://www.xiaohongshu.com/search_result?keyword=<kw>`，`document.body.innerText` 可直接取到**笔记标题+作者+日期+点赞数**列表（实测 14 组关键词全部成功） | 慢病调研 |
| **小红书笔记正文+评论** | 需要 `xsec_token` | ①搜索页 `a[href*="/search_result/"]` 里带 `/search_result/<noteId>?xsec_token=<token>` ②goto `https://www.xiaohongshu.com/explore/<noteId>?xsec_token=<token>&xsec_source=pc_feed` ③正文取 `#detail-desc`（或 `.note-content`/`.desc`），评论在 `body.innerText` 的「共 N 条评论」之后 | 2026-10-06 实测 **8 篇正文+高赞评论全部成功**；**注意**：`/explore/<id>` 不带 token 会失败；点赞数不得当人群比例 |
| `mp.weixin.qq.com` 文章直链抓取 | 多数已失效 | 直链实测 3 条中 2 条返回「该内容已被发布者删除」「此账号已自主注销，内容无法查看」 | 2026-10-06 |
| **公众号正文抓取（经搜狗 302）** | 搜狗对 curl 有 antispider，**必须走真浏览器** | ①`https://weixin.sogou.com/weixin?type=2&query=<kw>` 取 `.news-list li h3 a` 的 `/link?url=...` ②ego `page.goto('https://weixin.sogou.com'+href)` 自动 302 到 `mp.weixin.qq.com/s?...` ③正文取 `#js_content`（或 `.rich_media_content`） | 2026-10-06 实测 6 条中 **4 条成功**；失败形态：「参数错误」（签名过期）、「环境异常，完成验证后即可继续访问」（触发风控） |
| **百度贴吧** | 免登录可读 | ①列表 `https://tieba.baidu.com/f?kw=<吧名>&ie=utf-8`，`body.innerText` 取帖子标题+作者+回复数 ②正文 `https://tieba.baidu.com/p/<帖子id>`，取「全部回复」之后的内容 | 2026-10-06 实测糖尿病/高血压/痛风/糖尿病肾病/糖尿病足/高血脂 6 个吧全部成功；**注意**：`展开 N 条回复` 与「登录后查看全部评论」需登录，评论样本不完整；**吧内广告/药托含量高**，引用须剔除 |
| **B站搜索页** | 免登录可读标题层 | `https://search.bilibili.com/all?keyword=<kw>`，`.bili-video-card` 取标题+UP主+播放量 | 2026-10-06 实测成功；**字幕/评论需走 `bili-daily` 子技能** |
| **需登录/不可用平台**（2026-10-06 本机实测） | — | 豆瓣搜索返回「获取搜索结果失败: 错误103」；知乎搜索页 302 到 `zhihu.com/signin`；微博搜索 302 到 `passport.weibo.com/sso/signin`；抖音正文未渲染（仅页脚）；**Reddit `CDP request timed out: Page.navigate`（本机网络不通）** | 遇到这些站点**不要反复重试**，直接标注「未覆盖」或改用 anysearch 垂直域兜底 |
| `nhc.gov.cn` 政策 PDF 抓下后是**图片版**（无文字层），extract/curl 都取不到正文 | 扫描件/图片型 PDF | **改用同文号的转载页/权威媒体转引页交叉印证数值**，并在报告中显式标注「原文 PDF 无文字层，数值经多路转引印证」 | 2026-10-06《成人高尿酸血症与痛风食养指南（2024年版）》，两路转引一致 |
| 学术期刊官网 PDF（如 zhgxyzz.xml-journal.net）`anysearch extract` 不适用 | extract 不支持 PDF | **gitbash `curl -s -L -A Mozilla` 下载 → Python `pymupdf`(fitz) 提取文字**，中文正常 | 2026-10-06 中华高血压杂志家庭聚集性高血压论文，4741 字全文提取成功 |
| `huggingface.co` 报 `URL hostname resolves to a non-public IP address`（本机 DNS/宿主被拦，非站点问题） | HF 域名不可达 | 改走**上游官方仓库**：GitHub 网页正文会被截断 → 用 `raw.githubusercontent.com/<org>/<repo>/main/README.md` 与 `.../LICENSE`（模型卡正文与许可证原文同效）；ComfyUI 侧用 `docs.comfy.org/<path>.md`（Mintlify 支持 `.md` 后缀直出纯正文，绕开 SPA 截断） | 2026-10-06 文生图调研实测；`.md` 后缀法同时解决了 docs.comfy.org 正文被导航栏挤掉的问题 |
| `reddit.com` 帖子正文 `anysearch extract` 只返回 "Prove your humanity"（Cloudflare 人机校验） | 服务端抽取器同样被拦 | 标「未取得正文」，**不要**把搜索摘要里的数字当实测引用（摘要缺上下文，易误读）；换同主题的官方博客/GitHub issue 替代 | 2026-10-06 r/StableDiffusion 7800 XT 帖；同批改用 AMD ROCm 官方博客与 ComfyUI issue 拿到可用数字 |
| 内置 `web_search` 查「XX 官网 + 社区实测」类技术问题，返回百度图片/ZOL/词典/汽车站噪声 | 该后端对技术类长查询的召回质量差 | 技术调研直接走 `anysearch_search`（`scripts/anysearch_cli.py`，前置 `NO_PROXY=api.anysearch.com`）；仍失败再退回 gitbash curl / ego 浏览器 | 2026-10-06 实测：同一问题内置搜索全噪声，anysearch 首屏即命中 smeltcore / AMD 博客 / GitHub issue |
| `anysearch batch_search` 偶发 `Search service temporarily unavailable`（同一批内部分 query 失败） | 服务端限流/抖动 | **逐条 `search` 重试**或稍后重跑；不要因一次 batch 失败判定「无资料」 | 2026-10-06 文生图调研，3 query 中第 2、3 条失败 |

## 2. ego 浏览器（browser-cdp）稳定性守则

| 坑 | 规则 |
|---|---|
| **标签页复用/并发抢占会返回别站内容**（对 cline.bot/models 取到 GLM 页面文本；返回 URL 与实际 DOM 不符） | 抓取前用 `document.location.href` 校验域名；不依赖持久标签；关键数据用 curl 双通道对账 |
| `ego_script` 未打印 `@@DSH_RESULT@@` 时工具报 error 但 **stdout 仍含有效 JSON** | 读 stdout 内容判定，不凭工具 ok 标志 |
| `ego_script` 内直接 `fetch()` 报 `TypeError: fetch is not a function` | 包进 `page.evaluate(async()=>{await fetch(url)})` |
| 脚本含正则字面量（`/Accept all/i`）触发 `SyntaxError: Invalid regular expression flags` | 改 `new RegExp(...)` 或 `includes/indexOf` |
| `ego_click selector='text=X'` 报 matched 2 elements；`role:tab[name=...]` 报 matched 0 | 用 `ego_script` + `page.evaluate` 遍历 DOM 主动 click |
| `page.goto('x.com/pricing')` 返回错误站内容（如 qoder.com 返回 ClinePass 正文） | `ego_space_open` 新建空间再 navigate，或换备用域（qoder.cn） |
| 资源发现：相对路径 goto 报 `Cannot navigate to invalid URL` | 资源清单用 `performance.getEntriesByType('resource')` 发现 + 完整 URL 访问 |

### 站点可达性黑名单（本机网络环境，2026-10-03 实测）

| 站点 | 状态 | 替代 |
|---|---|---|
| linux.do | ❌ CDP `Page.navigate` 超时（ego-browser exited code 1） | 镜像 `global.v2ex.co` + `sov2ex` 检索 API |
| nodeseek.com | ❌ 同上 | 同上 |
| Yunwu / SSSAiCode / packyapi.com | ❌ 不可达 | 字段标「未核验」 |

## 3. B站（bili-daily）字幕与清单通道

| 坑 | 正确做法 |
|---|---|
| `view` 接口 `subtitle.list` 的 `has_url` **全为 False**（只有 lang 无 URL） | 不能用它预筛有无字幕；必须再调 `player/wbi/v2` 拿真实 subtitle_url |
| `player/wbi/v2` 只传 bvid 返回 `code=-400` | 先 `view` 拿 cid，再带 **bvid+cid+cookie** 请求 |
| `/x/space/wbi/arc/search` 无签名报 `-799/-403` | **wbi 签名必须带**：img_key+sub_key → MIXIN_KEY_ENC_TAB 重排 → md5（纯 Python 可实现）；清单交叉验证用 polymer feed/space 独立通道 |
| **「精简版」视频无字幕轨，完整版才有**（391 视频实测覆盖率 43%） | 同一内容常有两版；清单阶段不要按"缺字幕"剔除，先查有无完整版 |

### 字幕清洗（enrich_note LLM 通道）模型选择（391 篇实测，2026-10-04）

| 模型 | 实测表现 | 用否 |
|---|---|---|
| `deepseek-v4-pro`（sensenova 池） | 压缩 62%，保留口语风格，**零幻觉** | **首选** |
| `deepseek-v4.1-flash`（DSH 网关） | 忠实，压缩 34% | 次选（需要高压缩时选 pro） |
| `agnes-3.0-flash` | **会编造原文没有的整段内容**（"大厂"0 次却写出"从大厂逃离"整节） | **禁用于清洗** |
| 注意 | sensenova 池**无** v4.1-flash（403 token plan），有 v4-pro / v4-flash / kimi-k3 / glm-5.2 | — |

### 清洗质量规则（同批实测）

1. **标点恢复是关键杠杆**：funasr ct-punc（1159 字/秒）把文字墙变可读文本；不变式=仅插标点、去标点后字序列一致 → 可双指针精确对齐回字符级时间轴。
2. **忠实度门禁必须上下文判定**，词表包含会大量误伤：「总而言之」句首才判旁白（句中是连接词）；「大厂/抓手/躺平」会被正常使用（"抓手无寸铁"）→ 移出词表。修正后门禁从 47 篇未过收敛到 0。
3. **分块 bug（代码级）**：按时间锚点切块时单个锚点段超限不再切 → 61,004 字超长块 → 超 max_tokens → 无限重试。修复：单段超限按句切；max_tokens 按输入长度动态定。
4. **规则清洗天花板 ~9%**：口水词仅 0.3% 字数，噪声主体是 ASR 碎片化（段长中位 8 字）——语义级去废话必须 LLM。

## 4. 本机网络/环境基线

- Windows 系统代理对 SSL 握手有干扰：调 anysearch 前置 `export NO_PROXY=api.anysearch.com no_proxy=api.anysearch.com`（D6 已载）。
- 本地 PowerShell HTTP 客户端会**误报 404**：URL 存活判定用 curl 直连 / anysearch extract / ego 浏览器 Bing 交叉（D5 已载）。
- PowerShell `Get-Content/Set-Content` 会把 UTF-8 按 GBK 写坏（曾毁 SKILL.md 与 evolve.py）：**改技能文件一律用 write/edit 工具**。
- Windows 控制台 GBK：python 脚本打印中文/emoji 先 `sys.stdout.reconfigure(encoding='utf-8')`。
