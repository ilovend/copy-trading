"""Shared application paths.

Code lives in /opt/ct-app (read-only for instances).
Per-instance data lives under INSTANCE_ROOT.
"""
from __future__ import annotations

import os
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
APP_BOT = APP_ROOT / "bot"
APP_WEBUI = APP_ROOT / "webui"
INSTANCE_ROOT = Path(
    os.environ.get("INSTANCE_ROOT") or "/root/ct-instances/default"
).resolve()
BOT_DATA = INSTANCE_ROOT / "bot" / "data"
GUI_DIR = INSTANCE_ROOT / "gui"
ENV_FILE = INSTANCE_ROOT / ".env"
SESSION_PATH = INSTANCE_ROOT / "session.json"
COPY_CONFIG = GUI_DIR / "copy_config.json"
STRATEGY_CONFIG = GUI_DIR / "strategy_config.json"