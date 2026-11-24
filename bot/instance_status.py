#!/usr/bin/env python3
"""Atomic per-instance runtime status shared by engine and cockpit/manager."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

ROOT = Path(os.environ.get("INSTANCE_ROOT") or "/root/ct-instances/default").resolve()
RUNTIME = ROOT / "runtime"
STATUS = RUNTIME / "runtime-status.json"


def _read() -> dict:
    try:
        value = json.loads(STATUS.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def update(role: str, **fields) -> None:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    data = _read()
    item = data.get(role) if isinstance(data.get(role), dict) else {}
    item.update(fields)
    item["pid"] = os.getpid()
    item["updated"] = int(time.time())
    data[role] = item
    tmp = STATUS.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, STATUS)
