#!/usr/bin/env python3
"""Binance copy-portfolio user-data streams -> Telegram push.

Auto-discovers every copy portfolio of the logged-in account, opens one
WebSocket per portfolio, and pushes every fill to Telegram.

  1. GET  /bapi/futures/v1/private/future/copy-trade/copy-portfolio/detail-list
  2. POST /bapi/futures/v1/private/future/user-data/user-listen-key   (per portfolio)
  3. wss://fstream.binance.com/ws/<listenKey>                        (one per portfolio)
  4. PUT  same endpoint every 25 min to keep each key alive
  5. reconnect with backoff, rebuild key when it expires

Run:  python bot/listen_copy.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
import queue
import threading
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "bot" / "data"
DATA.mkdir(parents=True, exist_ok=True)

BASE = "https://www.binance.com"
WS_BASE = "wss://fstream.binance.com"
WS_PATH = "/private/stream"


# ---------------------------------------------------------------- env / config
# 通知与环境已抽到 notify.py，这里只做转发，保持旧调用点不变
from notify import (TG_CHAT, TG_PROXY, TG_TOKEN, load_env,  # noqa: E402,F401
                    tg_send, tg_send_async)

FALLBACK_PID = os.environ.get("COPY_PORTFOLIO_ID", "")

# 登录态与 bapi 已抽到 bapi.py
import bapi  # noqa: E402

SESSION_PATH = bapi.SESSION_PATH


def __getattr__(name: str):
    """兼容旧代码：SESSION / HEADERS 始终取 bapi 的最新值。"""
    if name in ("SESSION", "HEADERS", "SESSION_MTIME", "DEFAULT_UA"):
        return getattr(bapi, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def api(path: str, method: str = "POST", body: dict | None = None) -> dict:
    return bapi.api(path, method, body)


def load_session() -> None:
    bapi.load_session()


def reload_session_if_changed() -> bool:
    return bapi.reload_session_if_changed()


# ------------------------------------------------------------------ utilities
def num(x, digits: int = 6) -> str:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return str(x)
    s = f"{v:,.{digits}f}".rstrip("0").rstrip(".")
    return s or "0"


def discover_portfolios() -> list[dict]:
    try:
        j = api("/bapi/futures/v1/private/future/copy-trade/copy-portfolio/detail-list", "GET")
        rows = j.get("data") or []
    except Exception as e:  # noqa: BLE001
        log(f"discover failed: {e}")
        rows = []
    out = []
    for x in rows:
        if x.get("status") not in (None, "ACTIVE"):
            continue
        out.append({
            "copyPortfolioId": x.get("copyPortfolioId"),
            "leadPortfolioId": x.get("leadPortfolioId"),
            "nickname": x.get("nickname") or "?",
            "netCopyAmount": x.get("netCopyAmount"),        # 净投入本金
            "marginBalance": x.get("marginBalance"),        # 保证金余额（开仓基数）
            "realizedPnl": x.get("realizedPnl"),
            "unrealizedPnl": x.get("unrealizedPnl"),
        })
    if not out and FALLBACK_PID:
        out = [{"copyPortfolioId": FALLBACK_PID, "leadPortfolioId": None,
                "nickname": "portfolio", "netCopyAmount": None,
                "marginBalance": None, "realizedPnl": None, "unrealizedPnl": None}]
    return out


def parse_tokens(raw: str) -> list[str]:
    return [t.strip() for t in (raw or "").replace("，", ",").split(",") if t.strip()]


def select_portfolios(all_pf: list[dict], cli_tokens: list[str]) -> tuple[list[dict], list[str]]:
    """Filter discovered portfolios.

    Match rule: a token matches if it equals copyPortfolioId / leadPortfolioId,
    or is a case-insensitive substring of the nickname.
    Tokens come from env (WATCH_PORTFOLIOS / WATCH_LEADS / WATCH_NICKNAMES) plus --only.
    """
    tokens = (parse_tokens(os.environ.get("WATCH_PORTFOLIOS"))
              + parse_tokens(os.environ.get("WATCH_LEADS"))
              + parse_tokens(os.environ.get("WATCH_NICKNAMES"))
              + list(cli_tokens))
    tokens = list(dict.fromkeys(tokens))
    if not tokens:
        return all_pf, []

    def hit(pf: dict, tok: str) -> bool:
        t = tok.lower()
        return (pf["copyPortfolioId"] == tok
                or (pf.get("leadPortfolioId") or "") == tok
                or t in (pf["nickname"] or "").lower())

    picked = [pf for pf in all_pf if any(hit(pf, t) for t in tokens)]
    unmatched = [t for t in tokens
                 if not any(hit(pf, t) for pf in all_pf)]
    return picked, unmatched


def new_listen_key(pid: str) -> str:
    j = api("/bapi/futures/v1/private/future/user-data/user-listen-key",
            "POST", {"copyTradeType": "COPY", "portfolioId": pid})
    if not j.get("success"):
        raise RuntimeError(f"listenKey failed: {j}")
    return j["data"]


def keepalive_listen_key(pid: str) -> None:
    api("/bapi/futures/v1/private/future/user-data/user-listen-key",
        "PUT", {"copyTradeType": "COPY", "portfolioId": pid})


def fmt_fill(o: dict, nickname: str) -> str:
    rp = float(o.get("rp") or 0)
    qty = float(o.get("l") or 0)
    price = float(o.get("ap") or o.get("L") or 0)
    ps = o.get("ps")
    is_long = ps == "LONG" or (ps == "BOTH" and o.get("S") == "BUY")
    side = "多" if is_long else "空"
    if rp == 0:
        return (f"🟢 开仓 {nickname} · {o.get('s')} {side}\n"
                f"价格 {num(price)}  数量 {num(qty)}\n"
                f"名义 {num(qty * price, 2)} U")
    sign = "+" if rp >= 0 else ""
    return (f"🔴 平仓 {nickname} · {o.get('s')} {side}\n"
            f"价格 {num(price)}  数量 {num(qty)}\n"
            f"盈亏 {sign}{num(rp, 4)} U")


# --------------------------------------------------------------------- stream
seen: set[str] = set()
stats = {"events": 0, "pushed": 0}
CLOCK_OFFSET_MS = 0

# 影子/实盘执行器可以挂在这里，每笔成交都会回调一次
SIGNAL_HOOK = None


def set_signal_hook(fn) -> None:
    global SIGNAL_HOOK
    SIGNAL_HOOK = fn


async def clock_loop() -> None:
    while True:
        await asyncio.sleep(600)
        await asyncio.to_thread(calibrate_clock)


def calibrate_clock() -> None:
    """Measure local-clock offset against Binance server time.

    Without this, a wrong system clock makes the reported latency look huge
    (e.g. +500 ms) even though the network is fine.
    """
    global CLOCK_OFFSET_MS
    try:
        t0 = time.time() * 1000
        req = urllib.request.Request("https://fapi.binance.com/fapi/v1/time",
                                     headers={"User-Agent": "copy-trading/0.1"})
        with urllib.request.urlopen(req, timeout=8) as r:
            server = json.loads(r.read().decode())["serverTime"]
        t1 = time.time() * 1000
        CLOCK_OFFSET_MS = server - (t0 + t1) / 2
        from version import __version__
        log(f"v{__version__} clock offset vs binance: {CLOCK_OFFSET_MS:+.0f} ms")
    except Exception as e:  # noqa: BLE001
        log(f"clock calibration failed: {e}")


def handle(msg: dict, pid: str, nickname: str, lead: str | None) -> None:
    with (DATA / f"raw-{datetime.now():%Y%m%d}.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"recv": int(time.time() * 1000), "portfolio": pid,
                            "msg": msg}, ensure_ascii=False) + "\n")

    # 新版 /private/stream 会把事件包成 {"stream": "<listenKey>", "data": {...}}
    if isinstance(msg, dict) and isinstance(msg.get("data"), dict):
        msg = msg["data"]

    etype = msg.get("e")
    if etype == "listenKeyExpired":
        raise RuntimeError("listenKey expired")
    if etype != "ORDER_TRADE_UPDATE":
        return
    o = msg.get("o") or {}
    if o.get("x") != "TRADE":
        return
    if float(o.get("l") or 0) <= 0:
        return
    key = f"{pid}:{o.get('i')}:{o.get('T')}:{o.get('l')}:{o.get('z')}"
    if key in seen:
        return
    seen.add(key)
    stats["events"] += 1

    latency = int(time.time() * 1000 + CLOCK_OFFSET_MS) - int(o.get("T") or 0)
    text = fmt_fill(o, nickname) + f"\n延迟 {latency} ms  {datetime.now():%H:%M:%S}"

    with (DATA / f"events-{datetime.now():%Y%m%d}.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"recv": int(time.time() * 1000), "portfolio": pid,
                            "nickname": nickname, "lead": lead,
                            "latency_ms": latency, "o": o}, ensure_ascii=False) + "\n")

    log(f"FILL [{nickname}] {o.get('s')} {o.get('S')} qty={o.get('l')} px={o.get('ap')} lag={latency}ms")

    if SIGNAL_HOOK is not None:
        try:
            SIGNAL_HOOK({
                "portfolio": pid,
                "nickname": nickname,
                "lead": lead,
                "symbol": o.get("s"),
                "side": o.get("S"),
                "position_side": o.get("ps"),
                "qty": float(o.get("l") or 0),
                "price": float(o.get("ap") or o.get("L") or 0),
                "realized_pnl": float(o.get("rp") or 0),
                "order_id": o.get("i"),
                "trade_time": int(o.get("T") or 0),
                "recv": int(time.time() * 1000),
                "recv_synced": int(time.time() * 1000 + CLOCK_OFFSET_MS),
            })
        except Exception as e:  # noqa: BLE001
            log(f"signal hook error: {e}")

    tg_send_async(text)
    stats["pushed"] += 1


async def keepalive_loop(pid: str, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=25 * 60)
            return
        except asyncio.TimeoutError:
            pass
        try:
            await asyncio.to_thread(reload_session_if_changed)
            await asyncio.to_thread(keepalive_listen_key, pid)
            log(f"keepalive ok [{pid[-6:]}]")
        except Exception as e:  # noqa: BLE001
            log(f"keepalive failed [{pid[-6:]}]: {e}")
            return


async def stream_portfolio(pf: dict) -> None:
    pid = pf["copyPortfolioId"]
    nickname = pf["nickname"]
    lead = pf.get("leadPortfolioId")
    tag = f"{nickname}({pid[-6:]})"
    backoff = 1
    fails = 0
    while True:
        try:
            await asyncio.to_thread(reload_session_if_changed)
            key = await asyncio.to_thread(new_listen_key, pid)
            stop = asyncio.Event()
            ka = asyncio.create_task(keepalive_loop(pid, stop))
            async with websockets.connect(f"{WS_BASE}{WS_PATH}", ping_interval=30,
                                          ping_timeout=30, max_queue=1024) as ws:
                # 新版路径必须显式订阅 listenKey 才会推送
                await ws.send(json.dumps({"method": "SUBSCRIBE", "params": [key], "id": 1}))
                backoff = 1
                fails = 0
                log(f"connected [{tag}] ws={WS_PATH} key={key[:8]}")
                async for raw in ws:
                    try:
                        handle(json.loads(raw), pid, nickname, lead)
                    except RuntimeError:
                        raise
                    except Exception as e:  # noqa: BLE001
                        log(f"handle error [{tag}]: {e}")
            stop.set()
            ka.cancel()
        except Exception as e:  # noqa: BLE001
            fails += 1
            log(f"stream error [{tag}]: {e}  retry in {backoff}s")
            if fails == 3:
                await asyncio.to_thread(
                    tg_send,
                    f"⚠️ 监听中断 {tag}\n{type(e).__name__}: {str(e)[:120]}\n"
                    "多半是网页会话(cookie)过期，需要重新抓取。")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)


async def run(cli_tokens: list[str] | None = None, dry_run: bool = False) -> None:
    load_session()
    await asyncio.to_thread(calibrate_clock)
    asyncio.create_task(clock_loop())
    all_pf = await asyncio.to_thread(discover_portfolios)
    if not all_pf:
        sys.exit("no copy portfolio found")
    log(f"discovered {len(all_pf)} portfolios:")
    for pf in all_pf:
        log(f"  {pf['nickname']:20s} copy={pf['copyPortfolioId']} lead={pf['leadPortfolioId']} "
            f"net={pf['netCopyAmount']} margin={pf['marginBalance']}")

    picked, unmatched = select_portfolios(all_pf, cli_tokens or [])
    if unmatched:
        log(f"WARN 未匹配到: {unmatched}")
        log(f"     可选: {[p['nickname'] for p in all_pf]}")
    if not picked:
        sys.exit("筛选结果为空，检查 WATCH_* / --only 的写法")

    log(f"watching {len(picked)}/{len(all_pf)}: " + ", ".join(p["nickname"] for p in picked))
    if dry_run:
        log("dry-run: 不建立连接，直接退出")
        return

    await asyncio.to_thread(
        tg_send,
        f"🚀 跟单监听已启动（实时 WS，{len(picked)} 个）\n" +
        "\n".join(f"· {p['nickname']}" for p in picked))
    await asyncio.gather(*(stream_portfolio(p) for p in picked))


def main() -> None:
    ap = argparse.ArgumentParser(description="Binance copy-trade fill -> Telegram")
    ap.add_argument("--only", default="",
                    help="只监听部分带单员：名字片段 / copyPortfolioId / leadPortfolioId，逗号分隔")
    ap.add_argument("--dry-run", action="store_true",
                    help="只列出筛选结果，不连接 WebSocket")
    args = ap.parse_args()
    try:
        asyncio.run(run(parse_tokens(args.only), args.dry_run))
    except KeyboardInterrupt:
        log("bye")


if __name__ == "__main__":
    main()
