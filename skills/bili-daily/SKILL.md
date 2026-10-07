---
name: bili-daily
description: 视频平台内容洞察子技能（web-intel 内嵌，2026-10-02 并入）。B站字幕抓取（各分类 top20/关键词榜/内容理解/趋势周报/关注订阅/ASR 兜底/观看实证）+ 爱奇艺元数据文本信号。经 /web-intel 路由进入；需要 B站/哔哩哔哩/视频字幕/热榜/关键词榜/UP 订阅/视频内容理解时由 web-intel 加载本文件。
version: 2.1.0
---

# B站字幕抓取与每日热门洞察（bilibili-daily-insight）

> **归属（2026-10-02）**：本技能是 `web-intel` 的 workflow 层子技能，位于
> `skills/bili-daily/`，经 `/web-intel` 路由进入。
> **部署位置**：把本仓库的 `web-intel/` 放进技能目录（例如 `~/.agents/skills/web-intel/`），
> 本技能即落在 `~/.agents/skills/web-intel/skills/bili-daily/`。
> **本地凭据**：B站登录 cookie 需自备（复制 `cookies.txt.example` 为 `cookies.txt` 并填入
> 自己的 `SESSDATA` / `bili_jct`），**不随仓库分发**。
> **新工作一律走 web-intel 入口**。

B站视频**字幕**抓取 + 每日热门洞察（分类榜/关键词榜/内容理解）。基于团队对抗审判 TC-20260811-BILI（字幕路径）与 TC-20260811-BILI2（功能扩展，终审 V1-V11）。

## 何时使用

用户要求「B站字幕」「每日热门视频在说什么」「各分类热门榜」「热词/关键词榜」时启用。

## 核心事实（2026-08 实测）

1. **字幕列表对游客隐藏**（/x/player/wbi/v2 需 SESSDATA 登录态）；subtitle_url 带 auth_key 时效签名（过期 403）
2. **分类榜 ranking/v2?rid=X 带登录 cookie 无需 wbi 签名**（实测 code=0，~95 条/区）——登录 cookie 即通行证
3. 全站 popular 游客零签名直取；弹幕/评论不在本技能范围（用户不需要）

## 使用流程

```bash
# 1. 扫码登录获取 cookie（6 个月一次；纯 Python，零外部软件）
python bilibili_login.py --out cookies.txt

# 2a. 完整每日管线（分类 top20 + 全站 + 关键词榜 + 内容理解 + 延伸统计）
python bili_daily_v2.py --cookies cookies.txt --out-dir out/

# 2b. 仅字幕文件（单视频或批量）
python fetch_subtitles.py --bvid BV1xx411c7mD --cookies cookies.txt --out-dir subs/
python crawl_bilibili_daily.py --ps 20 --cookies cookies.txt --out-dir subs/

# 2c. 单视频转录（字幕优先多级降级 → frontmatter Markdown；见下方融合章节）
python transcribe_video.py --bvid BV1xx411c7mD --cookies cookies.txt --out-dir out/transcripts
python enrich_note.py out/transcripts/    # 转录产物加工（摘要/要点/标签）
```

## 功能与算法（TC-20260811-BILI2 终审 V1-V11）

| 功能 | 实现 |
|------|------|
| 各分类 top20 | 9 分区（动画/音乐/游戏/娱乐/知识/生活/鬼畜/舞蹈/科技）ranking/v2 带 cookie 直取 |
| 跨日去重 | seen_bvids.json（bvid→首见日期，长期留存）；重复上榜则 **pass 彻底跳过**；同日跨分区并集去重 |
| 关键词综合排序 top20 | 关键词级主榜 score=0.7A+0.3B（A=Σ词权×视频热度 归一化，B=覆盖数奖励），n_k≥2 门槛；视频级副榜=每热词 top3 视频 |
| 字幕预处理 | difflib 相似度>0.85 相邻合并 → 叠词压缩 → jieba.posseg 词性白名单（n/nr/ns/nt/nz/v/vn/eng）+ 口语停用词 → TF-IDF（DF 累计表稳定日间抖动） |
| 内容理解（启发式） | 时间轴分段（gap>60s 且段长≥15s，封顶 5）→ 段内关键词密度要点（带时间戳）→ 梗概=标题×字幕共现打分 top1 句 → 口语化情感词典（否定词翻转，仅参考）；LLM opt-in 默认关 |
| P0 延伸 | corpus.jsonl 统计持久化（无全文字段）→ 7/30 日关键词趋势；Obsidian 零片段笔记（frontmatter+改写摘要+时间戳回链，仅本地 vault）；每日简报私密模板 |

## 合规约束（强制，V7/V8/V10）

- **预算**：日请求 160 预警（字幕候选 N 降级 40→25）/ 200 硬停；串行 ≥1.5s；412/429 当日停止
- **数据生命周期**：字幕全文 24-48h 物理删除（任务结束即清）；元数据 ≤30 天；bvid+统计语料长期
- **引用检测**：连续 ≥15 字与原文重合判为引用（超配额拒绝输出）；摘要为改写式
- **红线**：不存 uid、不破解验证码/身份轮换、不碰 DRM、不商用、不公开发布（报告水印「个人学习研究·勿公开传播」）
- 参考实现：Bilibili-Obsidian-Clipper（登录态直取字幕实证）；终审案卷 deliverables/trial/2026-08-11/TC-20260811-BILI2/

## 扩展模块（TC-20260811-BILI3 终审 V1-V8）

```bash
# 周报/月报（纯本地，零请求；corpus 累积 ≥3 天出趋势榜）
python weekly_report.py --corpus corpus.jsonl --out weekly.md [--days 7|30]

# 订阅监控（subs.json 用户手写：up_name 必需 + mid 锚点，权限 600，禁止自动回写）
python bili_daily_v2.py --cookies cookies.txt --out-dir out/ --subs subs.json

# ASR 兜底（无字幕轨视频本地转写，日限 5，需 pip install faster-whisper）
python bili_daily_v2.py --cookies cookies.txt --out-dir out/ --enable-asr
```

| 模块 | 实现要点（终审裁决） |
|------|---------------------|
| `asr_fallback.py` | faster-whisper small/int8；**主通道=playurl 接口 dash.audio（带 cookie 实测可用，每视频 1 请求）**，yt-dlp fallback；白名单三条件硬失败；asr_state.json 占位记账+失败回补；音频即删+文本 24-48h 删；音乐/鬼畜/舞蹈三区 ASR 不参与关键词主榜（×0.7 降权） |
| `subs.py` | search/type 带 cookie 查新（实测 arc/search -799 不可用）；author 精确匹配；7 日零匹配提示；subs.json 哈希不变断言（禁回写）；日志脱敏单一入口；固定前 5 UP、池 ≤20/日 |
| `weekly_report.py` | 7/30 日滚动窗口；词频双口径相对频率环比；三榜（新词=first_seen 落窗+上周零提及 / 飙升 / 消退，min_days=2、min_count=3、topN 周15/月30）；月报 ≥21 日保口径；缺口阈值 max(3, 0.2×days)；水印双处；规则模板解读无 LLM |

**实测记录（2026-08-11）**：DASH 音频主通道 playurl 接口带 cookie 免 wbi 签名返回 dash.audio，26.3MB 音频流下载验证通过；player/wbi/v2 响应**不含** dash 字段（初版假设被实测否定，已改用 playurl）。

## 单视频转录管道（融合 chubbyskills v0.13.0，2026-09-28）

> 来源：github.com/chubbyguan/chubbyskills（MIT，v0.13.0）的 `bilibili-transcribe`
> （字幕优先多级降级 + yt-dlp 通道 + SenseVoice-Small 引擎 + VTT/SRT 解析 +
> markdown_schema_v1 frontmatter 协议）与 `content-enrich`（摘要/要点/标签加工协议）。
> 已吸收进 `transcribe_video.py` / `enrich_note.py`；保留本技能登录态 API 主通道与严格合规框架。

```bash
# 单视频转录（T1 字幕优先；cookies 为登录态，必需参数）
python transcribe_video.py --bvid BV1xx411c7mD --cookies cookies.txt
python transcribe_video.py --url https://www.bilibili.com/video/BV1xx411c7mD/ --cookies cookies.txt --out-dir out/transcripts
# 批量（每行 BV 或 URL）；--engine sensevoice 强制 SenseVoice；--no-api-subtitle 跳过 T1；--budget 计入 req_budget
python transcribe_video.py --list urls.txt --cookies cookies.txt --budget

# 产物加工（启发式零外发默认；--llm 走 DeepSeek，内容外发须显式开启）
python enrich_note.py out/transcripts/           # 批量目录
python enrich_note.py xxx.md --llm              # LLM 加工（需 DEEPSEEK_API_KEY）
```

> ⚠️ **LLM 清洗/去废话开工前必读 `references/subtitle-cleaning-pipeline.md`**：模型选择表（v4-pro 首选 / **agnes 禁用——会编造原文没有的整段**）、忠实度门禁上下文判定规则、标点恢复双指针对齐不变式、分块防无限重试（单锚点超限按句切 + max_tokens 动态定）、精简版/完整版字幕坑。全部来自 391 篇字幕实测（2026-10-04）。

| 通道 | 说明 | 实测（2026-09-28） |
|------|------|-------------------|
| T1 API字幕 | /x/player/wbi/v2 带登录 cookie；复用 fetch_subtitles | ✅ 29661 字/视频，与 T2 字数一致（双通道互验）。**命中率实测 64.2%（77/120）**，见下 |
| T2 yt-dlp字幕 | --write-subs --write-auto-subs，带 cookie；VTT/SRT 解析兼容 | ✅ ai-zh 自动字幕命中；**免登录实测 412**（wbi 游客签名被拦，与既有结论一致） |
| T3 SenseVoice | funasr iic/SenseVoiceSmall（chubbyskills 引擎），DASH 音频；**默认走 ROCm GPU 常驻 worker** | ✅ **端到端跑通**（4442 字/时政视频；rtf≈0.037≈**27×实时**，30.5s 音频 1.1s 转完；纯音乐视频正确识出 🎼） |
| T4 faster-whisper | CTranslate2 CPU（ctranslate2 4.8.1 + faster-whisper） | ✅ **端到端跑通**（同视频 4291 字；逐行分句输出） |

### T1 命中率实测（2026-10-07，推翻此前「100%」断言）

口径 = `t1_api_subtitle()`：**有字幕轨 且 正文 ≥50 字** 才算命中。

| 样本 | 命中 | 命中率 | 未命中原因 |
|------|------|--------|-----------|
| `--n 30 --seed 42` | 16/30 | **53.3%** | 无字幕轨 / view失败 / 正文过短 |
| `--n 120 --seed 7` | 77/120 | **64.2%** | **无字幕轨 39**、view失败/无cid 1、正文过短 3（27/4/13 字） |

命中语言**全部 ai-zh**。→ **约 1/3 视频没有可用字幕轨、必须走 T3/T4 本地转写**，
T3 是常用路径而非边缘兜底。复测脚本 `measure_t1_hit.py`，明细 `t1_hit_result_n120.json`。

### T3 GPU 加速（2026-10-07 接入）

主环境 torch 是 CPU 版（无 GPU），ROCm 栈在独立 venv，**两者无法同进程共存**，
故走**子进程 + 行分隔 JSON 协议**的常驻 worker：

| 文件 | 角色 |
|------|------|
| `t3_rocm_worker.py` | 在 `D:\rocm101-venv` 内运行；加载模型一次后循环收活；库日志全部改道 stderr；支持 `with_timestamps`（段级时间戳）；**双引擎**（`sensevoice` 启动预热 / `whisper_turbo` 懒加载），按请求 `engine` 字段分派 |
| `t3_rocm_client.py` | 主环境侧客户端；`T3RocmWorker` 单例 + **`WorkerPool` 池**、超时/重启/优雅退出、永久失效标记；`transcribe_full(..., engine=)` 取完整响应 |
| `test_t3_rocm.py` | 32 条断言（含 9a–9g 段级时间戳）+ `--red` / `--red-ts` 可红性自检 + `--no-gpu` |
| `test_t3_pool.py` | 25 条断言（池语义 + 真并发 + 防串话 + 合规限速）+ `--red` 可红性自检 |
| `test_asr_gpu.py` | 14 条断言（P1：`asr_fallback` GPU 优先/CPU 兜底契约）+ `--red` 可红性自检 |
| `test_t3_meta.py` | 21 条断言（P2b：标签词表对齐 + 段级标签对齐不变式 + meta 契约）+ `--red` 可红性自检 |
| `test_t5_turbo.py` | 17 条断言（P2：引擎分派/开关降级/单例与池透传/默认路径不变）+ `--red` 可红性自检 + `--no-gpu` |

开关：`--t3-device-mode {auto,rocm,cpu}`（默认 auto：先试 GPU 再回落 CPU），
或环境变量 `WEBINTEL_T3_DEVICE_MODE`。产物 frontmatter 的 `transcriber` 字段
**如实记录实际通道**（`SenseVoice-Small(GPU)` / `SenseVoice-Small`）。

**为什么必须常驻**：就绪固定开销 ≈10–27s（import torch+funasr + 模型加载 +
首次 MIOpen 内核编译），而单次推理仅数秒。批量（`--list`）时第 2 个视频起复用，
实测 4 视频批量：**GPU 就绪行只在第 1 个视频出现一次**，后续直接转写。

**跨设备一致性**：同一次采样下 GPU 与 CPU 输出**逐字相同（相似度 1.0000）**。
注意 SenseVoice 本身**非逐字确定**——同一音频重复推理会在**固定位置**（759 字样本的
第 320、473 字）出现 1 字抖动（相似度 0.9987），**CPU 侧在完全相同位置同样抖动**，
属模型/浮点累加性质，不是 ROCm 引入的。

**隔离基准（2026-10-07）**：⚠️ 最初测得的「CPU 合计 172.0s / GPU 99.1s、**2.2x**」其 CPU
基线**是机器带载时测的，已作废**。空载清洁复测（同一 300.7s 音频、n=3 取中位）：

| 路径 | 墙钟 | 推理 | xRT | 输出 |
|------|------|------|-----|------|
| ROCm-GPU | 19.29s | **8.43s** | 35.7x | 822 字 |
| 主环境-CPU | 23.61s | **11.59s** | 25.9x | **822 字（逐字相同）** |

→ **真实倍率 1.37x（推理）/ 1.22x（端到端）**。GPU 的价值主要在 **CPU 秒数约减半**
（GPU 33.5s 含加载 vs CPU 推理即 57.0s），不是墙钟数量级。生产函数佐证：
`t3_sensevoice()` 自报 `time_escape=14.14s`、`rtf_avg=0.047`。

### T3 并行 worker 池（2026-10-07，批量的真正杠杆）

`--list` 批量模式下 T3 默认起 **2 个常驻 GPU worker** 并行（`--t3-workers N` 或
`WEBINTEL_T3_WORKERS` 可调，1 = 串行）。DirectML 的「同一 session 不可并发 Run」
硬约束（实测 4 线程 → `GPU device removed` 全崩）在 **ROCm/PyTorch 上不存在**：

| 并发 | 墙钟 | 成功 | 音频合计 | 聚合吞吐 |
|------|------|------|---------|---------|
| N=1 | 19.1s | 1/1 | 301s | 15.7x |
| N=2 | 21.5s | **2/2** | 496s | **23.1x** |
| N=4 | 25.7s | **4/4** | 611s | **23.8x** |

N=2 已吃到 97% 收益故默认 2，上限 8；单 worker 显存 1.2–1.7GB（共 16GB，余量充足）。
**冷启动代价**：2 个 worker 同时首次加载模型且 MIOpen 内核缓存为空时，就绪实测
**46.6s**（单 worker 11.6s）；缓存热后降到 **11.9 / 12.6s**。小批量（≤2 条）会被冷启动
吃掉收益，大批量才划算。端到端实测（4 条视频、含网络 T1/T2 尝试与音频下载）：**58s 完成 4/4**。

**合规（必须保留）**：`fetch_subtitles.pace()` 已加互斥锁 —— 并行下多线程同时调
`pace()` 时，若无锁则各线程都会读到「距上次请求已够久」而同时放行，
**全局 1.5s 最小间隔失效**，直接违反 B站限速红线。加锁后语义不变
（`test_t3_pool.py` 断言 7a/7b 锁死：4 线程并发总跨度 ≥ 4.5s）。

**红线**：`WEBINTEL_T3_DEVICE=cpu` **不要在 ROCm venv 内跑** —— 同一 300s 音频
13.5 分钟烧掉 **8052 CPU 秒**仍未完成（约 140x 劣化）。CPU 兜底必须留在主环境。

### P1：段级时间戳 + `asr_fallback` 并入 GPU（2026-10-07）

**第二处 CPU 转写点**：`asr_fallback.transcribe_audio()`（`bili_daily_v2.py --enable-asr`
的兜底路径）原本硬编码 faster-whisper small/int8 CPU，与 T3 是**两套独立引擎**。
现改为 **GPU 优先、失败回落 CPU**：

| 改动 | 说明 |
|------|------|
| worker 协议 | 新增 `with_timestamps`；`sentence_timestamp=True` 时返回 `segments=[{from,to,content}]` |
| 时间戳口径 | funasr 的 `sentence_info.start/end` 是**毫秒**，已 `/1000.0` 换算为**秒**，与 faster-whisper 的 `s.start/s.end` 对齐 |
| 消费方契约 | `bili_daily_v2.py:471` 直接读 `s["from"]/s["to"]` 写 `seg_ts`，`:491` 送 `merge_dups` —— **单位错会毁掉整条时间轴** |
| 回滚 | `WEBINTEL_T3_DEVICE_MODE=cpu` 彻底绕开 GPU；`model=` 显式传入时跳过 GPU（打桩语义） |

实测（300s 音频）：`with_timestamps=True` → **91 段**，`from` 单调不减，
段尾 `299.98s`（音频 300.0s，**证明秒级口径正确**），正文相似度 **0.9987**（加时间戳不改变正文）。
`transcribe_audio()` 走 GPU 返回 91 段；`prefer_gpu=False` 走 CPU 返回 50 段，两者 key 集合完全相同。

**顺带修掉的两个真 bug（P1 暴露，均已回归锁定）**：

1. **`NO_PROXY` 含 `[::1]` 让 CPU 兜底完全不可用**：httpx 解析代理白名单时抛
   `InvalidURL: Invalid port: ':1]'`，faster-whisper 按模型名加载即崩。
   `asr_fallback._sanitize_proxy_env()` 把 `[::1]` 归一为 `::1`（语义不变）。
2. **`WhisperModel("small")` 联网解析模型导致间歇性失败**：抛
   `RemoteProtocolError: Server disconnected without sending a response.`
   —— 同一用例一次通过一次失败。改为 `_find_local_whisper()` 命中本地缓存目录后
   完全离线（与 `transcribe_video._find_local_model` 同口径）。

### P2b：SenseVoice 情感/事件标记结构化（2026-10-07）

SenseVoice 的输出**自带结构化标签**，形如

```
<|zh|><|NEUTRAL|><|BGM|><|withitn|>句1 <|zh|><|ANGRY|><|BGM|><|withitn|>句2 ...
```

`<|HAPPY|>` 等是**情感**（8 种），`<|BGM|>` / `<|Laughter|>` 等是**音频事件**（12 种）。
`rich_transcription_postprocess` 会把它们换成 emoji（😊🎼）混进正文，**换掉就再也统计不出来**，
所以必须在后处理**之前**解析。worker 现在恒返回顶层 `meta`（零额外算力，只多一次字符串切分）：

| 字段 | 含义 |
|------|------|
| `emotions` / `events` | 标签计数字典（如 `{"NEUTRAL":69,"ANGRY":1}` / `{"BGM":77}`） |
| `has_music` | `bool(events["BGM"])` —— 可用于「纯音乐视频」判定 |
| `music_ratio` | BGM 句数 / 总句数（0–1） |
| `emotion_segments` | `[{from,to,emotion,events}]`，**只保留有区分度的段** |

**★ 段级标签只能从顶层 `record["text"]` 取**：`sentence_info[i]["text"]` 已被 funasr
清洗、**不含任何标签**（2026-10-07 实测：77 条 `sentence_info` 的 `text`/`sentence` 全是纯句子）。
第一版实现正是踩了这个坑，`emotion_segments` 恒为空。正确做法是按 `<|withitn|>` 切分
（`raw.split("<|withitn|>")[1:]`），第 k 段携带的标签即第 k 句的情感/事件，
用 `enumerate(si_list)` 的**原始下标**取值以免跳过空 content 时错位。

**过滤规则**：`emotion in (None,"NEUTRAL")` 且事件只有 BGM 的段**不入列** —— 实测多数音频
每句都带 `<|BGM|>`，若全收进来列表等于全文、毫无信息量；音乐覆盖改由 `music_ratio` 表达。
该音频 77 句 → `emotion_segments` 只剩 **8 条**（1 条 ANGRY + 7 条 EMO_UNKNOWN）。

**契约边界**：meta 走**独立的顶层字段**，**不动 `segments` 的 `{from,to,content}`** ——
P1 的 `test_asr_gpu.py` 断言 7a 要求 GPU 与 CPU 的段 key 集合完全相同。
`t3_sensevoice_rocm()` / `t3_sensevoice_dispatch()` 仍是原契约（只返回文本 / 2 元组），
要 meta 用 `t3_sensevoice_rocm_meta()` / `t3_sensevoice_dispatch_meta()`（3 元组）。
`build_markdown(..., audio_meta=...)` **非空才写**，既有产物格式不变。

**时间戳成本**（配对基准，300s 音频、GPU 常驻、交替各 3 轮）：
不带 **4.63s**(中位) vs 带 **4.78s**(中位) → **+3.2%**，在噪声量级内，故 `transcribe_full`
在 meta 通道**恒开** `with_timestamps=True`。

**词表对齐有断言**：`test_t3_meta.py` 断言 1b 直接从 funasr 源码读 `emoji_dict`，
要求 `_EMOTION_TAGS | _EVENT_TAGS` 完全覆盖（上游新增标签会立刻变红）。


### P2：whisper-turbo 质量兜底（2026-10-07）

降级链由 `T1 → T2 → T3 → T4` 变为 **`T1 → T2 → T3 → T5 → T4`**：

| 通道 | 引擎 | 何时用 | 产物 `transcriber` |
|------|------|--------|-------------------|
| T3 | SenseVoice-Small | 默认主引擎 | `SenseVoice-Small(GPU)` |
| **T5** | **whisper-large-v3-turbo** | **仅 T3 返回 None 后** | `whisper-turbo(GPU兜底)` |
| T4 | faster-whisper small | T5 也不可用 | `faster-whisper(降级)` |

**为什么只作兜底不作默认**（探针实测，300.7s 音频）：
- 速度：turbo 12.36s vs SenseVoice 4.78s（稳态）→ **慢 2.6x**
- 输出：**无标点**，且尾部会幻觉出超出音频时长的内容（实测出现 (298.48, 328.46)
  的「优优独播剧场——YoYo Television Series Exclusive」，而音频只有 300.7s）
- 显存：额外 **1.79GB**（懒加载 —— 只有真走到 T5 才付这个成本）

**复用同一套 worker**：T5 不另起进程，走 `transcribe_full(..., engine="whisper_turbo")`；
worker 侧 `load_engine()` 懒加载并缓存，`sensevoice` 启动即预热（ready 即代表 T3 可用）。
T5 **不产 meta**（无情感/事件标签），且未请求时不含 `segments`。

**两个必须记住的坑**（探针与 `test_t5_turbo.py` 都已锁死）：
- >30s 音频**必须** `return_timestamps=True`，否则 transformers 直接抛
  `ValueError: You have passed more than 3000 mel input features ...`。故 T5 恒开时间戳。
- `pipeline(..., local_files_only=True)` 会把该参数**泄漏进 generate_kwargs** →
  `ValueError: The following 'model_kwargs' are not used by the model: ['local_files_only']`。
  只能靠环境变量 `HF_HUB_OFFLINE=1` + `TRANSFORMERS_OFFLINE=1` 做离线（worker 顶部已设）。

**开关**：`--no-t5` / 环境变量 `WEBINTEL_T5_ENABLE=0`（默认开）；
`WEBINTEL_T3_DEVICE_MODE=cpu` 时 T5 自动跳过（只在 GPU 上有意义）；
`WEBINTEL_T5_MODEL` 可换模型 id（默认 `openai/whisper-large-v3-turbo`）。
回滚：`--no-t5` 即恢复 P2 前的 T3→T4 行为。


**部署要点（2026-09-28 实锤）**：① 模型统一放 **`C:\ms_cache`（纯 ASCII 路径）**——sentencepiece 等 C++ 扩展用 GBK 处理路径，用户目录「<user>」中文路径必崩（Python `exists=True` 但 C 层 Not found）；脚本顶部自动注入 `MODELSCOPE_CACHE=C:/ms_cache`。② funasr 装后 **torchaudio 被移除需重装**（`pip install torchaudio`，实测 torch 2.12.1+cp 配 torchaudio 2.11.0 共存正常）。③ 模型缓存 ~1.36G（T3 901M + T4 464M），modelscope 下载极快（~75MB/s）。

输出产物为 frontmatter Markdown（字段对齐 chubby `markdown_schema_v1`：
title/type/platform/source/bvid/author/created/transcriber/language/tags），
正文含水印「个人学习研究·勿公开传播」。DASH 下载独立实现（verify=False，规避本机
schannel 吊销检查离线导致的 CDN TLS 断连，等价 yt-dlp --no-check-certificates）；
音频即删（生命周期），转录文本随 out/ 24-48h 清理策略。

## 观看实证分析（TC-20260811-BILI5）

```bash
# 拉取观看历史并聚合（仅落盘聚合，原始零落盘；progress/stat 丢弃）
python watch_history.py --cookies cookies.txt --pages 10
# 实证分析：观看题材分布 + 关注×观看交叉 + 实证/综合双榜
python analyze_watch.py --out watch_analysis.md
```

| 模块 | 要点 |
|------|------|
| `watch_history.py` | history 接口（/x/v2/history，带 cookie）；**三层合规护栏**（ALLOWED_API 白名单断言 / sanitize 字段投影丢 stat·progress / 唯一 dump 入口）；输出 watch_ups.json（per-UP 聚合：n 去重视频数/total 次数/first·last_view_at/zones 官方分区/topics 子域） |
| `analyze_watch.py` | S4_emp=0.35·min(1,n/10)+0.25·min(1,d/8)+0.40·e^(−Δd/60)（n=0→0.30 标「窗口内未观测」）；n≥10 实证晋级；sign 融合（观看≥3 用官方分区）；**W<30 天参数冻结仅披露**；实证榜+综合榜+潜在新关注 |

**实测（2026-08-11）**：观看窗口仅 3 天（500 条，日均 ~167 条观看）——短窗口触发参数冻结；「妮可科技」在 sign 分析与观看实证双信号一致（交叉验证）。

## 主题化订阅（TC-20260811-BILI6 终审 V1-V8）

```bash
# AI 主题 UP 筛选（S1 sign + S2 观看交叉，0 请求；S3 投稿验证跨日执行）
python ai_ups_filter.py [--verify]

# 生成推荐配置（AI 组置前 + group 字段 + _baseline）→ dry-run → 应用
python apply_subs.py subs.recommended.json              # dry-run 变化摘要
python apply_subs.py subs.recommended.json --apply      # 正式应用（备份+基线比对+原子替换）

# 全局请求预算（跨进程统一 160 预警/200 硬停/412 blocked）
from req_budget import budget; budget.spend("subs"); budget.block()
```

| 模块 | 实现（终审裁决） |
|------|-----------------|
| `ai_ups_filter.py` | AI 词库 v1.0 冻结（强 20 词 S1=1.0 + 弱 11 词 S1=0.5 + `ai` 边界正则）；排除 E1 纯商务硬排除/E2 搬运软判定/E3 蓝 V 保留；T=0.5S1+0.2S2+0.3S3；核心 ≥0.6/候选 ≥0.4；S2 降级实现（watch_ups.json zones 科技分区→0.5，topics 空） |
| `apply_subs.py` | 四条件：物理隔离/dry-run 默认+--apply/基线比对（_baseline.sha256）+--force/备份滚动 7 份+os.replace 原子替换+自检回滚 |
| `req_budget.py` | 文件锁+日账本跨进程计数；160 预警（-1 返回）/200 硬停（SystemExit）；412/429 写 blocked 当日全停；by_channel 分账 |
| `history_snapshot.json` | 合规整改产物：593 观看标题→聚合快照（total/theme_share）后原件已物理删除 |

**subs.json group 白名单**（6 值，_legend 顶层键图例）：ai/tech/news/industry/politics/military；politics/military 默认敏感组（不进周报/导出/建议配置，日报折叠）。

## 关注动态订阅（订阅范围当天更新全覆盖 · 知识区重点）

```bash
# 拉取全部关注 UP 的最新投稿（1-3 请求覆盖 1103 个关注，替代逐 UP 搜索）
# 当天新投稿全部列出；知识区 UP（本地 sign 分类识别）优先并抓字幕深度处理；其余仅清单
python feed_subs.py --cookies cookies.txt --out-dir out/ [--pages 3] [--max-subs 40]
```

| 项 | 实现 |
|----|------|
| 数据源 | 关注动态 feed `/x/polymer/web-dynamic/v1/feed/all`（带 cookie 实测可用；archive 在 `modules.module_dynamic.major.archive`，stat.play 为「8.8万」格式需解析） |
| 覆盖 | 3 页 ~58 条/次，覆盖全部关注 UP 最新投稿——「订阅范围内当天有更新的都关注」 |
| 去重 | seen_bvids.json 跨模块并集，已见 pass |
| 知识区重点 | 本地 sign 分类（BILI4 词库，0 请求）识别知识区 UP → 优先排序 + 补 cid 抓字幕（view+player+subtitle，预算 ≤40） |
| 输出 | `out/feed_<date>.md` 当日新投稿清单（知识区 ⭐ 标记）+ corpus 追加 |
| 预算 | feed ≤3 + 知识区字幕 ≤40 ≈ 43/200，与主线/订阅池共享 req_budget |

> 说明：首跑会记录当天全部新投稿（可能较多）；之后每天只出现真正的新投稿。

### 已知限制与 Backlog（BILI7 终审 + 用户决策 2026-08-12）

| 限制 | 状态 |
|------|------|
| arc/search（UP 投稿）-799 持续封禁 | ✅ 已替代：feed/space 无签名+带 cookie（实测 code=0） |
| acc/info（UP 资料）-403 | ✅ 已替代：card?mid= （实测 code=0） |
| **每周必看历史榜（series/one 需 wbi）** | 🔒 **接受缺口（用户决策），登记 backlog**——红线内不可达（带 cookie 无签名 -352 实测；网页 JS 渲染无内嵌数据）；若未来 wbi 门槛变化或启用「现成工具例外」再评估 |
| feed/space 无签名稳定性 | ⚠️ 预案：被拦则降级 feed/all+search |
| 播放器子接口（online/total、pagelist、dm/view） | 📋 backlog：性价比排序 online/total > pagelist > dm/view，需求出现时接入 |

## 多平台路由（TC-20260811-BILI8 终审）

| 平台 | 能力 | 模块 |
|------|------|------|
| B站 | 字幕/榜单/关键词/订阅/ASR（全量） | 本技能主路径 |
| 爱奇艺 | 元数据文本信号（标题/简介/标签，免登录实测） | `platforms/iqiyi.py` |
| 抖音 | ⏸ 冻结（字幕需 a_bogus 签名；desc 文本信号待需求确认） | — |
| 腾讯视频 | ⏸ 第二档（音频→ASR 待打通）；cKey VTT 需用户批准 | — |
| 优酷/西瓜/快手 | ❌ 归档不落地 | — |
| YouTube 等国际 | 🔒 backlog（前置=海外运行环境） | — |

**接入新平台前**：读 `references/platform-eval.md` 评估表 → 独立合规终审（TC 案卷制）→ 探针实测（1 温和请求验可达性）→ 平台级红线确认。红线映射：普适红线跨平台适用；签名逆向（cKey/cmd5x/a_bogus）需用户明示批准，例外不构成先例。

## 故障处理

| 现象 | 处理 |
|------|------|
| 全部「无字幕轨」 | 检查 cookies 是否含有效 SESSDATA |
| HTTP 412 / 429 | 当日停止该接口，次日再试 |
| 字幕下载 403 | auth_key 过期，重跑（脚本自动重取） |
| 关键词榜泛词多 | 扩充 STOPWORDS / 校准词性白名单（3 天试运行期） |
| 转录 T2 报 412（yt-dlp 免登录） | 正常：游客 wbi 签名被拦；T2 需带 cookie（transcribe_video 默认已带） |
| 转录 T3/T4 报 Not found（模型文件明明在） | **Windows 中文路径 bug**：sentencepiece/ctranslate2 用 GBK 处理路径，「<user>」编码成乱码。模型缓存须放纯 ASCII 路径（脚本已自动用 `C:\ms_cache`，勿改） |
| 转录 T3 报 torchaudio not installed | funasr 安装会移除 torchaudio；重装 `pip install torchaudio`（实测 torch 2.12.1 + torchaudio 2.11.0 共存正常） |
| 转录 T3 缺 funasr | `pip install funasr`（本机已装 torch/modelscope）；模型走 modelscope（iic/SenseVoiceSmall）国内可达 |
| T3 没走 GPU，日志出现「ROCm 不可用，回落 CPU」 | 查 `D:\rocm101-venv\Scripts\python.exe` 是否存在；或 `%TEMP%\t3_rocm_worker.log` 末尾 5 行（客户端已自动回读）。临时强制：`--t3-device-mode cpu` 确认 CPU 路径本身正常 |
| T3 GPU 首次很慢（10–27s） | 正常：import torch+funasr + 模型加载 + **首次 MIOpen 内核编译**。批量时只付一次，第 2 个视频起为 7s 量级 |
| T3 输出与上次差 1 个字 | 正常：SenseVoice 本身非逐字确定（CPU 侧在相同位置同样抖动，相似度 0.9987）。断言口径用「相似度 >0.99」 |
| 批量时每条都打印「首次会等待模型就绪」 | 已修：仅在 `w.proc is None or w.proc.poll() is not None` 时打印 |
