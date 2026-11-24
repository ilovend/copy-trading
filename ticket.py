#!/usr/bin/env python3
"""Issue a 30-second, one-time cockpit login ticket for an instance."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import time
from pathlib import Path

ROOT = Path(os.environ.get("INSTANCE_ROOT") or "/root/ct-instances/default").resolve()
RUNTIME = ROOT / "runtime"
TICKETS = RUNTIME / "tickets.json"


def atomic_write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ttl", type=int, default=30)
    args = ap.parse_args()
    RUNTIME.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(TICKETS.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    now = time.time()
    data = {k: v for k, v in data.items() if isinstance(v, dict) and float(v.get("exp", 0)) > now}
    token = secrets.token_urlsafe(32)
    data[hashlib.sha256(token.encode()).hexdigest()] = {
        "exp": now + max(5, min(args.ttl, 120)),
        "used": False,
    }
    atomic_write(TICKETS, data)
    print(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
