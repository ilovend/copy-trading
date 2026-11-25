"""币安网页版私有接口：登录态（session.json / .env）+ bapi 请求。

从 listen_copy 抽出来，复制引擎和工具不再依赖监听器。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.request
from pathlib import Path

from paths import INSTANCE_ROOT as ROOT
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


_COOKIE_LOCK = threading.Lock()


def _persist_session() -> None:
    """Write rotated cookies back without dropping other session fields."""
    try:
        old = {}
        if SESSION_PATH.exists():
            old = json.loads(SESSION_PATH.read_text(encoding="utf-8-sig"))
        old.update({k: v for k, v in SESSION.items() if v is not None})
        tmp = SESSION_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(old, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, SESSION_PATH)
        try:
            os.chmod(SESSION_PATH, 0o600)
        except OSError:
            pass
    except Exception as exc:  # noqa: BLE001
        _log(f"persist session failed: {exc}")


def _capture_set_cookie(resp) -> None:
    """Binance rotates web cookies via Set-Cookie; keep the latest values."""
    raw_headers = resp.headers
    values = raw_headers.get_all("Set-Cookie") or []
    if not values:
        return
    with _COOKIE_LOCK:
        cookie = HEADERS.get("cookie", "") or SESSION.get("cookie", "")
        pairs = []
        for part in cookie.split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            name, value = part.split("=", 1)
            pairs.append([name.strip(), value.strip()])
        index = {name: i for i, (name, _) in enumerate(pairs)}
        changed = False
        for raw in values:
            first = raw.split(";", 1)[0].strip()
            if "=" not in first:
                continue
            name, value = first.split("=", 1)
            name, value = name.strip(), value.strip()
            if not name:
                continue
            lower = raw.lower()
            delete = not value or "max-age=0" in lower
            if name in index:
                i = index[name]
                if delete:
                    pairs.pop(i)
                    index = {n: j for j, (n, _) in enumerate(pairs)}
                    changed = True
                elif pairs[i][1] != value:
                    pairs[i][1] = value
                    changed = True
            elif not delete:
                index[name] = len(pairs)
                pairs.append([name, value])
                changed = True
        if changed:
            new_cookie = "; ".join(f"{n}={v}" for n, v in pairs)
            SESSION["cookie"] = new_cookie
            HEADERS["cookie"] = new_cookie
            _persist_session()
            _log("session cookie updated from Set-Cookie")


def api(path: str, method: str = "POST", body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers=HEADERS, method=method)
    with urllib.request.urlopen(req, timeout=15) as r:
        _capture_set_cookie(r)
        return json.loads(r.read().decode())
