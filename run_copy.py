#!/usr/bin/env python3
"""Shared-code launcher for one instance's copy."""
from __future__ import annotations

import logging
import os
import runpy
import signal
import sys
import zlib
from logging.handlers import RotatingFileHandler
from pathlib import Path

APP = Path(__file__).resolve().parent
for i, arg in enumerate(sys.argv):
    if arg == "--instance-root" and i + 1 < len(sys.argv):
        os.environ["INSTANCE_ROOT"] = sys.argv[i + 1]
        break
ROOT = Path(os.environ.get("INSTANCE_ROOT") or "/root/ct-instances/default").resolve()
sys.path.insert(0, str(APP / "bot"))
from lockport import lock_port
import instance_status

os.environ.setdefault("TRADE_LOCK_PORT", str(lock_port()))
_instance_cfg = ROOT / "gui/copy_config.json"
try:
    _cfg = __import__("json").loads(_instance_cfg.read_text(encoding="utf-8"))
except Exception:
    _cfg = {}

logger = logging.getLogger("ct-copy")
logger.setLevel(logging.INFO)
logger.propagate = False
handler = RotatingFileHandler(ROOT / "copy.log", maxBytes=2 * 1024 * 1024,
                              backupCount=3, encoding="utf-8")
handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
logger.addHandler(handler)

class _Stream:
    def write(self, message: str) -> int:
        if message:
            for line in message.splitlines():
                if line:
                    logger.info(line)
        return len(message)
    def flush(self) -> None:
        for h in logger.handlers:
            h.flush()
    def isatty(self) -> bool:
        return False

sys.stdout = _Stream()
sys.stderr = _Stream()

def _on_term(signum, frame):
    instance_status.update("copy", running=False, mode=_cfg.get("mode", "live_dry"), lock_port=os.environ.get("TRADE_LOCK_PORT"))
    raise SystemExit(143)


signal.signal(signal.SIGTERM, _on_term)
signal.signal(signal.SIGINT, _on_term)
sys.argv = ["copy_run.py"]
instance_status.update("copy", running=True, mode=_cfg.get("mode", "live_dry"), lock_port=os.environ.get("TRADE_LOCK_PORT"))
try:
    runpy.run_path(str(APP / "bot/copy_run.py"), run_name="__main__")
finally:
    instance_status.update("copy", running=False, mode=_cfg.get("mode", "live_dry"), lock_port=os.environ.get("TRADE_LOCK_PORT"))
