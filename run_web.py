#!/usr/bin/env python3
"""Shared-code launcher for one instance's web."""
from __future__ import annotations

import logging
import os
import runpy
import sys
import zlib
from logging.handlers import RotatingFileHandler
from pathlib import Path

APP = Path(__file__).resolve().parent
for i, arg in enumerate(sys.argv):
    if arg == "--instance-root" and i + 1 < len(sys.argv):
        os.environ["INSTANCE_ROOT"] = sys.argv[i + 1]
        break
sys.path.insert(0, str(APP / "bot"))
ROOT = Path(os.environ.get("INSTANCE_ROOT") or "/root/ct-instances/default").resolve()

logger = logging.getLogger("ct-web")
logger.setLevel(logging.INFO)
logger.propagate = False
handler = RotatingFileHandler(ROOT / "web.log", maxBytes=2 * 1024 * 1024,
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

sys.argv = ["webui/app.py"]
runpy.run_path(str(APP / "webui/app.py"), run_name="__main__")
