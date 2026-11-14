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
import re
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


def upsert_env(pairs: dict) -> None:
    path = ROOT / ".env"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    out, seen = [], set()
    for line in lines:
        st = line.strip()
        if st and not st.startswith("#") and "=" in st:
            k = st.split("=", 1)[0].strip()
            if k in pairs:
                out.append(f"{k}={pairs[k]}")
                seen.add(k)
                continue
        out.append(line)
    for k, v in pairs.items():
        if k not in seen:
            out.append(f"{k}={v}")
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    tmp.replace(path)
    try:
        path.chmod(0o600)
    except Exception:
        pass
    for k, v in pairs.items():
        os.environ[k] = v


def credential_status() -> dict:
    key = os.environ.get("EXEC_API_KEY", "").strip()
    secret = os.environ.get("EXEC_API_SECRET", "").strip()
    cookie = os.environ.get("BINANCE_COOKIE", "").strip()
    csrf = os.environ.get("BINANCE_CSRFTOKEN", "").strip()
    return {
        "ok": True,
        "api_key": ("*" * max(0, len(key) - 4) + key[-4:]) if key else "",
        "has_api_key": bool(key and secret),
        "has_cookie": bool(cookie and csrf),
        "session_file": (ROOT / "session.json").exists(),
        "ua": bool(os.environ.get("BINANCE_UA", "").strip()),
    }


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


def _norm(v):
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.strip())
        except ValueError:
            return v.strip().lower()
    return v


def _flatten(d: dict, prefix: str = "") -> dict:
    out = {}
    for k, v in (d or {}).items():
        p = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flatten(v, p + "."))
        else:
            out[p] = v
    return out


def preset_info(cfg: dict) -> dict:
    try:
        import strategy_config as SC
        import strategy_registry as REG
        sid = str(cfg.get("strategy") or "")
        spec = REG.get(sid)
        name = spec.name if spec else sid
        best = None
        for pid, preset in SC.PRESETS.items():
            if str(preset.get("strategy")) != sid:
                continue
            ok = True
            for path, val in _flatten(preset).items():
                if path == "mode":
                    continue
                if _norm(get_path(cfg, path)) != _norm(val):
                    ok = False
                    break
            if ok:
                best = pid
                break
        label = SC.PRESET_LABELS.get(best, "") if best else ""
        return {"strategy_id": sid, "strategy_name": name,
                "preset_id": best or "", "preset_label": label or "自定义参数"}
    except Exception:
        return {"strategy_id": "", "strategy_name": "", "preset_id": "", "preset_label": ""}


def rss_mb():
    try:
        r = subprocess.run(["pgrep", "-f", "bot/strategy_run.py"],
                           capture_output=True, text=True, timeout=2)
        pids = [x.strip() for x in (r.stdout or "").splitlines() if x.strip()]
        if not pids:
            return None
        text = Path(f"/proc/{pids[0]}/status").read_text(encoding="utf-8", errors="replace")
        m = re.search(r"^VmRSS:\s+(\d+)\s+kB", text, re.M)
        return round(int(m.group(1)) / 1024, 1) if m else None
    except Exception:
        return None


def filter_snapshot() -> dict:
    try:
        import symbol_filter as SF
        data = SF._clean(SF.load())
        items = [SF.describe(sym, v) for sym, v in (data.get("blacklist") or {}).items()]
        items.sort(key=lambda x: (not x["auto"], x.get("until_ts") or 0, x["symbol"]))
        return {"ok": True, "items": items}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:160], "items": []}


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


def tail_log(n: int = 160, events_only: bool = False) -> list[str]:
    try:
        if not LOG.exists():
            return []
        raw = LOG.read_bytes()
        if len(raw) > 512_000:
            raw = raw[-512_000:]
        lines = raw.decode("utf-8", "replace").splitlines()
        if events_only:
            skip = ("统计", "状态", "候选池更新", "WS 已连接", "REST 兜底",
                    "配置来源", "exchangeInfo", "自研策略 v", "策略：", "均价模式", "追踪止盈")
            pat = re.compile(r"(信号\s{2,}|SKIP|OPEN\??|CLOSE\??|ADD\??|⛔|异常|失败|错误|Traceback|NameError|KeyError|-20\d\d|-41\d\d)")
            lines = [x for x in lines if not any(s in x for s in skip) and pat.search(x)]
        return lines[-max(10, min(n, 500)):]
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
        "meta": preset_info(cfg),
        "rss_mb": rss_mb(),
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
            events = (q.get("filter") or [""])[0] == "events"
            return self._json({"ok": True, "lines": tail_log(n, events)})
        if self.path.startswith("/api/account"):
            return self._json({"ok": True, **account()})
        if self.path.startswith("/api/credentials"):
            return self._json(credential_status())
        if self.path.startswith("/api/filter"):
            return self._json(filter_snapshot())
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

        if self.path.startswith("/api/credentials/apikey"):
            key = str(data.get("key") or "").strip()
            secret = str(data.get("secret") or "").strip()
            if len(key) < 10 or len(secret) < 10:
                return self._json({"ok": False, "error": "API Key / Secret 不能为空且长度不足"}, 400)
            cfg = read_json(CONFIG, {})
            restart = bool(data.get("restart"))
            if restart and str(cfg.get("mode")) == "live" and data.get("confirm") != "LIVE":
                return self._json({"ok": False, "error": "实盘重启需要输入 LIVE 确认"}, 400)
            test = {}
            try:
                from exec_client import ExecClient
                test = ExecClient(key, secret).test()
            except Exception as e:  # noqa: BLE001
                return self._json({"ok": False, "error": f"新 Key 测试失败：{str(e)[:160]}"}, 400)
            upsert_env({"EXEC_API_KEY": key, "EXEC_API_SECRET": secret})
            restarted = False
            if restart:
                try:
                    subprocess.run([INIT, "restart"], capture_output=True, text=True, timeout=20)
                    restarted = True
                except Exception as e:  # noqa: BLE001
                    return self._json({"ok": False, "error": f"已保存但重启失败: {e}"}, 500)
            return self._json({"ok": True, "restarted": restarted, "test": test,
                               "api_key": credential_status()["api_key"]})

        if self.path.startswith("/api/credentials/session"):
            cookie = str(data.get("cookie") or "").strip()
            csrf = str(data.get("csrf") or "").strip()
            ua = str(data.get("ua") or "").strip()
            if not cookie or not csrf:
                return self._json({"ok": False, "error": "cookie 和 csrftoken 不能为空"}, 400)
            upsert_env({"BINANCE_COOKIE": cookie, "BINANCE_CSRFTOKEN": csrf})
            if ua:
                upsert_env({"BINANCE_UA": ua})
            sess_path = ROOT / "session.json"
            sess = read_json(sess_path, {})
            if not isinstance(sess, dict):
                sess = {}
            sess.update({"cookie": cookie, "csrftoken": csrf})
            if ua:
                sess["user-agent"] = ua
            write_json(sess_path, sess)
            try:
                sess_path.chmod(0o600)
            except Exception:
                pass
            return self._json({"ok": True, "has_cookie": True, "session_file": True})

        if self.path.startswith("/api/filter/block"):
            raw = data.get("symbols") or data.get("symbol") or ""
            if isinstance(raw, str):
                symbols = [x.strip().upper() for x in raw.replace("，", ",").split(",")]
            elif isinstance(raw, list):
                symbols = [str(x).strip().upper() for x in raw]
            else:
                symbols = []
            symbols = [x for x in symbols if x]
            if not symbols:
                return self._json({"ok": False, "error": "请输入币种，例如 BTCUSDT, ETHUSDT"}, 400)
            if len(symbols) > 20:
                return self._json({"ok": False, "error": "一次最多 20 个"}, 400)
            for s in symbols:
                if not re.fullmatch(r"[A-Z0-9]{2,30}USDT", s):
                    return self._json({"ok": False, "error": f"币种不合法: {s}"}, 400)
            try:
                import symbol_filter as SF
                for s in symbols:
                    SF.block(s, "手动", 0)   # 永久
            except Exception as e:  # noqa: BLE001
                return self._json({"ok": False, "error": str(e)[:160]}, 500)
            return self._json({"ok": True, "blocked": symbols})

        if self.path.startswith("/api/filter/unblock"):
            sym = str(data.get("symbol") or "").strip().upper()
            if not re.fullmatch(r"[A-Z0-9]{2,30}USDT", sym):
                return self._json({"ok": False, "error": "币种不合法"}, 400)
            try:
                import symbol_filter as SF
                removed = SF.unblock(sym)
            except Exception as e:  # noqa: BLE001
                return self._json({"ok": False, "error": str(e)[:160]}, 500)
            return self._json({"ok": True, "removed": removed, "symbol": sym})

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
