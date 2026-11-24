#!/usr/bin/env python3
"""Stable account lock port.

Priority: instance.json account.account_id / account.account_group, then the
first 12 chars of EXEC_API_KEY as a fallback. Both copy and strategy engines
must call this and export TRADE_LOCK_PORT.
"""
from __future__ import annotations

import json
import os
import zlib
from pathlib import Path

ROOT = Path(os.environ.get("INSTANCE_ROOT") or "/root/ct-instances/default").resolve()


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _env_value(key: str) -> str:
    try:
        for line in (ROOT / ".env").read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return ""


def identity() -> str:
    meta = _read_json(ROOT / "instance.json")
    account = meta.get("account") if isinstance(meta.get("account"), dict) else {}
    for key in ("account_id", "account_group"):
        value = str(account.get(key) or "").strip()
        if value:
            return value
    return _env_value("EXEC_API_KEY")[:12]


def lock_port() -> int:
    value = identity()
    return 47000 + (zlib.crc32(value.encode("utf-8")) % 2000) if value else 47620
