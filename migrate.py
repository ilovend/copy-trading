#!/usr/bin/env python3
"""One-time migration from an old root into an instance data directory.

Does not copy application code and does not start any service.
Usage:
  INSTANCE_ROOT not required for this tool.
  python3 migrate.py --source /root/ct-router --id a1 --port 8787 \
      --copy-config-from /root/ct-instances/seed/gui/copy_config.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path

NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
DEFAULT_ROOT = Path("/root/ct-instances")


def atomic_json(path: Path, value: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def copy_file(src: Path, dst: Path, mode: int | None = None) -> bool:
    if not src.is_file():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    if mode is not None:
        os.chmod(dst, mode)
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True)
    ap.add_argument("--id", required=True)
    ap.add_argument("--root", default=str(DEFAULT_ROOT))
    ap.add_argument("--port", type=int, default=None, help="legacy port metadata only")
    ap.add_argument("--copy-config-from", default="")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not NAME_RE.fullmatch(args.id):
        print("error=invalid instance id")
        return 2
    src = Path(args.source).resolve()
    if not (src / "bot" / "copy_run.py").is_file():
        print(f"error=source is not a copy-trading root: {src}")
        return 2
    dest = Path(args.root) / args.id
    if dest.exists():
        print(f"error=destination exists: {dest}")
        return 2

    plan = [
        "env" if (src / ".env").is_file() else "env-missing",
        "session" if (src / "session.json").is_file() else "session-missing",
        "strategy-config" if (src / "gui" / "strategy_config.json").is_file() else "strategy-default",
        "copy-config" if (src / "gui" / "copy_config.json").is_file() else "copy-config-seed",
        "bot-data" if (src / "bot" / "data").is_dir() else "bot-data-empty",
    ]
    print(f"source={src}")
    print(f"dest={dest}")
    print("plan=" + ",".join(plan))
    if args.dry_run:
        print("dry_run=1")
        return 0

    dest.mkdir(parents=True, exist_ok=False)
    (dest / "gui").mkdir(parents=True, exist_ok=True)
    (dest / "bot" / "data").mkdir(parents=True, exist_ok=True)

    # Secrets and login state
    copy_file(src / ".env", dest / ".env", 0o600)
    copy_file(src / "session.json", dest / "session.json", 0o600)

    # Strategy config, preserving the existing logic/config
    copy_file(src / "gui" / "strategy_config.json", dest / "gui" / "strategy_config.json")

    # Copy configuration: source first, then seed, then safe default
    copied_copy = copy_file(src / "gui" / "copy_config.json", dest / "gui" / "copy_config.json")
    if not copied_copy and args.copy_config_from:
        copied_copy = copy_file(Path(args.copy_config_from), dest / "gui" / "copy_config.json")
    if copied_copy:
        try:
            cfg = json.loads((dest / "gui" / "copy_config.json").read_text(encoding="utf-8"))
            cfg["mode"] = "live_dry"
            atomic_json(dest / "gui" / "copy_config.json", cfg)
        except Exception:
            pass
    if not copied_copy:
        cfg = {
            "mode": "shadow",
            "ratio_mode": "net_equity",
            "exec_equity": 100.0,
            "max_order_notional": 10.0,
            "max_total_notional": 40.0,
            "k": 1.0,
            "fixed_notional_usdt": 5.0,
            "margin_per_trade": 5.0,
            "leverage": 20,
            "monitored": [],
            "targets": {},
        }
        atomic_json(dest / "gui" / "copy_config.json", cfg)

    # Keep historical strategy state/blacklists, but not growing logs.
    src_data = src / "bot" / "data"
    if src_data.is_dir():
        ignore = shutil.ignore_patterns("__pycache__", "*.pyc", "*.log", "*.jsonl")
        shutil.copytree(src_data, dest / "bot" / "data", dirs_exist_ok=True, ignore=ignore)

    env_keys = set()
    if (dest / ".env").is_file():
        for line in (dest / ".env").read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                env_keys.add(line.split("=", 1)[0].strip())

    metadata = {
        "schema_version": 2,
        "id": args.id,
        "legacy_source": str(src),
        "legacy_port": args.port,
        "web_port": None,
        "account": {
            "label": "",
            "api_state": "configured" if {"EXEC_API_KEY", "EXEC_API_SECRET"} <= env_keys else "missing",
            "account_group": "",
        },
        "engines": {
            "copy": {"enabled": False, "config": "gui/copy_config.json"},
            "strategy": {"enabled": False, "config": "gui/strategy_config.json"},
            "active_live": "none",
        },
        "risk": {"trade_lock": "auto"},
        "notify": {"telegram": bool({"TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"} & env_keys),
                   "wechat": "WECHAT_WEBHOOK" in env_keys},
    }
    atomic_json(dest / "instance.json", metadata)
    print(f"ok={args.id} session={'yes' if (dest / 'session.json').is_file() else 'no'} "
          f"api={metadata['account']['api_state']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
