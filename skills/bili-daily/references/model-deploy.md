# B站转录本机部署方案（T3 SenseVoice / T4 faster-whisper）

> 状态：**✅ 部署完成并双引擎实测通过**（2026-09-28）
> 对象：bili-daily 技能 `transcribe_video.py` 的音频转录通道

## 一、可行性取证（2026-09-28 实测，非推断）

### 硬件
| 项 | 实测值 | 判定 |
|----|--------|------|
| CPU | Intel i5-12400（6P/12T，支持 AVX2） | ✅ 两个模型 CPU 推理均足够 |
| 内存 | 31GB（当时可用 12GB，负载 60%） | ✅ |
| GPU | AMD Radeon RX 7800 XT（无 NVIDIA） | ⚠️ CUDA 不可用；DirectML 路径当前 DLL 损坏（见风险） |
| 磁盘 | C: 464GB 总量 / 241GB 空闲 | ✅ 模型合计 ~1.36GB 无压力 |

### 软件
| 依赖 | 状态 | 说明 |
|------|------|------|
| Python 3.12.8 AMD64 | ✅ | |
| torch 2.12.1+cpu | ✅ 已装 | CPU 版即可 |
| torchaudio 0.27.1 | ✅ 已装 | funasr 需要 |
| modelscope 1.37.0 | ✅ 已装 | 模型下载通道 |
| ctranslate2 4.8.1 | ✅ 已装 | faster-whisper 推理后端（T4 立即可用） |
| faster-whisper | ✅ 已装 | T4 已就绪 |
| funasr | ❌ 未装 | T3 唯一软件缺口（`pip install funasr`） |
| torch-directml 0.2.5 | ⚠️ DLL 加载失败 | GPU 加速暂不可用，CPU 已够用，不阻塞 |

### 模型源（modelscope.cn，均实测 HTTP 200、国内可达）
| 模型 | repo | 文件/体积 | 格式 |
|------|------|-----------|------|
| SenseVoice-Small | `iic/SenseVoiceSmall`（达摩院官方） | 10 文件 / **894MB**（model.pt 893MB） | torch pt |
| faster-whisper small | `Systran/faster-whisper-small` | 7 文件 / **464MB**（model.bin 461MB） | CTranslate2（ctranslate2 4.8 兼容）|

## 二、推荐路线

**T3 SenseVoice-Small 主选、T4 faster-whisper 备选**，理由：
1. 中文识别质量 SenseVoice 优于 whisper small；B站以中文口语为主
2. SenseVoice CPU 速度快（RTF 远低于实时，20 分钟音频约 1–3 分钟；whisper small int8 约 4–6 分钟）
3. T3 软件缺口小：torch/modelscope/torchaudio 已齐，只差 `funasr` 一个包
4. T4 几乎零成本启用：ctranslate2 + faster-whisper 已装，只差模型下载；作为 funasr 安装异常时的兜底

## 三、部署步骤（批准后执行）

### 步骤 1：安装 funasr（约 100–200MB wheel）
```bash
pip install funasr
# funasr 依赖 modelscope(已有)/torch(已有)/torchaudio(已有)/kaldi-native-fbank 等自动补齐
```

### 步骤 2：下载模型（modelscope，~1.36GB）
```bash
# T3 主模型（894MB）
modelscope download --model iic/SenseVoiceSmall \
  --local_dir ~/.cache/modelscope/hub/models--iic--SenseVoiceSmall

# T4 备选模型（464MB）
modelscope download --model Systran/faster-whisper-small \
  --local_dir ~/.cache/modelscope/hub/models--Systran--faster-whisper-small
```
`transcribe_video.py` 已实现 `_find_local_model()`：自动探测上述两个缓存目录，
命中即本地加载（完全避开 HuggingFace）。

### 步骤 3：端到端验证（红→绿）
```bash
# 用已知无字幕的非会员短视频，验证 T3 全链路
python transcribe_video.py --bvid <无字幕视频> --cookies cookies.txt --engine sensevoice --out-dir out/transcripts
# 验收标准：产物 frontmatter transcriber="SenseVoice-Small"；字数与时长合理（~200字/分钟）；
# 与同视频 T1/T2 字幕（若存在）抽查关键词重合（防幻觉）
# 再跑 --engine faster-whisper 验证 T4 备选
```

### 步骤 4：性能基线记录
记录每个引擎对同一视频的耗时/字数，写入 SKILL.md 实测矩阵（含 T1/T2 已有基线互验）。

## 四、合规（沿用现有红线，不因部署放宽）
- 模型仅本机推理，内容不出本机
- 音频即删；转录文本按 24–48h 生命周期
- 请求限速/预算/水印逻辑不变

## 五、已知风险与规避
| 风险 | 影响 | 规避 |
|------|------|------|
| funasr 与 Python 3.12 兼容性 | funasr 安装/导入失败 | T4 兜底可用；或 `pip install funasr --pre`/venv 隔离 |
| torch-directml DLL 损坏 | GPU 加速不可用（非阻塞） | 维持 CPU；需要时重装匹配 wheel（可选优化） |
| CV2/onnx 等 funasr 隐性依赖 | 安装报错 | 按 pip 报错逐个补装 |
| SenseVoice 输出含标点/情感标签 | 文本带 <\|zh\|> 等特殊标记 | funasr `rich_transcription_postprocess` 已处理（代码已接入） |
| whisper 幻觉（无音段/空白段重复） | 转写质量 | VAD 过滤已开；抽查验收 |

## 六、执行记录（2026-09-28 实测）

| 步骤 | 结果 |
|------|------|
| pip install funasr | ✅ funasr 1.4.16（连带升级 numba/llvmlite）；**副作用：torchaudio 被移除**，需重装 |
| torchaudio 重装 | ✅ `pip install torchaudio` → 2.11.0（torch 2.12.1+cpu 共存正常，import 验证） |
| T3 模型下载 | ✅ modelscope `iic/SenseVoiceSmall` 894MB~（~18s，75MB/s） |
| T4 模型下载 | ✅ modelscope `Systran/faster-whisper-small` 464MB（~9s） |
| T3 端到端 | ✅ 时政视频 4442 字；rtf≈0.037（约 27×实时）；纯音乐视频正确输出「🎼」（非幻觉） |
| T4 端到端 | ✅ 同视频 4291 字，逐行分句输出；frontmatter/水印正确 |
| 缓存整理 | ✅ 统一在 `C:\ms_cache`（hub 464M + models 901M ≈ 1.36G）；冗余副本已删 |

### 踩坑记录（本机特有，勿再踩）
1. **Windows 中文路径 bug（实锤）**：sentencepiece/CTranslate2 等 C++ 扩展按 GBK 处理路径，
   「C:\Users\<user>\...」中文字符被编码成乱码 → `os.path.exists()` True 但 C 层报 Not found。
   **解法：模型缓存搬 `C:\ms_cache`（纯 ASCII），脚本顶部注入 `MODELSCOPE_CACHE=C:/ms_cache`。**
2. **funasr 的模型目录结构是 `$MODELSCOPE_CACHE/models/<org>/<name>`**（无 hub 层）；
   用 `local_dir` 预下载到 `models--iic--SenseVoiceSmall` 根结构会被 funasr 1.4.x
   remote-code 加载拒绝 → **传模型 id `"iic/SenseVoiceSmall"` 由 funasr 自己管理**。
3. **funasr 安装移除 torchaudio**：装后必须 `pip install torchaudio`（版本自动解析）。

## 七、后续可选项
- [x] ~~torch-directml 修复（AMD GPU 加速，非阻塞）~~ → **已由 ROCm 10.1 路线取代**（见第八节）：
  torch-directml 锁死 torch==2.4.1 且缺 `torch_directml_native.pyd`，导入即失败，不再考虑。
- [ ] 更轻量：faster-whisper tiny/base（~75/145MB）作为快速试听档

## 八、T3 GPU 加速：ROCm 10.1 常驻 worker（2026-10-07 落地）

### 为什么是 ROCm 而不是 DirectML / Vulkan / OpenVINO
| 路线 | 结论 | 原因 |
|------|------|------|
| ORT-DirectML | ❌ 否决 | 官方不支持并行执行；4 线程并发同一 session 必 **GPU device removed 全崩** |
| torch-directml | ❌ 否决 | 锁死 torch==2.4.1，且本机 wheel 缺 `torch_directml_native.pyd`，导入失败 |
| whisper.cpp + Vulkan | ❌ 否决 | 硬件层通（探针枚举到 7800 XT），但工程层三缺：v1.9.4 Release assets 为空、本机无 MSVC/cmake/Vulkan SDK |
| **ROCm 10.1 + PyTorch** | ✅ **采用** | 官方 pip 链路，本机 `D:\rocm101-venv` 实测可用（详见 `deliverables/rocm10-win11-7800xt/`） |

### 架构：必须子进程隔离 + 常驻
主环境 `C:\Users\<user>\AppData\Local\Programs\Python\Python312\python.exe` 是
**torch 2.12.1+cpu（无 GPU）**；ROCm 栈在独立 venv **`D:\rocm101-venv`**
（Python 3.13.13，torch 2.14.0+rocm10.1.0，hip 7.16.26385）。
**两者无法同进程共存** → 子进程 + 行分隔 JSON 协议。

**必须常驻**：就绪固定开销 ≈10–27s（import torch+funasr 10.6s + 模型加载 4.7s +
首次 MIOpen 内核编译），而单次推理仅 7s 左右。若每个视频起一次进程，
7s 会变成 ~21s，得不偿失。批量（`--list`）是本技能主用法，常驻后第 2 个起只剩推理耗时。

| 文件 | 角色 |
|------|------|
| `t3_rocm_worker.py` | 在 ROCm venv 内运行。`_split_protocol_channel()` 用 `os.fdopen(os.dup(1))` 备份 fd1 作协议通道，再 `os.dup2(2,1)` + `sys.stdout=sys.stderr`，把 funasr/modelscope/torch 的日志与进度条全部改道 stderr —— **这是协议不被污染的关键** |
| `t3_rocm_client.py` | 主环境侧。`T3RocmWorker` 单例 + **`WorkerPool` 池**、`READY_TIMEOUT=300`、`INFER_TIMEOUT=3600`、`MAX_RESTARTS=1`、`_dead` 永久失效标记（venv 缺失/就绪超时后本次进程不再重试，避免每个视频白等超时）、`atexit` 优雅退出 |
| `test_t3_rocm.py` | 25 条断言 + `--red` 可红性自检 + `--no-gpu` 跳过 GPU 用例 |
| `test_t3_pool.py` | 25 条断言（池语义 / 真并发 / **逐条字数对号入座防串话** / 合规限速）+ `--red` 可红性自检 |

### 开关与观测
- CLI：`--t3-device-mode {auto,rocm,cpu}`（默认 auto = 先试 GPU 再回落 CPU）
- CLI：`--t3-workers N`（批量池大小，默认 2，1 = 串行；仅 `--list` 生效）
- 环境变量：`WEBINTEL_T3_DEVICE_MODE`、`WEBINTEL_T3_WORKERS`、`WEBINTEL_T3_DEVICE`、
  `WEBINTEL_T3_MODEL`、`WEBINTEL_T3_READY_TIMEOUT`、`WEBINTEL_T3_INFER_TIMEOUT`、
  `MIOPEN_CUSTOM_CACHE_DIR`、`TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL=1`
- 产物 frontmatter 的 `transcriber` 字段**如实记录实际通道**
  （`SenseVoice-Small(GPU)` / `SenseVoice-Small`）—— 一眼可查这次到底走了哪条路
- 子进程日志落 `%TEMP%/t3_rocm_worker.log`；**池内每个 worker 单独一个日志文件**
  `t3_rocm_worker_1.log` / `_2.log`…（并发写同一文件无法诊断），失败时回读末 5 行

### 池与限速的合规耦合（不可省）
`WorkerPool` 让多个视频并行跑，于是 `fetch_subtitles.pace()` 会被**多线程同时调用**。
原实现无锁：各线程先读 `_last_request_at`、判定「已够久」、再各自放行，
**全局 `MIN_INTERVAL=1.5s` 最小间隔失效**，直接违反 B站限速红线。
已加 `_pace_lock = threading.Lock()` 把「判定 + 睡眠 + 记时」变为原子操作，语义不变。
`test_t3_pool.py` 断言 7a/7b 锁死：4 线程并发 `pace()` 总跨度 ≥ 4.5s（实测 6.09s），
相邻间隔全部 ≥ 1.5s。
⚠️ 另注意 `t3_sensevoice_rocm(audio_path, worker=...)` 的 `worker` 参数是关键：
批量路径必须把**池**传下去（池自身有 `transcribe()`），否则会退化成单例、多起一个进程。

### 性能实测（2026-10-07）

> ⚠️ **本小节 2026-10-07 晚已修正**：首版 ①② 表的 CPU 基线**是机器带载时测的，偏高约 6 倍**
> （70.11s / 41.31s / 172.0s 全部作废）。下方为**空载清洁复测**结论。

**① 清洁复测**（同一 300.7s 音频，`t3_rocm_worker.py --selftest`，n=3 取中位）
| 路径 | 墙钟 | 推理 | xRT | 输出 |
|------|------|------|-----|------|
| ROCm-GPU（`D:\rocm101-venv`，device=cuda） | 19.29s | **8.43s** | 35.7x | 822 字 |
| 主环境-CPU（device=cpu） | 23.61s | **11.59s** | 25.9x | **822 字（逐字相同）** |

→ **真实倍率 1.37x（推理）/ 1.22x（端到端），不是 2.2x–9.7x。**
GPU 的价值在 **CPU 秒数约减半**（GPU 33.5s 含加载 vs CPU 推理即 57.0s）。
生产函数佐证：`t3_sensevoice()` 第 2 轮 20.24s，funasr 自报 `time_escape=14.143`、
`rtf_avg=0.047`。**纪律：比较 A/B 之前先量化噪声基线。**

**② 并行 worker 池（真正的大杠杆）**
`--list` 批量模式下默认起 **2 个常驻 GPU worker**（`--t3-workers N` / `WEBINTEL_T3_WORKERS`，
1 = 串行，上限 8）。DirectML 的「同一 session 不可并发 Run」硬约束在 ROCm 上不存在：

| 并发 | 墙钟 | 成功 | 音频合计 | 聚合吞吐 |
|------|------|------|---------|---------|
| N=1 | 19.1s | 1/1 | 301s | 15.7x |
| N=2 | 21.5s | **2/2** | 496s | **23.1x** |
| N=4 | 25.7s | **4/4** | 611s | **23.8x** |

N=2 已吃到 97% 收益故默认 2。冷启动：2 个 worker 首次同时加载且 MIOpen 缓存为空时
就绪 46.6s（单 worker 11.6s），缓存热后 11.9 / 12.6s。端到端（4 条视频含网络与下载）**58s 完成 4/4**。

**③ 跨设备一致性**：同一次采样下 GPU 与 CPU 输出**逐字相同（相似度 1.0000）**。
注意 SenseVoice 本身**非逐字确定**：同一音频重复推理会在**固定位置**出现 1 字抖动
（759 字样本第 320、473 字，相似度 0.9987），**CPU 侧在完全相同位置同样抖动**
→ 属模型/浮点累加性质，**不是 ROCm 引入的**。故测试断言用「相似度 >0.99」而非逐字相等。

**④ 反面实测（红线）**：`WEBINTEL_T3_DEVICE=cpu` 在 ROCm venv 内跑同一 300s 音频，
**13.5 分钟累积 8052 CPU 秒（≈10 核满载）仍未完成**（强杀），对照主环境仅 57s
→ **约 140x 劣化**。CPU 兜底必须留在主环境。

### 踩坑记录（本机特有）
1. **funasr 日志污染协议**：funasr/modelscope/tqdm 都往 stdout 写。必须 dup2 改道，
   否则父进程 JSON 解析被进度条打断。测试断言 5b/5c 专门锁死这一点。2. **子进程日志缓冲**：调试崩溃时须 `python -u`，否则日志被缓冲吞掉、误判 exit=127。
3. **`subprocess.run(text=True)` 未指定 encoding**：Windows 默认 GBK，严格解码遇非 GBK
   字符直接崩 → stdout 空 → 误判「全部失败」。须 `text=True, encoding="utf-8", errors="replace"`。
4. **CPU 路径每视频重载模型**：`t3_sensevoice()` 每次调用都新建 `AutoModel`
   （日志会打印 4 次「加载 SenseVoice-Small」），这是 CPU 侧固定开销的主要来源，
   也是它比 GPU 常驻 worker 慢得多的原因之一。
5. **断言脚本自身编码**：Windows GBK 控制台下 emoji 断言标记会 `UnicodeEncodeError` 崩，
   须在脚本顶部 `sys.stdout.reconfigure(encoding="utf-8", errors="replace")`。
6. **CPU 秒数统计不能只算 root**：`psutil` 要遍历 `root.children(recursive=True)`，
   只统计 root 会得到 0（GPU 侧曾因此误显示 `cpu_secs=0.0`）。
7. **子进程输出必须落文件**：用 `stdout=subprocess.PIPE` + 轮询 `p.poll()` 时，
   子进程日志塞满管道会**死锁**（实测 600s 超时）。改为 `stdout=日志文件`。
8. **`tasklist` 在 gitbash 中无输出**；查进程 CPU 秒用
   `powershell Get-Process python | Select Id,CPU`。
9. **`local_files_only` 会泄漏进 transformers 的 `generate_kwargs`** →
   `ValueError: The following model_kwargs are not used by the model: ['local_files_only']`。
   离线加载改用 `HF_HUB_OFFLINE=1` + `TRANSFORMERS_OFFLINE=1`。
10. **`NO_PROXY` 里的方括号 IPv6 项会让 httpx 直接崩**（2026-10-07 实测）：本机
   `NO_PROXY=...,::1,[::1],r4.codes`，httpx 解析代理白名单时抛
   `httpx.InvalidURL: Invalid port: ':1]'`。faster-whisper 按**模型名**加载时会经
   huggingface_hub → httpx，于是**加载即崩**，CPU 兜底路径完全不可用。
   修法：`asr_fallback._sanitize_proxy_env()` 在模块导入时把 `[::1]` 归一为 `::1`。
11. **`WhisperModel("small")` 会联网解析模型，造成间歇性失败**：本机 huggingface.co
   不可达、`HF_ENDPOINT` 镜像也常断连，实测抛
   `RemoteProtocolError: Server disconnected without sending a response.`
   —— 同一用例**一次通过、一次失败**。修法：`asr_fallback._find_local_whisper()`
   先定位本地缓存快照目录再传入（与 `transcribe_video._find_local_model` 同口径），
   完全离线。`test_asr_gpu.py` 断言 8a/8c/8b 锁死这两条。

## 九、P1：段级时间戳 + `asr_fallback` 并入 GPU（2026-10-07 落地）

### 为什么必须扩展协议而不是丢时间戳
`asr_fallback.transcribe_audio()` 是 web-intel 里**第二处** CPU 转写点
（第一处是 `transcribe_video.py` 的 T3）。它的返回是**段级**结构
`[{from,to,content}]`，下游 `bili_daily_v2.py:471` 直接读 `s["from"]/s["to"]` 写
`seg_ts`，`:491` 再送 `merge_dups`（相邻相似度 >0.85 合并、时间取并集）。
若 GPU 通道只回纯文本，时间轴整条丢失 → 故用户裁定**扩展 worker 协议**。

### 协议与口径
| 项 | 值 |
|---|---|
| 父→子 | `{"audio": "<路径>", "with_timestamps": true}` |
| 子→父 | `{"ok":true, ..., "segments":[{"from":0.31,"to":1.67,"content":"..."}], "seg_count":N}` |
| 时间戳来源 | funasr `sentence_timestamp=True` → `record["sentence_info"]`（**start/end 为毫秒**） |
| 换算 | `/1000.0` → **秒**，与 faster-whisper 的 `s.start/s.end` 同口径 |
| 退化保护 | `sentence_info` 缺失但有文本时给单段 `[{from:0.0,to:0.0,content:text}]`，不丢文本 |
| 默认 | 不带 `with_timestamps` 时响应**不含** `segments`（协议载荷不无谓变大） |

### 客户端 API
- `T3RocmWorker.transcribe_full(path, with_timestamps=False)` → 完整响应 dict
- `T3RocmWorker.transcribe(path)` → 仍返回文本（**向后兼容**，`transcribe_video.py` 无需改动）
- `WorkerPool.transcribe_full(...)` / `WorkerPool.transcribe(...)` 同上

### asr_fallback 接入
- `_t3_gpu_segments(path)`：GPU 探针；`WEBINTEL_T3_DEVICE_MODE=cpu` 直接返回 None（回滚）
- `transcribe_audio(path, model=None, prefer_gpu=True)`：GPU 优先 → CPU faster-whisper 兜底
- `model=` 显式传入时**跳过 GPU**（测试打桩语义，`test_asr_gpu.py` 2a 锁死）

### 实测（300s 音频，`C:\Temp\audiotest\long5min.wav`）
| 项 | 结果 |
|---|---|
| `with_timestamps=True` | **91 段**，首段 `0.01→7.1`，段尾 `299.98s`（音频 300.0s） |
| 秒级口径正确性 | 若误用毫秒，段尾会是 299980 → 断言 9d/6a 锁死 |
| 加时间戳对正文的影响 | 相似度 **0.9987**（SenseVoice 本身非逐字确定，非时间戳引入） |
| `transcribe_audio()` GPU | 91 段；`prefer_gpu=False` CPU → 50 段；**key 集合完全相同** |

### 测试
| 套件 | 结果 |
|---|---|
| `test_t3_rocm.py` | **32/32 全绿**（原 25 + 新增 9a–9g）；`--red-ts` 可红性通过 |
| `test_t3_pool.py` | **25/25 全绿**（P0 未被破坏） |
| `test_asr_gpu.py` | **14/14 全绿**；`--red` 可红性通过 |
| `test_thread_limit.py` | 全绿 |

### 回滚
`--t3-device-mode cpu` / `WEBINTEL_T3_DEVICE_MODE=cpu` 彻底绕开 GPU；
ROCm venv 缺失时 `available()` 为 False 自动回落 CPU，功能不受影响。

## 十、P2b：SenseVoice 情感/事件标记结构化（2026-10-07 落地）

### 标签从哪来
SenseVoice 的输出**自带结构化标签**，形如

```
<|zh|><|NEUTRAL|><|BGM|><|withitn|>句1 <|zh|><|ANGRY|><|BGM|><|withitn|>句2 ...
```

`funasr/utils/postprocess_utils.py` 的 `emoji_dict`（L368–400）定义了两类：

| 类 | 标签 |
|---|---|
| 情感（8） | `HAPPY SAD ANGRY NEUTRAL FEARFUL DISGUSTED SURPRISED EMO_UNKNOWN` |
| 事件（12） | `BGM Speech Applause Laughter Cry Sneeze Breath Cough Sing Speech_Noise GBG Event_UNK` |
| 语言 / 规整 | `zh en yue ja ko nospeech` / `withitn woitn` |

⚠️ `rich_transcription_postprocess` 使用的前置映射表（L339–365）**少了**
`EMO_UNKNOWN`/`Sing`/`Speech_Noise`/`GBG` —— 两表不一致，取**并集**为准。

### ★ 段级标签只能从顶层 `record["text"]` 取（第一版踩的坑）
`sentence_info[i]["text"]` 已被 funasr **清洗过、不含任何标签**
（2026-10-07 实测：77 条 `sentence_info` 的 `keys=['start','end','text','sentence','timestamp']`，
`text`/`sentence` 全是纯句子）→ 从那里取标签会得到 `emotion_segments` **恒为空**。

正确做法：标签只存在于**顶层** `record["text"]`，按 `<|withitn|>` 切分对齐 ——

```python
tag_chunks = raw.split(_ITN_MARK)[1:]        # _ITN_MARK = "<|withitn|>"
for i2, si in enumerate(si_list):
    ...
    s_em, s_ev = _parse_meta(tag_chunks[i2] if i2 < len(tag_chunks) else "")
```

用 `enumerate` 的**原始下标** `i2`（不是过滤后的计数）取值，否则跳过空 content 的句
会让后续所有标签**整体错位**。

### meta 契约
```json
{"emotions": {"NEUTRAL": 69, "ANGRY": 1, "EMO_UNKNOWN": 7},
 "events": {"BGM": 77},
 "has_music": true,
 "music_ratio": 1.0,
 "emotion_segments": [{"from": 0.31, "to": 1.67, "emotion": "ANGRY", "events": ["BGM"]}]}
```

- **过滤规则**：`emotion in (None,"NEUTRAL")` 且事件只有 BGM 的段**不入列**。
  实测多数音频每句都带 `<|BGM|>`，全收进来列表等于全文；音乐覆盖改由 `music_ratio`
  表达。该音频 77 句 → 只剩 **8 条**（1 ANGRY + 7 EMO_UNKNOWN）。
- **不动 `segments` 契约**：meta 走独立顶层字段，`{from,to,content}` 保持原样
  （P1 的 `test_asr_gpu.py` 断言 7a 要求 GPU/CPU 段 key 集合完全相同）。
- **零额外算力**：只多一次字符串切分；标签本来就在模型输出里。
- **恒开时间戳**：`emotion_segments` 的区间需要 `sentence_info`，而配对基准
  （300s 音频、GPU 常驻、交替各 3 轮）显示 不带 **4.63s** vs 带 **4.78s** → **+3.2%**，
  在噪声量级内，故 meta 通道恒开 `with_timestamps=True`。

### 客户端 / 调度 API
| 函数 | 返回 |
|---|---|
| `t3_sensevoice_rocm_meta(path, worker=None)` | `(text, meta)` |
| `t3_sensevoice_rocm(path, worker=None)` | 文本（**P1 契约不变**，薄封装） |
| `t3_sensevoice_dispatch(path, worker=None)` | 2 元组 `(text, backend)`（**不变**） |
| `t3_sensevoice_dispatch_meta(path, worker=None)` | 3 元组 `(text, backend, meta)` |
| `build_markdown(..., audio_meta=None)` | **非空才写** `audio_meta`，既有产物格式不变 |

### 实测（`C:\Temp\t3audio\BV1cuZwYZEZr.m4a`，300.7s）
| 项 | 结果 |
|---|---|
| 端到端 `t3_sensevoice_dispatch_meta` | backend=`SenseVoice-Small(GPU)`，822 字 |
| meta | `emotions={'NEUTRAL':69,'ANGRY':1,'EMO_UNKNOWN':7}`、`events={'BGM':77}`、`has_music=True`、`music_ratio=1.0` |
| `emotion_segments` | **8 条**（首条 `0.31→1.67 ANGRY [BGM]`），区间为**秒级** |
| frontmatter | `audio_meta:` 行是合法 JSON |

### 测试
| 套件 | 结果 |
|---|---|
| `test_t3_meta.py`（新增） | **21/21 全绿**；`--red` 注入两处故障 → 3 条断言变红、exit 0 |
| `test_t3_rocm.py` | **32/32 全绿**（P1 未破坏） |
| `test_t3_pool.py` | **25/25 全绿**（P0 未破坏） |
| `test_asr_gpu.py` | **14/14 全绿**（P1 未破坏） |
| `test_thread_limit.py` | 全绿 |

断言 1b 直接从 funasr 源码读 `emoji_dict`，要求 `_EMOTION_TAGS | _EVENT_TAGS` 完全覆盖
—— **上游新增标签会立刻变红**，防静默漏统计。

### 回滚
meta 是**纯附加**：不消费 `meta` 的调用方行为逐字节不变；`build_markdown` 不传
`audio_meta` 时不写该字段。要彻底关掉只需不再调 `*_meta` 变体。


## 十一、P2：whisper-large-v3-turbo 质量兜底（2026-10-07 落地）

### 定位
降级链 `T1 → T2 → T3 → T4` 变为 **`T1 → T2 → T3 → T5 → T4`**。
T5 **只在 T3 返回 None 之后**才试，是「质量兜底」而非默认引擎；其下仍有 T4
（CPU faster-whisper）兜底。场景：无字幕轨 + SenseVoice 失败时，原兜底识别质量一般
且吃 CPU，T5 用同一套 GPU worker 跑 large-v3-turbo，质量显著更好。

### 为什么不是默认（探针实测，300.7s 音频 `C:\Temp\t3audio\BV1cuZwYZEZr.m4a`）
| 维度 | SenseVoice（T3） | whisper-large-v3-turbo（T5） |
|---|---|---|
| 稳态耗时 | **4.78s** | 12.36s（冷启 17.45s） |
| RTF | ~0.016 | 0.041（24.3x） |
| 标点 | 有 | **无** |
| 尾部 | 正常 | **幻觉超出音频时长 27.8s**（(298.48, 328.46)「优优独播剧场——YoYo Television Series Exclusive」，音频仅 300.7s） |
| 显存 | — | +1.79GB（懒加载） |
| meta | 有 | 无 |

### 引擎分派（复用同一套常驻 worker，不另起进程）
- worker 侧新增 `ENGINE_SENSEVOICE = "sensevoice"` / `ENGINE_WHISPER_TURBO = "whisper_turbo"`
  / `T5_MODEL_ID`（可由 `WEBINTEL_T5_MODEL` 覆盖）/ `_MODELS` 注册表 / `load_engine(engine)`。
- `load_engine()` **懒加载**：`sensevoice` 启动即预热（ready 即代表 T3 可用）；
  `whisper_turbo` 首次被请求才加载，避免每个 worker 白背 1.8GB 显存与 2s 加载。
- 协议：父 → 子多一个可选字段 `"engine"`；缺省 sensevoice。ready 信息新增 `engines` 与 `t5_model`。
- `serve()` 由 `serve(model)` 改为 **`serve()`（无参）**，内部按 `req["engine"]` 分派；
  `main()` 启动时 `_MODELS[ENGINE_SENSEVOICE] = model` 预热主引擎。
- 客户端：`T3RocmWorker.transcribe_full(audio_path, with_timestamps=False, engine=None)`
  与 `WorkerPool.transcribe_full(...)` 同步增加 `engine` 透传 —— **单例与池两条路径都覆盖**，
  由 `test_t5_turbo.py` 断言 1c / 1d / 4a 锁死。

### transcribe_video 侧
- 新增 `t5_whisper_turbo(audio_path, worker=None)`；`transcribe_one()` 的 T3 分支改为
  T3 失败 → T5 → T4，`transcriber` 依次标 `SenseVoice-Small(GPU)` /
  `whisper-turbo(GPU兜底)` / `faster-whisper(降级)`。
- CLI 新增 `--no-t5`；环境变量 `WEBINTEL_T5_ENABLE`（默认 `"1"`）。
  `WEBINTEL_T3_DEVICE_MODE=cpu` 时 T5 直接跳过（只在 GPU 上有意义）。

### 两个必须记住的坑
1. **>30s 音频必须 `return_timestamps=True`**，否则 transformers 直接抛
   `ValueError: You have passed more than 3000 mel input features (> 30 seconds) which automatically enables long-form generation which requires the model to predict timestamp tokens.`
   → T5 恒开时间戳（`with_timestamps` 只控制是否回传 `segments`）。
2. **`pipeline(..., local_files_only=True)` 会泄漏进 `generate_kwargs`** →
   `ValueError: The following 'model_kwargs' are not used by the model: ['local_files_only']`。
   离线只能靠 `HF_HUB_OFFLINE=1` + `TRANSFORMERS_OFFLINE=1`（worker 顶部 setdefault）。
   另：`torch_dtype` 在 transformers 5.19 已弃用（建议 `dtype`）；MIOpen 会告警
   `gfx1101_30.HIP.fdb.txt` 不可读（不影响运行）。

### 实测（主环境调用，2026-10-07）
| 用例 | 结果 |
|---|---|
| 单例 `transcribe_full(engine="whisper_turbo")` | ok=True / engine=whisper_turbo / **757 字** / 无 meta / 无 segments |
| 单例默认（不传 engine） | **822 字** / has_meta=True（**P1/P2b 行为不变**） |
| 池 `transcribe_full(engine="whisper_turbo")` | ok=True / engine=whisper_turbo / 757 字 |
| `tv.t5_whisper_turbo(AUDIO)` | 757 字 |
| `WEBINTEL_T5_ENABLE=0` / `mode=cpu` | 均返回 None（跳过） |

### 测试
`test_t5_turbo.py`：**17 条断言全绿**（4 组：静态契约 1a–1f / 开关降级 2a–2c /
真实 GPU 3a–3g / 池透传 4a）；`--red` → 通过 14 / 失败 3（**2a、2b、3b**），
「可红性验证通过」；`--no-gpu` 只跑 9 条非 GPU 断言。

### 回滚
`--no-t5`（或 `WEBINTEL_T5_ENABLE=0`）即恢复 P2 前的 `T3 → T4` 行为；
worker 侧不请求 `engine=whisper_turbo` 就永远不会加载 turbo 模型。



