# web-intel

> 网络情报超级技能：**取得到（Acquire）→ 读得懂（Enrich）→ 判得准（Assess）→ 交得出（Deliver）**。

一个给 AI Agent 用的技能包（Claude Skills / Agent Skills 格式）。它不是 SDK，而是**路由层 + 一组可执行脚本 + 一套研究方法论**：把「搜、抓、转、读、判、写报告」这条链路拆成可复用的子技能，并明确每步该读哪份参考文档。

## 这是什么

| 阶段 | 干什么 | 用什么 |
|---|---|---|
| **A 获取** | 找到并取回原始材料 | `anysearch` 实时搜索 / `browser-cdp` 真浏览器 / `baoyu-url-to-markdown` |
| **E 富化** | 转格式、抽正文、转字幕、做笔记 | `baoyu-*` 系列 / `bili-daily` 的 `enrich_note.py` |
| **As 评估** | 分级、交叉验证、对抗审查 | `deep-research` / `references/claims-ledger.md` / `references/ach.md` |
| **D 交付** | 成报告 / 简报 / 数据集 | `deep-research*` 报告模板 + `references/digital-evidence.md` |

简单任务不必走满四阶段——一条搜索能答完的，别上管道。

## 目录结构

```
web-intel/
├── SKILL.md                    # 路由层（先读这个）
├── references/                 # 方法论（按需读，不复制进主文件）
│   ├── research-discipline.md  # 调研纪律（深度调研默认强制读）
│   ├── claims-ledger.md        # 论断账本
│   ├── ach.md                  # 竞争性假设分析（Heuer ACH）
│   ├── digital-evidence.md     # 数字取证规范
│   ├── source-dispatch.md      # 信源分派
│   ├── competitor-intel.md     # 竞品情报
│   ├── pivot.md                # 转向判断
│   └── channel-notes.md        # 渠道笔记
├── scripts/
│   └── self_evolve.py          # 自我进化日志（记录走错的路，供后续避坑）
└── skills/
    └── bili-daily/             # 视频平台内容洞察子技能
        ├── SKILL.md
        ├── references/
        ├── platforms/
        └── *.py                # 抓取 / 转写 / 分析脚本
```

## 安装

把本仓库内容放进 Agent 的技能目录：

```bash
git clone https://github.com/edisonzerolam/web-intel-skill.git
# 然后放到技能目录，例如：
#   ~/.agents/skills/web-intel/
#   ~/.claude/skills/web-intel/
```

入口是 `SKILL.md`。它只做路由，会按需把具体子技能的 `SKILL.md` 载入上下文。

## bili-daily：B站字幕与每日洞察

这是本仓库里体量最大、也最"实战"的子技能。核心能力：

- **字幕抓取**：分类榜 top20 / 关键词榜 / UP 订阅 / 观看历史，多级降级链
- **转写兜底**：T1 官方 API 字幕 → T2 yt-dlp → T3 SenseVoice（GPU）→ T5 whisper-large-v3-turbo（GPU）→ T4 faster-whisper（CPU）
- **内容理解**：每日热门在说什么、趋势周报、情感/事件结构化标记
- **合规限速**：请求预算账本（`req_budget.py`），并发下带锁的全局最小间隔

### 字幕获取的降级链

```
T1  B站官方字幕 API（需登录 cookie）
T2  yt-dlp（与 T1 看到同一份字幕可得性，增量约 0）
T3  SenseVoice-Small on ROCm GPU（常驻 worker，段级时间戳 + 情感/事件）
T5  whisper-large-v3-turbo（质量兜底，比 T3 慢约 2.6x）
T4  faster-whisper（CPU 兜底）
```

> **实测提醒**：T1 命中率约 **53%（n=30）/ 64%（n=120）**，未命中 100% 是「UP 本身未开 AI 字幕」，与视频长度/类型无关。这是**内容侧**决定的，没有源头优化空间——所以后面的 GPU 转写链路才是必要的。

### AMD GPU 加速（本仓库的实测结论）

如果你也只有 AMD 卡、没有 NVIDIA/CUDA，这份踩坑记录能省很多时间（详见 `skills/bili-daily/references/model-deploy.md`）：

| 路线 | 结论 |
|---|---|
| **ROCm + PyTorch** | ✅ **可行且推荐**。AMD HIP SDK for Windows 官方支持 RDNA3（gfx1101）。实测 RX 7800 XT 上 SenseVoice 相对 CPU **推理 1.37x / 端到端 1.22x**，但**CPU 秒数约减半**（33s vs 57s）——对"CPU 占用高"这个诉求才是关键收益 |
| **DirectML** | ⚠️ 能用但有硬约束：同一 session 不可并发 `Run`，多线程直接 `GPU device removed` |
| **torch-directml** | ❌ 锁死 `torch==2.4.1`，与新版 torch 不兼容 |
| **CTranslate2 / faster-whisper** | ❌ 官方构建为 CUDA-only，无 ROCm/DirectML 后端 |
| **ONNX Runtime ROCm EP** | ❌ 自 1.23 起移除，历史 wheel 全为 Linux |
| **whisper.cpp + Vulkan** | ⚠️ 硬件层完全通（Vulkan 正常枚举 7800 XT），但官方无 Windows 预编译二进制，需自建 8–10GB 工具链 |

**架构要点**：主环境（CPU torch）与 ROCm venv（`torch+rocm`）**无法同进程共存**，因此 T3 采用**子进程隔离 + 行分隔 JSON 协议**的常驻 worker，并用 worker 池（默认 2 路）吃掉并发。单 worker ≈15.7x 实时，2 worker ≈23.1x 实时；再往上收益饱和（97% 收益在 2 路时已吃到）。

> ⚠️ **红线**：不要在 ROCm venv 里跑 CPU 兜底。同一段 300s 音频会劣化到约 **140x** 慢。CPU 兜底必须留在主环境。

### 段级时间戳与情感标记

T3 worker 支持 `with_timestamps`，返回 `[{from, to, content}]`（秒级）；开启 `meta` 后额外返回情感/事件标记（SenseVoice 的 8 类情感 + 12 类事件）、`music_ratio` 等。实测时间戳开销约 **+3.2%**（噪声量级内，故恒开）。

## 凭据配置

**本仓库不含任何凭据。** 需要 B站登录态的脚本请自备 cookie：

```bash
cd skills/bili-daily
cp cookies.txt.example cookies.txt
# 编辑 cookies.txt，填入自己的 SESSDATA / bili_jct
```

`cookies.txt`、`.env`、以及所有运行期数据文件均已在 `.gitignore` 中排除。

## 运行测试

`skills/bili-daily/` 下自带测试，纯断言式、无外部依赖：

```bash
cd skills/bili-daily
python test_thread_limit.py     # 线程限制
python test_t3_rocm.py          # T3 ROCm worker（需 ROCm venv）
python test_t3_pool.py          # worker 池
python test_asr_gpu.py          # GPU/CPU 一致性
python test_t3_meta.py          # 情感/事件元数据
python test_t5_turbo.py         # whisper-turbo 兜底
```

多数测试支持 `--red` 注入故障以验证"测试本身是有效的"（可红性），以及 `--no-gpu` 跳过 GPU 相关断言。

## 许可

MIT，见 [LICENSE](LICENSE)。

## 说明

仓库中的 `references/` 记录了大量一手实测数据（AMD 生态调研、ct-punc 分块扫描、T1 命中率统计等）。这些结论带有明确的环境标注与样本量，**引用时请连同前提一起引用**——例如"T1 命中率 53%"是 n=30 的配对样本结果，不是普适常数。
