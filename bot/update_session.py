#!/usr/bin/env python3
"""Update probe/session.json from a copied cURL.

Usage:
    python bot/update_session.py --clipboard          # read from Windows clipboard
    python bot/update_session.py --file probe/curl.txt
    python bot/update_session.py                      # then paste, end with EOF (Ctrl+Z Enter)

Accepts both "Copy as cURL (bash)" and "Copy as cURL (cmd)" formats.
Validates the session against the copy-trading API before writing.
"""
from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SESSION_PATH = ROOT / "probe" / "session.json"
BASE = "https://www.binance.com"

WANTED = {
    "cookie": "cookie",
    "csrftoken": "csrftoken",
    "bnc-uuid": "bnc-uuid",
    "fvideo-id": "fvideo-id",
    "fvideo-token": "fvideo-token",
    "x-passthrough-token": "x-passthrough-token",
    "device-info": "device-info",
    "user-agent": "user-agent",
}


def read_input(args) -> str:
    if args.file:
        return Path(args.file).read_text(encoding="utf-8-sig", errors="replace")
    if args.clipboard:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"],
                             capture_output=True, text=True)
        return out.stdout
    print("粘贴 cURL，结束后按 Ctrl+Z 回车：", file=sys.stderr)
    return sys.stdin.read()


def normalize(text: str) -> str:
    """Strip cmd-style ^ escapes so shlex can do the real parsing."""
    return text.replace("^", "")


def parse_curl(text: str) -> dict:
    lex = shlex.shlex(normalize(text), posix=True)
    lex.whitespace_split = True
    lex.commenters = ""
    tokens = list(lex)

    headers: dict[str, str] = {}
    cookie = None
    body = None
    i = 0
    while i < len(tokens):
        t = tokens[i]
        nxt = tokens[i + 1] if i + 1 < len(tokens) else None
        if t in ("-H", "--header") and nxt is not None:
            if ":" in nxt:
                k, v = nxt.split(":", 1)
                k, v = k.strip().lower(), v.strip()
                if k and v:
                    headers[k] = v
            i += 2
            continue
        if t in ("-b", "--cookie") and nxt is not None:
            cookie = nxt.strip()
            i += 2
            continue
        if t in ("--data-raw", "--data", "--data-binary", "-d") and nxt is not None:
            body = nxt.strip()
            i += 2
            continue
        i += 1

    portfolio_id = None
    if body:
        mm = re.search(r'"portfolioId"\s*:\s*"(\d+)"', body) or re.search(r"portfolioId\D+(\d{10,})", body)
        if mm:
            portfolio_id = mm.group(1)

    sess = {key: headers[src] for key, src in WANTED.items() if src in headers}
    if cookie:
        sess["cookie"] = cookie
    if portfolio_id:
        sess["portfolioId"] = portfolio_id
    return sess


def token_expiry(cookie: str) -> str | None:
    import base64
    m = re.search(r"bnc-challenge-token=([^;]+)", cookie or "")
    if not m:
        return None
    parts = m.group(1).split(".")
    if len(parts) < 2:
        return None
    p = parts[1].replace("-", "+").replace("_", "/")
    p += "=" * (-len(p) % 4)
    try:
        payload = json.loads(base64.b64decode(p))
        exp = datetime.fromtimestamp(payload["exp"])
        return exp.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:  # noqa: BLE001
        return None


def validate(sess: dict) -> tuple[bool, str, str | None]:
    """Validate the session with an endpoint that needs no portfolioId.

    Returns (ok, message, first_portfolio_id).
    """
    if not sess.get("cookie"):
        return False, "没有解析到 cookie", None
    headers = {
        "accept": "*/*",
        "clienttype": "web",
        "content-type": "application/json",
        "lang": "zh-CN",
        "origin": BASE,
        "referer": f"{BASE}/zh-CN/copy-trading/copy-management",
        "user-agent": sess.get("user-agent", ""),
        "cookie": sess["cookie"],
    }
    for k in ("csrftoken", "bnc-uuid", "fvideo-id", "fvideo-token",
              "x-passthrough-token", "device-info"):
        if sess.get(k):
            headers[k] = sess[k]
    req = urllib.request.Request(
        BASE + "/bapi/futures/v1/private/future/copy-trade/copy-portfolio/detail-list",
        headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            j = json.loads(r.read().decode())
        if j.get("success"):
            rows = j.get("data") or []
            pid = rows[0].get("copyPortfolioId") if rows else None
            return True, f"有效，{len(rows)} 个跟单组合", pid
        return False, f"接口返回 {j.get('code')} {j.get('message')}", None
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {e.read().decode()[:160]}", None
    except Exception as e:  # noqa: BLE001
        return False, repr(e), None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file")
    ap.add_argument("--clipboard", action="store_true")
    args = ap.parse_args()

    raw = read_input(args)
    if not raw.strip():
        sys.exit("输入为空")

    sess = parse_curl(raw)
    print("解析结果：")
    for k in ("cookie", "csrftoken", "bnc-uuid", "fvideo-id", "fvideo-token",
              "x-passthrough-token", "device-info", "user-agent", "portfolioId"):
        v = sess.get(k)
        if k == "cookie" and v:
            print(f"  {k:20s} len={len(v)}  logined={'logined=y' in v}")
        elif v:
            print(f"  {k:20s} {'*' * min(len(v), 8)} (len={len(v)})")

    exp = token_expiry(sess.get("cookie", ""))
    if exp:
        print(f"  challenge-token 过期时间 {exp}")

    if SESSION_PATH.exists():
        old = json.loads(SESSION_PATH.read_text(encoding="utf-8-sig"))
        if not sess.get("portfolioId"):
            sess["portfolioId"] = old.get("portfolioId")
        if not sess.get("user-agent"):
            sess["user-agent"] = old.get("user-agent", "")

    ok, msg, pid = validate(sess)
    print(f"校验：{'✅' if ok else '❌'} {msg}")
    if not ok:
        sys.exit(1)

    if not sess.get("portfolioId") and pid:
        sess["portfolioId"] = pid

    SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
    SESSION_PATH.write_text(json.dumps(sess, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已写入 {SESSION_PATH}")
    print("下一步：重启监听器（关闭 start.bat 窗口再打开，或重启后台进程）。")


if __name__ == "__main__":
    main()
