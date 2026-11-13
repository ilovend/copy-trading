#!/usr/bin/env python3
"""极简 Web 控制台：手机浏览器查看/控制路由器上的策略。

无第三方依赖，只用 stdlib。默认监听 0.0.0.0:8787，HTTP Basic 认证。
环境变量：
    CT_ROOT       项目目录，默认 /root/ct-router
    CT_INIT       init 脚本，默认 /etc/init.d/copy-strategy
    WEB_PASSWORD  登录密码（必须设置）
    WEB_PORT      端口，默认 8787
"""
from __future__ import annotations

import base64
import hmac
import json
import os
import subprocess
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(os.environ.get("CT_ROOT", "/root/ct-router")).resolve()
BOT = ROOT / "bot"
if str(BOT) not in sys.path:
    sys.path.insert(0, str(BOT))

def _load_dotenv() -> None:
    path = ROOT / ".env"
    if not path.exists():
        return
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except Exception:
        pass


_load_dotenv()

INIT = os.environ.get("CT_INIT", "/etc/init.d/copy-strategy")
PASSWORD = os.environ.get("WEB_PASSWORD", "")
PORT = int(os.environ.get("WEB_PORT", "8787"))
INDEX = Path(__file__).resolve().parent / "index.html"
STATUS = ROOT / "bot" / "data" / "strategy-status.json"
POSITIONS = ROOT / "bot" / "data" / "strategy-open.json"
LOG = ROOT / "strategy.log"
CONFIG = ROOT / "gui" / "strategy_config.json"

_ACCOUNT = {"t": 0.0, "data": None}
_LOCK = threading.Lock()

# 允许 Web 修改的字段：点路径 -> (类型, 最小值, 最大值)
FIELDS = {
    "mode": (str, None, None),
    "interval": (str, None, None),
    "signal_interval": (str, None, None),
    "direction": (str, None, None),
    "trade.notional": (float, 1, 100),
    "trade.max_positions": (int, 1, 5),
    "trade.max_daily_loss": (float, 1, 200),
    "trade.max_spread_pct": (float, 0, 2),
    "trade.entry_mode": (str, None, None),
    "trade.leverage": (int, 1, 50),
    "basket.notional": (float, 1, 100),
    "basket.basket_tp_pct": (float, 0.1, 20),
    "basket.hard_stop_pct": (float, 0.5, 30),
    "basket.add_step_pct": (float, 0.5, 10),
    "basket.max_adds": (int, 0, 10),
    "basket.trail_activate_pct": (float, 0, 20),
    "basket.trail_gap_pct": (float, 0, 10),
}
ENUMS = {
    "mode": {"shadow", "live_dry", "live"},
    "interval": {"1m", "3m", "5m", "15m", "30m", "1h"},
    "signal_interval": {"1m", "3m", "5m", "15m", "30m", "1h"},
    "direction": {"short", "long", "both"},
    "trade.entry_mode": {"taker", "maker"},
}


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def get_path(data: dict, key: str):
    cur = data
    for part in key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def set_path(data: dict, key: str, value) -> None:
    parts = key.split(".")
    cur = data
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def service_status() -> str:
    try:
        r = subprocess.run([INIT, "status"], capture_output=True, text=True, timeout=3)
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        if "running" in out.lower():
            return "running"
        if "stopped" in out.lower() or "inactive" in out.lower():
            return "stopped"
        return out[:80] or "unknown"
    except Exception as e:  # noqa: BLE001
        return f"unknown ({str(e)[:60]})"


def tail_log(n: int = 160) -> list[str]:
    try:
        if not LOG.exists():
            return []
        raw = LOG.read_bytes()
        if len(raw) > 512_000:
            raw = raw[-512_000:]
        text = raw.decode("utf-8", "replace")
        return text.splitlines()[-max(10, min(n, 500)):]
    except Exception as e:  # noqa: BLE001
        return [f"读取日志失败: {e}"]


def account() -> dict:
    with _LOCK:
        if time.time() - _ACCOUNT["t"] < 5 and _ACCOUNT["data"] is not None:
            return _ACCOUNT["data"]
    data = {"ok": False, "error": ""}
    try:
        from exec_client import ExecClient
        data = {"ok": True, **ExecClient().test()}
    except Exception as e:  # noqa: BLE001
        data["error"] = str(e)[:160]
    with _LOCK:
        _ACCOUNT.update(t=time.time(), data=data)
    return data


def snapshot() -> dict:
    st = read_json(STATUS, {})
    pos = read_json(POSITIONS, {})
    cfg = read_json(CONFIG, {})
    return {
        "now": int(time.time()),
        "service": service_status(),
        "status": st,
        "positions": pos if isinstance(pos, dict) else {},
        "config": {k: get_path(cfg, k) for k in FIELDS},
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "copy-web/1.0"

    def log_message(self, fmt, *args):  # noqa: D401
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _authorized(self) -> bool:
        if not PASSWORD:
            return False
        h = self.headers.get("Authorization", "")
        if not h.startswith("Basic "):
            return False
        try:
            raw = base64.b64decode(h[6:]).decode("utf-8")
            _, pw = raw.split(":", 1)
        except Exception:  # noqa: BLE001
            return False
        return hmac.compare_digest(pw, PASSWORD)

    def _need_auth(self) -> None:
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="copy-trading"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _json(self, data, code: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        return json.loads(raw.decode("utf-8") or "{}")

    def _same_origin(self) -> bool:
        return self.headers.get("X-Requested-With") == "copy-webui"

    def do_GET(self) -> None:  # noqa: N802
        if not self._authorized():
            return self._need_auth()
        if self.path == "/" or self.path.startswith("/?"):
            try:
                body = INDEX.read_bytes()
            except Exception as e:  # noqa: BLE001
                return self._json({"ok": False, "error": f"index.html: {e}"}, 500)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/api/status"):
            return self._json({"ok": True, **snapshot()})
        if self.path.startswith("/api/log"):
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(self.path).query)
            try:
                n = int((q.get("lines") or ["160"])[0])
            except ValueError:
                n = 160
            return self._json({"ok": True, "lines": tail_log(n)})
        if self.path.startswith("/api/account"):
            return self._json({"ok": True, **account()})
        if self.path.startswith("/api/config"):
            cfg = read_json(CONFIG, {})
            return self._json({"ok": True, "values": {k: get_path(cfg, k) for k in FIELDS}})
        return self._json({"ok": False, "error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        if not self._authorized():
            return self._need_auth()
        if not self._same_origin():
            return self._json({"ok": False, "error": "missing X-Requested-With"}, 403)
        try:
            data = self._body()
        except Exception:  # noqa: BLE001
            return self._json({"ok": False, "error": "invalid json"}, 400)

        if self.path.startswith("/api/control"):
            action = str(data.get("action") or "")
            if action not in ("start", "stop", "restart"):
                return self._json({"ok": False, "error": "bad action"}, 400)
            cfg = read_json(CONFIG, {})
            if action in ("start", "restart") and str(cfg.get("mode")) == "live" \
                    and data.get("confirm") != "LIVE":
                return self._json({"ok": False, "error": "实盘操作需要确认"}, 400)
            try:
                r = subprocess.run([INIT, action], capture_output=True, text=True, timeout=20)
            except Exception as e:  # noqa: BLE001
                return self._json({"ok": False, "error": str(e)[:160]}, 500)
            time.sleep(1.2)
            return self._json({"ok": r.returncode == 0, "output": (r.stdout or r.stderr or "")[-300:],
                               "service": service_status()})

        if self.path.startswith("/api/config"):
            values = data.get("values") or {}
            if not isinstance(values, dict):
                return self._json({"ok": False, "error": "values 必须是对象"}, 400)
            cfg = read_json(CONFIG, {})
            old_mode = str(cfg.get("mode") or "shadow")
            for key, val in values.items():
                if key not in FIELDS:
                    return self._json({"ok": False, "error": f"不允许修改 {key}"}, 400)
                typ, lo, hi = FIELDS[key]
                try:
                    val = typ(val)
                except Exception:  # noqa: BLE001
                    return self._json({"ok": False, "error": f"{key} 类型错误"}, 400)
                if key in ENUMS and val not in ENUMS[key]:
                    return self._json({"ok": False, "error": f"{key} 取值不合法"}, 400)
                if lo is not None and (val < lo or val > hi):
                    return self._json({"ok": False, "error": f"{key} 超出范围 {lo}~{hi}"}, 400)
                set_path(cfg, key, val)
            if str(cfg.get("mode")) == "live" and data.get("confirm") != "LIVE":
                return self._json({"ok": False, "error": "切到实盘需要输入 LIVE 确认"}, 400)
            write_json(CONFIG, cfg)
            restarted = False
            if data.get("restart"):
                try:
                    subprocess.run([INIT, "restart"], capture_output=True, text=True, timeout=20)
                    restarted = True
                except Exception as e:  # noqa: BLE001
                    return self._json({"ok": False, "error": f"保存成功但重启失败: {e}"}, 500)
            return self._json({"ok": True, "mode": cfg.get("mode"), "old_mode": old_mode,
                               "restarted": restarted})

        return self._json({"ok": False, "error": "not found"}, 404)


def main() -> None:
    if not PASSWORD:
        print("WEB_PASSWORD 未设置，拒绝启动", file=sys.stderr)
        raise SystemExit(2)
    if not INDEX.exists():
        print(f"缺少 {INDEX}", file=sys.stderr)
        raise SystemExit(2)
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"web ui on 0.0.0.0:{PORT} root={ROOT}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
