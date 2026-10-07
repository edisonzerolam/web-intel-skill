#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
req_budget: 全局请求预算（TC-20260811-BILI6 V8 ③）
====================================================
跨进程统一 B站日请求计数：文件锁 + 日账本（data/req_ledger.json）。
阈值：160 预警（字幕候选降级）/ 200 硬停；412/429 写 blocked 当日全停。

用法（三入口统一）：
  from req_budget import budget
  budget.spend("subs")        # 返回 0 正常 / -1 预警 / 抛 SystemExit 硬停
  budget.block()              # 412/429 时调用，当日全停
"""
import json
import os
import sys
import time
from datetime import date

LEDGER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "req_ledger.json")
WARN = 160
HARD = 200


def _lock_file(path):
    """Windows 文件锁（msvcrt.locking）；跨平台回退无锁。"""
    try:
        import msvcrt
        f = open(path + ".lock", "a+")
        msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
        return f
    except Exception:
        return None


def _unlock(f):
    try:
        if f is not None:
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            f.close()
    except Exception:
        pass


def _read():
    if os.path.exists(LEDGER):
        try:
            return json.load(open(LEDGER, encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _write(data):
    os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
    tmp = LEDGER + ".tmp"
    json.dump(data, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    os.replace(tmp, LEDGER)


class Budget:
    def __init__(self):
        self._today = date.today().isoformat()

    def spend(self, channel="main", n=1):
        """原子计数；返回 0 正常 / -1 预警；超硬停抛 SystemExit。"""
        today = date.today().isoformat()
        lock = _lock_file(LEDGER)
        try:
            data = _read()
            if data.get("date") != today:  # 跨零点 rollover（锁内判定）
                data = {"date": today, "count": 0, "warned": False, "blocked": False,
                        "by_channel": {}}
            if data.get("blocked"):
                raise SystemExit(f"[预算] 当日已 blocked（412/429），停止一切请求。")
            data["count"] = data.get("count", 0) + n
            data["by_channel"][channel] = data["by_channel"].get(channel, 0) + n
            if data["count"] >= HARD:
                data["blocked"] = True
                _write(data)
                raise SystemExit(f"[预算] 已达日硬停 {HARD}，当日不再发起任何请求。")
            if data["count"] >= WARN and not data.get("warned"):
                data["warned"] = True
                _write(data)
                return -1  # 预警：调用方降级（字幕候选 N→25）
            _write(data)
            return 0
        finally:
            _unlock(lock)

    def block(self):
        """412/429 当日全停标记。"""
        lock = _lock_file(LEDGER)
        try:
            data = _read()
            data.setdefault("date", date.today().isoformat())
            data["blocked"] = True
            _write(data)
        finally:
            _unlock(lock)

    def summary(self):
        data = _read()
        return f"{data.get('count', 0)}/{HARD}（预警 {WARN}）blocked={data.get('blocked', False)}"


budget = Budget()
