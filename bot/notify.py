"""通知与本地环境：读取 .env、Telegram 同步/异步发送。

从 listen_copy 抽出来，策略/复制/工具都直接依赖本模块，
不再需要「为了发一条 TG 去 import 监听器」。
"""
from __future__ import annotations

import os
import queue
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def load_env(path: Path | None = None) -> None:
    """把 .env 里的键写进 os.environ（不覆盖已有值）。"""
    path = Path(path) if path else ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
TG_PROXY = os.environ.get("TELEGRAM_PROXY", "").strip()


def tg_send(text: str) -> bool:
    """同步发送，失败重试一次。"""
    if not TG_TOKEN or not TG_CHAT:
        _log("telegram not configured")
        return False
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = urllib.parse.urlencode({
        "chat_id": TG_CHAT, "text": text, "disable_web_page_preview": "true",
    }).encode()
    handlers = []
    if TG_PROXY:
        handlers.append(urllib.request.ProxyHandler({"http": TG_PROXY, "https": TG_PROXY}))
    opener = urllib.request.build_opener(*handlers)
    for attempt in (1, 2):
        try:
            with opener.open(url, data=payload, timeout=10) as r:
                return r.status == 200
        except Exception as e:  # noqa: BLE001
            _log(f"telegram error ({attempt}/2): {e}")
            time.sleep(1)
    return False


_tg_q: "queue.Queue[str]" = queue.Queue(maxsize=200)


def _tg_worker() -> None:
    while True:
        text = _tg_q.get()
        try:
            tg_send(text)
        except Exception as e:  # noqa: BLE001
            _log(f"telegram worker error: {e}")


def tg_send_async(text: str) -> None:
    """旁路推送：入队即返回，网络慢或失败都不影响主流程。"""
    try:
        _tg_q.put_nowait(text)
    except queue.Full:
        _log("telegram 队列已满，丢弃一条推送")


threading.Thread(target=_tg_worker, daemon=True).start()
