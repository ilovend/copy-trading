"""币安网页版私有接口：登录态（session.json / .env）+ bapi 请求。

从 listen_copy 抽出来，复制引擎和工具不再依赖监听器。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://www.binance.com"

SESSION_PATH = ROOT / "session.json"
LEGACY_SESSION = ROOT / "probe" / "session.json"
SESSION: dict = {}
HEADERS: dict[str, str] = {}
SESSION_MTIME = 0.0

DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36 Edg/157.0.0.0")


def _log(msg: str) -> None:
    print(f"[bapi] {msg}", flush=True)


def _session_file() -> Path:
    """登录态文件：根目录 session.json 优先；老版本在 probe/ 下则自动迁移。"""
    if SESSION_PATH.exists():
        return SESSION_PATH
    if LEGACY_SESSION.exists():
        try:
            import shutil
            shutil.copy2(LEGACY_SESSION, SESSION_PATH)
            _log("已将 probe/session.json 迁移到根目录 session.json")
        except Exception:  # noqa: BLE001
            return LEGACY_SESSION
    return SESSION_PATH


def load_session() -> None:
    """Session source priority: .env (BINANCE_COOKIE + BINANCE_CSRFTOKEN) > session.json."""
    global SESSION, HEADERS, SESSION_MTIME
    file_sess: dict = {}
    path = _session_file()
    if path.exists():
        file_sess = json.loads(path.read_text(encoding="utf-8-sig"))
        SESSION_MTIME = path.stat().st_mtime

    env_cookie = os.environ.get("BINANCE_COOKIE", "").strip()
    env_csrf = os.environ.get("BINANCE_CSRFTOKEN", "").strip()
    if env_cookie and env_csrf:
        SESSION = {
            **file_sess,
            "cookie": env_cookie,
            "csrftoken": env_csrf,
            "user-agent": os.environ.get("BINANCE_UA", "").strip()
                          or file_sess.get("user-agent") or DEFAULT_UA,
        }
        _log("auth: using BINANCE_COOKIE / BINANCE_CSRFTOKEN from .env")
    else:
        SESSION = file_sess
        if not SESSION.get("cookie"):
            sys.exit("no session: run `python bot/update_session.py --clipboard` "
                     "or set BINANCE_COOKIE + BINANCE_CSRFTOKEN in .env")
        if not SESSION.get("csrftoken"):
            _log("WARN: session.json has no csrftoken, requests will likely return 401")

    HEADERS = {
        "accept": "*/*",
        "accept-language": "en,zh-CN;q=0.9,zh;q=0.8,en-GB;q=0.7,en-US;q=0.6",
        "bnc-level": "0",
        "bnc-time-zone": "Asia/Shanghai",
        "clienttype": "web",
        "content-type": "application/json",
        "lang": "zh-CN",
        "origin": BASE,
        "referer": f"{BASE}/zh-CN/copy-trading/copy-management",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "user-agent": SESSION.get("user-agent", DEFAULT_UA),
        "cookie": SESSION.get("cookie", ""),
    }
    if SESSION.get("csrftoken"):
        HEADERS["csrftoken"] = SESSION["csrftoken"]
    for k in ("bnc-uuid", "fvideo-id", "fvideo-token",
              "x-passthrough-token", "device-info"):
        if SESSION.get(k):
            HEADERS[k] = SESSION[k]


def reload_session_if_changed() -> bool:
    try:
        mtime = SESSION_PATH.stat().st_mtime
    except OSError:
        return False
    if mtime == SESSION_MTIME:
        return False
    load_session()
    _log("session.json changed -> reloaded")
    return True


def api(path: str, method: str = "POST", body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers=HEADERS, method=method)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())
