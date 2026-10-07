# 字幕清洗管线规范（bili-daily enrich LLM 通道）

> **来源**：斯奎奇大王项目 391 篇投稿字幕全量实测（2026-10-04，产物 <project>/deliverables/sikeqi-dawang/）。
> **用途**：任何「转录稿 → LLM 去废话/摘要」任务开工前先读本页；模型选错=幻觉率暴涨，分块错=无限重试。
> 本文件由 P1-3 落地（web-intel 改进报告 2026-10-04）。

---

## 1. 模型选择（实测数据，勿凭印象）

| 模型 | 压缩率 | 忠实度 | 实测结论 |
|---|---|---|---|
| **deepseek-v4-pro**（sensenova 池） | 62% | **零幻觉**，保留讲者口语风格 | **首选** |
| deepseek-v4.1-flash（DSH 网关） | 34% | 忠实、不编造 | 次选（轻压缩场景） |
| **agnes-3.0-flash** | — | **编造整段原文没有的内容**（"大厂"0 次却输出"从大厂逃离"整节）、爱写分析旁白 | **禁用于清洗** |

- 注意：sensenova 池**无** deepseek-v4.1-flash（403 token plan），可用 v4-pro / v4-flash / kimi-k3 / glm-5.2。
- 池路由与并发纪律见 `~/.dsh/skills/dsh-free-pool/INVOKE-RULES.md`（单一事实源）。

## 2. 忠实度门禁：必须上下文判定，禁词表一刀切

词表包含会大量误伤（实测 47 篇误判 → 修正规则后 0 篇）：

| 词 | 误伤场景 | 判定规则 |
|---|---|---|
| 总而言之 / 总的来说 | 讲者口语里的连接词（"总而言之都不值钱"） | 仅**句首 + 非列表项**才判旁白 |
| 大厂 / 抓手 / 躺平 / 逃离 | 小标题、成语正常使用（"抓手无寸铁"） | 移出幻觉词表 |

## 3. 标点恢复 = 质量杠杆（含时间轴不变式）

**引擎（2026-10-05 起升级）**：`punc_engine.py` — **全局共享资产**
`<CodeLib>\scripts\punc_engine.py`（登记于 `~/.dsh/memory/assets.md`），
sherpa ONNX ct-punc + OpenVINO GPU，实测长文本 **11–20x**（旧 funasr CPU ~1378 字/秒 → 新方案长文本约 4 万字/秒）。

```bash
# CLI（任意项目可直接调用）
python <CodeLib>\scripts\punc_engine.py --input raw.txt --output punct.txt
python <CodeLib>\scripts\punc_engine.py --dir in/ --out-dir out/
# 库
from punc_engine import restore_punc_safe   # 需先 sys.path.insert 共享目录
# 回退旧引擎
python ... --engine funasr
```

**必守约束（实测）**：
- **必须分块**：整篇一次推理超 ~16000 字必崩（注意力 O(n²)，24000 字显存/内存超限）；
  引擎已内置：>8000 字自动切 5000 字块，**块边界落标点**（否则产生「词中插标点」错位）。
- **不变式**：仅插入标点 → 去标点 + 英文小写归一后字序列与原文完全一致
  → 可用**双指针**把清洗文本精确对齐回字符级时间轴。
- 模型：`C:/ms_cache/sherpa_punc/model.int8.onnx`（sherpa-onnx 官方 int8，75.5MB）。

**★输入若已带标点，必须先剥标点再送引擎（2026-10-07 实测，import 级教训）**：

官方 `ai-zh` 字幕 raw 的 `content` 是**零标点**的（`BV11CYk6UECm` 4222 字，标点占比 0.0%，标点种类 `[]`），
所以旧管线只做「插入」。但 **SenseVoice ASR 产出自带标点**（`BV1HzV5zFEuY` 49411 字中标点
`{'。':2894,'，':2282,'？':840,...}`），再送 ct-punc 会**二次加标点**，产出
`哎，。等格啷格格格格？哎呀，，大家晚上好啊，。` 这类 `，。`/`？？`/`，，` 连排退化
（官方字幕路径的 docs 里 `[，。！？；：]{2,}` 计数全为 0，可作对照基线）。

```python
# 只剥中日韩/全角标点，保留 ASCII . , ' % $（承载小数/缩写/百分号）
EXISTING_PUNC_STRIP = set("，。！？；：、…—·“”‘’（）《》〈〉「」『』【】〖〗～")
def strip_existing_punct(text):
    return "".join(ch for ch in text if ch not in EXISTING_PUNC_STRIP)
# process() 里：先 body = [dict(s, content=strip_existing_punct(...))] 再 build_char_axis
```

- 剥标点**不破坏时间轴不变式**（仍只做插入，字序列不变，双指针对齐照旧）。
- 压缩率用**剥标点前的原始字数**做分母，否则压缩率虚高。
- 副作用是好的：有标点后 `is_pure_filler`/`is_housekeeping` 才能按句判定——
  无标点的 whisper 产物压缩率只有 2.2%，剥后重插的 SenseVoice 产物 18.4%。
- 回归核对：官方字幕 raw 1337 篇中仅 7 篇含 CJK 标点，且 docs 已建不会重建。

**质量等价性（已实测裁定，勿重复调研）**：新旧引擎**字序列完全一致**（差异仅英文大小写，属已载口径）；
章节数、对齐失配数完全相同；下游 LLM 整理质量的引擎间差异（中位 2.5pp）被 LLM 运行间噪声（std ~18pp）淹没，
**t=0.55 不可检出** → 可放心替换。

- 语义级去废话**必须 LLM**：规则清洗天花板 ~9%（纯口水词仅 0.3% 字数；噪声主体是 ASR 碎片化——段长中位 8 字——与口头禅、直播事务）。

## 4. 分块与输出上限（防丢内容/防无限重试，代码级教训）

- **bug A**：按时间锚点切块时，单个锚点段超限**也不会再切** → 出现 61,004 字超长块 → 输出超 max_tokens → 无限重试。
- **bug B（2026-10-05 修复）**：`finish=length`（输出被截断）时该块被**直接丢弃**，内容静默丢失；
  且 `max_tokens` 公式 `len(prompt)*0.9` 按「1 字≈1 token」估算，**中文实际约 1.5 token/字** → 长块必然截断。
  实测铁证：`BV11iud6tErR`（1652 字）输出需 **5434 token**，旧上限 4000 **必炸**；提为 6000 后完整输出。

- **必须实现的 4 条规则**（sikeqi 的 `tools/llm_organize.py` 已全部落地，见 `organize_chunk()`）：
  1. 单段超限后**按句再切**（复用句级边界，不丢时间锚）；
  2. `max_tokens` **按输入长度动态定**，且用**中文 token 比**估算（≈1.5 token/字）；
     实测下限 6000、上限 32000（4000 下限余量过薄：观测到 3835 token/4000 = 96%）；
  3. **`finish=length` 时按句二分降级重试**（递归 ≤2 层，块 >1500 字才切），**禁止直接丢弃该块**；
  4. 批量 worker 用受管作业（DSH job/后台任务），**禁 gitbash nohup 派生脱管进程**
     （job_kill 管不到，曾致 6 worker 常驻拖垮宿主，2026-10-04）。

- **回归测试**：`sikeqi-dawang/tools/test_llm_chunk_fallback.py`（4 条断言，含"可红性"；mock `call_llm`
  不产生真实 API 调用）。修复后实测：6/6 + 4/4 全通过（修复前同一批有 4 次 `finish=length`）。

## 5. 字幕获取通道（与 channel-notes.md §3 呼应）

- `view.subtitle.list` 不能预筛有无字幕（has_url 全 False）→ 必须再调 `player/wbi/v2`（**bvid+cid+cookie**，只传 bvid 报 -400）。
- UP 投稿清单 `/x/space/wbi/arc/search` 必须 **wbi 签名 + 登录 cookie**（-799/-403）；交叉验证用 polymer feed/space 独立通道。
- 同一内容常有**精简版/完整版**两条：精简版无字幕轨——清单阶段不按"缺字幕"剔除，先查完整版。

## 6. 无字幕视频补采：本机 GPU ASR（2026-10-07 实测定型）

参考实现：`sikeqi-dawang/tools/asr_gpu.py`（109 篇 / 529.8h 实测跑通）。

### 6.1 模型选型（CER 基准，官方 ai-zh 字幕为参照）

| 模型 | CER | 复读率 | 结论 |
|---|---|---|---|
| whisper-small | 24.9% | 3.1%（最差块 14.2% 崩坏） | 不选 |
| **whisper-large-v3-turbo** | **17.5%** | **0.2%（全 top1，稳定）** | **选** |

- **必须排除双语样本**：美国街访类视频音频是英语，官方"中文字幕"其实是 ASR 对英语的乱转写，
  混入后 CER 虚高到 60–78%。用 `latin_ratio>5%` 预筛（`tools/audit_official_subs.py` 全量审计产出）。
- **参照必须按时间窗裁剪**：拿"全片官方字幕"对比"只转写前 N 段的 ASR"，截断会把 CER 严重虚高（初版得 57.7%）。
- **比字数会误判**：large-v3-turbo 单看字数更多，其实开头是「哎呀」重复 41 次的幻觉；必须算 20 字滑窗复读率。

**★2026-10-07 改道：优先走技能 T3 常驻 worker 池（SenseVoice-Small）**
（`t3_rocm_client.py` + `transcribe_video.py --list`；本机参考实现在
`sikeqi-dawang/tools/asr_t3.py`）

| 引擎 | 平均 CER | 吞吐 | 标点 | 时间戳 |
|---|---|---|---|---|
| **SenseVoice-Small（T3 worker 池）** | **8.8%** | **63.1x**（3–4 worker 池可达 88–136x） | **自带 + ITN** | 真实（段中位 2.1s） |
| whisper-large-v3-turbo（T5 兜底） | 11.2% | 11.3x | 无 | 30s chunk 假边界 |

4 视频 × 2 引擎、以官方 ai-zh 字幕算 CER 的 A/B 实测（`sikeqi-dawang/tools/ab_engine_bench.py`）：
SenseVoice **CER 低 22%、吞吐快 5.6 倍**，且自带标点省掉一道引擎；
whisper 路线的 30s chunk 时间戳不产生真实章节（见上 6.2），T3 的段级时间戳印证了这点
（`BV1ghZoYwEcY` 段 span 中位 2.07s、末段 11335.65 ≈ 视频 11340s）。
→ **首选 T3/SenseVoice**；whisper-turbo 仅作 T5 兜底。

### 6.2 采集器必守（每条都有实测代价）

- **引擎**：transformers `pipeline` + `device=0` + `dtype=torch.float16`（AMD 卡走 ROCm venv，无 ctranslate2 故不用 faster-whisper）。
- **必须传 `chunk_length_s=30, stride_length_s=5`**，否则 >30s 音频抛
  `ValueError: You have passed more than 3000 mel input features (> 30 seconds)`；预热调用同样要带。
- **必须清代理环境变量**：脚本启动先 `os.environ.pop()` 掉 `HTTP_PROXY/HTTPS_PROXY/ALL_PROXY/NO_PROXY` 及小写共 8 个，
  否则 httpx 解析 `NO_PROXY` 里的 `[::1]` 抛 `InvalidURL: Invalid port: ':1]'`（与加载模型无关，纯环境坑）。
- **默认关时间戳**（`return_timestamps` 需显式开关）：开=12.4x，关=21x+，**提速近 2 倍**。
  且时间戳给的 30s chunk 时间轴**不产生真实章节**——chunk 间最大停顿仅 6.0s，`chapterize` 却报 14 个"章节"，
  全是长 chunk 的假边界。素材的真实结构来自下游 LLM 分节，不靠这个。
- **产物 schema 必须与官方字幕 raw 完全一致**：`process()` 读 `raw["body"]`（每项 `{from,to,content,sid,location}`），
  写成 `{"segments":[...]}` 会让 `process()` **返回 None 且无任何报错**，docs 静默不产出。这是本项目最贵的坑。
- **两道防御闸门**（宁可漏采不写残缺产物）：
  1. `view.is_upower_exclusive=True`（充电专属）→ playurl 只给试看片段（durl 180/300/600s，dash 空）→ 跳过；
  2. 下载后 ffprobe 真实时长 < 声明*0.95 → 判截断跳过。
  **★多P 视频必须用 `page.duration` 而非 `view.duration`**：后者是各分P 之和，
  单分P 比对必然触发"截断"误判（实测 `BV1ghZoYwEcY` 2P/11340s，分P 实际 3920s+7420s）。
- **OOM 修复**：长音频一次性解码触发 `not enough memory`。必须先 ffmpeg 转 16kHz 单声道 WAV 并按 600s 切片；
  `-c copy` 只切容器不降体积**仍会 OOM**，必须重编码。**ffmpeg segment muxer 不会自动创建输出目录**，
  不建目录则 rc=-2 无输出。
- **闸门3：转写 <50 字判失败不写盘**（曾把 chars=0 的空产物写进 manifest 污染下游）。
- **闸门4：密度物理上限**——中文正常语速 4–6 字/秒，官方字幕中位 4.14。
  `len(txt)/dur > 10 字/秒` 必然含幻觉，不写盘。实测 `BV1VB24YRE1K`（9s 音频）产出 320 字
  = **35.6 字/秒**，内容全是「欢迎订阅」复读——旧闸门只看字数下限，把它当成"OK 320字"写进了 raw。
- **闸门5：复读率**——`rep_ratio(txt)` = 20 字滑窗最高频次数/窗口数。正常 <2%，
  整段复读幻觉 >10%（该篇 19.7%）。判据用「进程存活 + 日志 mtime」双条件，见 6.3。
- **★别在批次运行中改采集器代码**：Python 已加载的模块不会热更新，改文件只影响下次启动。
  实测改了闸门后旧进程仍按旧代码写幻觉产物，必须 kill 后重启（断点续采会跳过已完成篇）。

### 6.3 吞吐与并发（RX 7800 XT 实测）

- 单进程串行 **21–28x 实时**（109 篇实测稳定在 20.6–28.4x）；多进程 n=3 时第 3 个进程 OOM，
  **GPU 单进程串行最稳**；batch_size 增大反而变慢（bs=16 → 7.44x）。
- **串行是刻意的**：ASR 走 ROCm GPU、标点引擎走 OpenVINO GPU，同卡并发会双输。
- **★等待链的停滞阈值必须大于单篇最长耗时**：最长篇 7.47h，@16x 约 28min、@20x 约 22min。
  曾用 600s 阈值 → 110 篇里 100 篇 >3.3h，会在转写中途误判"ASR 中断"并抢 GPU 起 docs（双输）。
  正确判据 = **进程存活 AND 日志 mtime**：进程在就继续等（阈值放到 3600s），
  进程消失且日志停 >120s 才判中断。
- **`ps -W` 会截断命令行**，`ps -W | grep "rocm101-venv.*asr_gpu"` 永远匹配不到（误报 PROC_GONE）；
  应匹配解释器路径 `ps -W | grep "rocm101-venv/Scripts/python"`。

### 6.4 密度不要单独当门禁

官方字幕 1170 篇长视频密度中位 4.14 字/秒，但**直播类天然低**（最低 0.90 字/秒，10 分位 2.99）。
ASR 产出 2.47 字/秒曾被误判为"丢内容"，逐段核对发现是真实停顿 + 直播事务，文本连贯完整。
**判质量要看文本可读性与复读率，不能只看密度。**

### 6.5 T3 常驻 worker 池：并发拐点与「池永久失效」自愈（2026-10-07 实测）

**并发是唯一正确用法**：`WorkerPool.transcribe_full()` 是**同步单条**调用
（`self._free.get()` 借一个空闲 worker 阻塞到结束，用完 `put` 回池）——
串行循环只会用到池里 1 个 worker，其余全白起。切片必须用
`ThreadPoolExecutor` 并发 `submit`（技能自身 `test_t3_pool.py` 也是这么做的）。

| workers | ASR 墙时（14601s 视频） | 吞吐 | GPU 累计 |
|---|---|---|---|
| 串行(1) | 198s | 73.7x | — |
| 2 | 147s | 99.3x | 270s |
| 3 | 126s | 116.1x | 335s |
| **4** | **108s** | **135.8x** | 352s |
| 5 | 118s | 123.5x（回退） | 478s |

**★池会「累积性全灭」并永久停摆——这是最贵的坑**（2026-10-07 一次跑掉 105 篇）：

- **症状**：前几篇正常（`OK 42285字`/`127.5x`），某篇起 `AcceleratorError: CUDA error:
  unspecified launch failure`（HIP `hipErrorLaunchFailure`），此后**每一篇**都是
  「N/N 个切片未转写」，秒级失败。实测 **558 条失败 / 仅 2 条 OK**。
  与输入无关：失败篇里包含**冒烟测试刚成功跑过**的同一批视频。
- **根因（`t3_rocm_client.py` WorkerPool）**：worker 失效后**不再放回池**，
  `_dead_count += 1`；`_dead_count >= size` 时置 `self._disabled = True` →
  之后每次 `transcribe_full()` **立即 `return None`**，且**没有任何自动重建路径**。
- **不是设备级挂死**：所有进程退出后，新起单 worker 立刻成功
  （600s 音频 12.7s 出 126 段）；Windows 事件日志 3 小时内**无**
  amd/Display/dxgkrnl 事件（排除 TDR 驱动复位）。
- **修复（单向依赖：调用方负责自愈）**：`WorkerPool.recycle()` = 整池
  `close()` 后重新拉起并复位 `_disabled`/`_dead_count`。**必须整池关掉**——
  设备只有在无进程持有时才恢复。`recycle()` **不能**在客户端自动触发
  （会关掉正被其它线程借用在飞的 worker），批量驱动应在篇间安全点调用。
- **批量驱动的自愈三件套**（`sikeqi-dawang/tools/asr_t3.py` 的 `PoolManager` 已落地）：
  1. 每 N 篇（实测 6）主动 recycle，代价 ≈ 每 worker 10–12s（模型加载 4–6s）；
  2. 某篇「全部切片失败」→ recycle 并**重试该篇一次**（单切片空=9s/16s 静音小视频，
     属正常，不重试）；每篇后检查 `pool._disabled` → recycle；
  3. **连续 3 篇即便换新池也失败 → 停机报错**，不空转。
- 默认并发取 **3**（116x）；4 是吞吐拐点但挂死概率更高，2 更稳（99x）。
- 等待链**不要**用 `ps -W | grep "rocm101-venv.*asr_t3"` 判存活（`ps -W` 截断命令行，
  永远匹配不到）；用 `ps -W | grep -c "rocm101-venv"` 或日志 mtime。

## 7. 下游产物维护：两个静默失效的坑（2026-10-07 实测）

ASR 补采与「重建下游产物」脚本配合时，有两处**不报错但完全不做事的失效**，
必须按下面的写法防。

### 7.1 清理/重建脚本必须按真实命名规则 glob，不能假设 `{bvid}.md`

产物实际命名是 **`{bvid}_{safe_name(title)}.md`**（`docs/` 与 `materials/` 皆然）。
实测某项目 `docs/` 共 1338 个文件，其中 **1337 个**以 `BV..._` 开头，
`{bvid}.md` 形式 **0 个**。只按 `os.path.join(pre, bvid + ".md")` 查找的清理脚本
→ **命中 0 → 陈旧产物永远删不掉**，而 `build_materials.py` / `llm_organize.py`
又是增量的（`if os.path.exists(out) and not overwrite: return skip`），
于是重采后的 raw 永远进不了 docs/materials，**整链静默空转**。

正确写法（docx `rebuild_asr_artifacts.py`）：

```python
def artifacts(bvid):
    pre = DOCS_DIR if ... else MATERIALS_DIR
    return (glob.glob(pre + "/" + bvid + "_*.md")   # 真实形式
            + glob.glob(pre + "/" + bvid + ".md"))  # 兜底
```

**先跑只读模式打印「将删除 N 个」，确认命中数非 0，再 `--apply`。**
修复后仍要验证：脚本应报出具体文件名，而不是 `0`。

### 7.2 索引生成器的模板必须内联 README 的全部手写段落

`build_index.py` 这类「从模板重建 README」的生成器，会把**只在生成物里手写过、
模板里没有**的段落**静默丢掉**。实测：README 有一整段「语料来源说明」
（含污染源剔除记录），而生成器模板中没有它 → 下一次重建就消失。

规矩：**README 里任何人工增补的内容，必须同步进生成器模板**；
每次改完 README 立刻无条件重跑一次生成器，diff 看有没有段落变少。
新增统计行（如「其中 B 站原生字幕 N 个 / 其中本机 ASR 补采 M 个」）也要一并进模板，
字段来源写清（本项目用 `manifest.videos[*].via == "asr"` 区分 ASR 补采篇）。

