#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
t3_rocm_client: 主环境侧调用 T3 ROCm GPU 常驻 worker 的客户端
==============================================================
主环境（Python 3.12 + torch 2.12.1+cpu）**没有 GPU 版 torch**，而 ROCm 栈装在
独立虚拟环境 D:\\rocm101-venv（Python 3.13 + torch 2.14.0+rocm10.1.0）。两者
无法在同一进程内共存，因此采用**子进程 + 行分隔 JSON 协议**：

    transcribe_video.py (主环境, CPU torch)
        └─ subprocess ─→ D:\\rocm101-venv\\python.exe t3_rocm_worker.py
                          （torch ROCm + funasr + SenseVoice + fsmn-vad）

为什么要常驻（2026-10-07 实测，300s 音频，产品路径同配置带 fsmn-vad）：
    进程就绪固定开销 ≈15s（import 10.6s + 模型加载 4.7s）
    GPU 推理 5.9s（51.0x 实时） vs 主环境 CPU 27.4~34.1s（11.0x / 8.8x）
    → 每个视频都新起进程：5.9+15 = 20.9s，相对 CPU 仍有 1.3~1.6x，但浪费；
      常驻复用后第 2 个视频起只有 5.9s，净赚 4.6~5.7x。
批量转录是本技能的主用法（--list），所以常驻收益直接落在主路径上。

健壮性设计：
  - 首次失败（venv 缺失 / 依赖缺失 / GPU 不可用 / 超时）→ 自动回落 CPU funasr，
    并在本次进程内记住失败，不再重试，避免每个视频都白等一次超时
  - worker 中途崩溃 → 最多重启 1 次，再失败即永久回落
  - 所有通信带超时；子进程 stderr 落日志文件，失败时回读末尾若干行用于诊断
  - stdout 是协议专用通道，子进程内的库日志已全部改道 stderr（见 worker 内注释）
"""
import atexit
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time

# ---------------------------------------------------------------- 默认配置
DEFAULT_ROCM_PYTHON = r"D:\rocm101-venv\Scripts\python.exe"
WORKER_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "t3_rocm_worker.py")
READY_TIMEOUT = float(os.environ.get("WEBINTEL_T3_READY_TIMEOUT", "300"))
INFER_TIMEOUT = float(os.environ.get("WEBINTEL_T3_INFER_TIMEOUT", "3600"))
MAX_RESTARTS = 1


def _log(msg):
    print(f"  [T3-ROCm] {msg}", file=sys.stderr, flush=True)


class T3RocmWorker:
    """ROCm 常驻转录 worker 的客户端（进程内单例，见 get_worker）。"""

    def __init__(self, python_exe=None, device=None, tag=0):
        self.python_exe = (python_exe or os.environ.get("WEBINTEL_T3_ROCM_PYTHON")
                           or DEFAULT_ROCM_PYTHON)
        self.device = device or os.environ.get("WEBINTEL_T3_DEVICE", "cuda")
        self.tag = int(tag)         # 池内编号；0 表示单例，日志沿用旧文件名
        self.proc = None
        self.info = {}
        self._q = queue.Queue()
        self._reader = None
        self._restarts = 0
        self._dead = False          # 永久失效（不再尝试）
        # 池内每个 worker 单独一个日志文件，否则并发写同一文件无法诊断
        _name = "t3_rocm_worker.log" if not self.tag else f"t3_rocm_worker_{self.tag}.log"
        self._log_path = os.path.join(tempfile.gettempdir(), _name)
        self._log_fh = None

    # ------------------------------------------------------------ 可用性
    def available(self):
        """静态检查：venv 解释器与 worker 脚本是否就位。"""
        if not os.path.isfile(self.python_exe):
            self._dead = True
            _log(f"未找到 ROCm 解释器 {self.python_exe}，回落 CPU")
            return False
        if not os.path.isfile(WORKER_SCRIPT):
            self._dead = True
            _log(f"未找到 worker 脚本 {WORKER_SCRIPT}，回落 CPU")
            return False
        return True

    # ------------------------------------------------------------ 启动
    def start(self):
        """拉起子进程并等待 ready。成功返回 True。"""
        if self._dead:
            return False
        if self.proc is not None and self.proc.poll() is None:
            return True
        if not self.available():
            return False

        env = dict(os.environ)
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["WEBINTEL_T3_DEVICE"] = self.device
        env.setdefault("WEBINTEL_CPU_THREADS", os.environ.get("WEBINTEL_CPU_THREADS", "4"))

        try:
            self._log_fh = open(self._log_path, "a", encoding="utf-8", errors="replace")
            self._log_fh.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} "
                               f"start device={self.device} =====\n")
            self._log_fh.flush()
            kwargs = {}
            if os.name == "nt":
                kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            self.proc = subprocess.Popen(
                [self.python_exe, "-u", WORKER_SCRIPT],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=self._log_fh,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                env=env, **kwargs)
        except Exception as e:
            self._dead = True
            _log(f"启动失败：{type(e).__name__}: {e}，回落 CPU")
            return False

        self._q = queue.Queue()
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()

        t0 = time.perf_counter()
        msg = self._read_json(READY_TIMEOUT)
        if msg is None:
            _log(f"等待就绪超时（{READY_TIMEOUT:.0f}s），回落 CPU。日志尾部：{self._log_tail()}")
            self._kill()
            self._dead = True
            return False
        if not msg.get("ready"):
            _log(f"worker 报告不可用：{msg.get('error', msg)}，回落 CPU")
            self._kill()
            self._dead = True
            return False

        self.info = msg
        _log(f"GPU 就绪 {msg.get('gpu')} | torch {msg.get('torch')} | hip {msg.get('hip')} "
             f"| 就绪耗时 {time.perf_counter() - t0:.1f}s（模型加载 {msg.get('load_sec')}s）"
             f"{'' if not self.tag else f' | worker#{self.tag}'}")
        return True

    def _pump(self):
        """读取线程：把子进程 stdout 的每行放进队列。"""
        try:
            for line in self.proc.stdout:
                self._q.put(line)
        except Exception:
            pass
        finally:
            self._q.put(None)  # EOF 哨兵

    def _read_json(self, timeout):
        try:
            line = self._q.get(timeout=timeout)
        except queue.Empty:
            return None
        if line is None:
            return None
        line = line.strip()
        if not line:
            return self._read_json(timeout)
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            _log(f"协议解析失败，忽略一行：{line[:120]}")
            return self._read_json(timeout)

    def _log_tail(self, n=5):
        try:
            with open(self._log_path, encoding="utf-8", errors="replace") as f:
                return " | ".join(x.strip() for x in f.readlines()[-n:] if x.strip())[-400:]
        except OSError:
            return "(无日志)"

    # ------------------------------------------------------------ 转录
    def transcribe_full(self, audio_path, with_timestamps=False, engine=None):
        """转录单个音频，返回 worker 的完整响应 dict；失败返回 None。

        with_timestamps=True 时响应里带 segments=[{from,to,content}]（单位秒），
        供 asr_fallback 这类需要段级时间戳的调用方使用。
        engine：None/"" 用 worker 默认（sensevoice）；"whisper_turbo" 走 T5 兜底引擎
        （worker 侧懒加载，首次请求会多等约 2s 模型加载）。
        """
        for attempt in range(MAX_RESTARTS + 1):
            if not self.start():
                return None
            try:
                req = {"audio": audio_path}
                if with_timestamps:
                    req["with_timestamps"] = True
                if engine:
                    req["engine"] = engine
                self.proc.stdin.write(json.dumps(req, ensure_ascii=False) + "\n")
                self.proc.stdin.flush()
            except Exception as e:
                _log(f"写入请求失败（{type(e).__name__}），重启 worker 重试")
                self._kill()
                continue

            msg = self._read_json(INFER_TIMEOUT)
            if msg is None:
                _log(f"推理超时/worker 退出，日志尾部：{self._log_tail()}")
                self._kill()
                continue
            if msg.get("ok"):
                return msg
            _log(f"推理失败：{str(msg.get('error'))[:200]}")
            return None  # 单条失败不重试（多为音频本身问题），交由上层回落

        self._dead = True
        return None

    def transcribe(self, audio_path):
        """转录单个音频。成功返回文本；失败返回 None（调用方回落 CPU）。"""
        msg = self.transcribe_full(audio_path)
        return (msg.get("text") or None) if msg else None

    # ------------------------------------------------------------ 收尾
    def _kill(self):
        if self.proc is None:
            return
        try:
            if self.proc.poll() is None:
                self.proc.kill()
        except Exception:
            pass
        try:
            self.proc.stdin and self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.stdout and self.proc.stdout.close()
        except Exception:
            pass
        self.proc = None

    def close(self):
        """优雅关闭：发 shutdown，等待退出；超时则强杀。"""
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.proc.stdin.write(json.dumps({"cmd": "shutdown"}) + "\n")
                self.proc.stdin.flush()
                self.proc.wait(timeout=20)
                _log("worker 已优雅退出")
            except Exception:
                self._kill()
        self._kill()
        if self._log_fh:
            try:
                self._log_fh.close()
            except Exception:
                pass
            self._log_fh = None


# ---------------------------------------------------------------- 单例
_WORKER = None
_WORKER_DISABLED = False


def get_worker(python_exe=None, device=None):
    """进程内单例。返回 T3RocmWorker 或 None（表示应直接走 CPU）。"""
    global _WORKER, _WORKER_DISABLED
    if _WORKER_DISABLED:
        return None
    if _WORKER is None:
        _WORKER = T3RocmWorker(python_exe=python_exe, device=device)
        atexit.register(close_worker)
    return _WORKER


def disable(reason=""):
    """标记本次进程不再尝试 ROCm（例如上层已判定不可用）。"""
    global _WORKER_DISABLED
    _WORKER_DISABLED = True
    if reason:
        _log(f"停用 ROCm 通道：{reason}")


def close_worker():
    global _WORKER
    if _WORKER is not None:
        _WORKER.close()
        _WORKER = None


def is_available(python_exe=None):
    """只做静态检查（不起进程），供 CLI 预检用。"""
    w = T3RocmWorker(python_exe=python_exe)
    return w.available()


# ---------------------------------------------------------------- 常驻 worker 池
# 为什么池是安全的：DirectML 的硬约束是「同一 session 不可并发 Run」（实测 4 线程
# 并发 → GPU device removed 全崩）；ROCm/PyTorch 没有这个限制。2026-10-07 实测
# 4 个独立 worker 进程同时跑 4 条音频：4/4 成功，聚合吞吐 15.7x → 23.8x，
# 单 worker 显存仅 1.2~1.7GB / 15.98GB。N=2 已吃到 97% 的收益，故默认 2。
POOL_SIZE_ENV = "WEBINTEL_T3_WORKERS"
POOL_MAX = 8
_POOL = None


def parse_pool_size(size=None):
    """worker 池大小。size=None 时读 WEBINTEL_T3_WORKERS，默认 2。"""
    raw = size if size is not None else os.environ.get(POOL_SIZE_ENV, "2")
    try:
        n = int(raw)
    except (TypeError, ValueError):
        _log(f"{POOL_SIZE_ENV}={raw!r} 不是整数，按 1 处理")
        return 1
    if n < 1:
        return 1
    if n > POOL_MAX:
        _log(f"{POOL_SIZE_ENV}={n} 超过上限 {POOL_MAX}，按 {POOL_MAX} 处理")
        return POOL_MAX
    return n


class WorkerPool:
    """N 个常驻 worker 的池，用于批量并发转录。

    与单例的关系：**二者互斥使用**（批量用池、单条用单例），避免多起进程。
    线程安全：内部用 queue 分发空闲 worker；worker 失效后不再放回池。

    ★ 池一旦全部失效就 **永久 `_disabled`**（调用方必须自己 `recycle()`）：
    2026-10-07 实测，4 worker 跑到第 5 篇时全部 hipErrorLaunchFailure，
    此后 `transcribe_full()` 立即返回 None，后续 105 篇秒失败（558 条
    「N/N 个切片未转写」）。而**所有进程退出后**新起 worker 立刻正常
    （单 worker 600s 音频 12.7s 出 126 段）→ 故障是**池内累积性**的，
    整池重建即可自愈；不要把它误判成设备级挂死或输入问题。
    自愈责任在调用方：批量驱动应在「某篇全部切片失败」或每 N 篇时调
    `recycle()`（不能在这里自动重建——`recycle()` 会关掉正在被其它线程
    借用的 worker，并发提交时会打断在飞的切片）。
    """

    def __init__(self, size=None, python_exe=None, device=None):
        self.size = parse_pool_size(size)
        self._python_exe = python_exe      # 留给 recycle() 重建用
        self._device = device
        self._workers = [T3RocmWorker(python_exe=python_exe, device=device, tag=i)
                         for i in range(self.size)]
        self._free = queue.Queue()
        for w in self._workers:
            self._free.put(w)
        self._disabled = False
        self._dead_count = 0
        self._lock = threading.Lock()

    def recycle(self):
        """整池重建（close 全部 worker 后重新拉起），复位 `_disabled`/`_dead_count`。

        必须在**没有在飞转录**时调用（单线程批量的篇间是安全点）。
        代价 ≈ 每 worker 10–12s（模型加载 4–6s）。
        """
        self.close()
        self._workers = [T3RocmWorker(python_exe=self._python_exe,
                                      device=self._device, tag=i)
                         for i in range(self.size)]
        for w in self._workers:
            self._free.put(w)
        self._dead_count = 0
        self._disabled = False

    def available(self):
        """静态检查（不起进程）。"""
        if self._disabled or not self._workers:
            return False
        return self._workers[0].available()

    def first_time(self):
        """是否还没有任何 worker 已拉起（供上层只打印一次「首次等待」提示）。"""
        return not any((w.proc is not None) for w in self._workers)

    def transcribe_full(self, audio_path, with_timestamps=False, engine=None):
        """借一个空闲 worker 转录，返回完整响应 dict。全部失效返回 None。

        engine：None/"" 用 worker 默认（sensevoice）；"whisper_turbo" 走 T5 兜底引擎。
        """
        if self._disabled:
            return None
        try:
            w = self._free.get(timeout=INFER_TIMEOUT)
        except queue.Empty:
            _log("等待空闲 worker 超时，本条件回落 CPU")
            return None
        try:
            msg = w.transcribe_full(audio_path, with_timestamps=with_timestamps,
                                    engine=engine)
        except Exception as e:
            _log(f"worker#{w.tag} 异常 {type(e).__name__}: {e}")
            msg = None
        if w._dead:
            with self._lock:
                self._dead_count += 1
                if self._dead_count >= self.size:
                    self._disabled = True
                    _log(f"worker 池全部失效（{self.size} 个），本池永久停用；"
                         f"调用方需 recycle() 整池重建（见 WorkerPool docstring）")
            return None          # 失效的 worker 不再放回池
        self._free.put(w)
        return msg

    def transcribe(self, audio_path):
        """借一个空闲 worker 转录。返回文本；全部失效返回 None（调用方回落 CPU）。"""
        msg = self.transcribe_full(audio_path)
        return (msg.get("text") or None) if msg else None

    def close(self):
        for w in self._workers:
            try:
                w.close()
            except Exception:
                pass
        self._workers = []
        self._free = queue.Queue()


def get_pool(size=None, python_exe=None, device=None):
    """进程内单例池。返回 WorkerPool。"""
    global _POOL
    if _POOL is None:
        _POOL = WorkerPool(size=size, python_exe=python_exe, device=device)
        atexit.register(close_pool)
    return _POOL


def close_pool():
    global _POOL
    if _POOL is not None:
        _POOL.close()
        _POOL = None
