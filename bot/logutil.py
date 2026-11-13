"""日志输出：控制台 + 可选文件（无人值守排障）。

从 strategy_engine 抽出，供引擎与各 Mixin 共用，避免循环 import。
"""
from __future__ import annotations

from datetime import datetime

_LOG_FILE = None


def set_log_file(path) -> None:
    global _LOG_FILE
    try:
        _LOG_FILE = open(path, "a", encoding="utf-8")
    except Exception:  # noqa: BLE001
        _LOG_FILE = None


def log(msg: str) -> None:
    line = f"{datetime.now():%H:%M:%S} {msg}"
    print(line, flush=True)
    if _LOG_FILE is not None:
        try:
            _LOG_FILE.write(line + "\n")
            _LOG_FILE.flush()
        except Exception:  # noqa: BLE001
            pass
