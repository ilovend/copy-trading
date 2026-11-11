#!/usr/bin/env python3
"""影子执行引擎：真实计算「本应下的单」，但不发单。

输入：listen_copy 的成交事件（signal hook）
输出：
    · bot/data/intents-YYYYMMDD.jsonl  每笔意图的完整决策链
    · 控制台/日志行（GUI 实时日志里能看到）

计算内容：
    1. 按比例模型算目标数量（净值比 / 固定倍数 / 固定保证金）
    2. 按交易所 stepSize、minNotional 取整
    3. 读盘口算「这么多量到底能成交在什么价」→ 滑点 bps
    4. 风控检查（单笔、单币、总敞口、信号年龄）

配置：gui/copy_config.json（缺省时用内置默认值）
"""
from __future__ import annotations

import json
import queue
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "bot" / "data"
DATA.mkdir(parents=True, exist_ok=True)
CONFIG_PATH = ROOT / "gui" / "copy_config.json"
FAPI = "https://fapi.binance.com"

DEFAULTS = {
    "mode": "shadow",              # shadow | live（live 尚未实现）
    "ratio_mode": "net_equity",    # net_equity | fixed_multiple | fixed_margin
    "k": 1.0,
    "exec_equity": 50.0,           # 执行账户权益（手填，接入 API 后自动读）
    "margin_per_trade": 5.0,       # 固定保证金模式：每笔保证金
    "leverage": 20,
    "max_order_notional": 10.0,
    "max_symbol_pct": 20.0,
    "max_total_notional": 50.0,
    "max_slippage_pct": 0.5,
    "max_signal_age_s": 3.0,
    "open_order_type": "ioc_limit",
    "close_order_type": "ioc_limit",
    "protective_stop_pct": 2.0,
    "push_intents": False,
}


def log(msg: str) -> None:
    print(f"{datetime.now():%H:%M:%S} {msg}", flush=True)


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception as e:  # noqa: BLE001
            log(f"config load failed: {e}")
    return cfg


def http_json(url: str, timeout: int = 8):
    req = urllib.request.Request(url, headers={"User-Agent": "copy-engine/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


class CopyEngine:
    def __init__(self) -> None:
        self.cfg = load_config()
        self.q: queue.Queue = queue.Queue()
        self._filters: dict[str, dict] = {}
        self._equity_cache: dict[str, tuple[float, float]] = {}
        self._exposure: dict[str, float] = {}
        self._total_exposure = 0.0
        self._day = datetime.now().date()
        self._stats = {"signals": 0, "intents": 0, "rejected": 0}
        threading.Thread(target=self._worker, daemon=True).start()

    # ------------------------------------------------------------- signal
    def on_signal(self, sig: dict) -> None:
        self.q.put(sig)

    def _worker(self) -> None:
        while True:
            sig = self.q.get()
            try:
                self._process(sig)
            except Exception as e:  # noqa: BLE001
                log(f"engine error: {e}")

    # ------------------------------------------------------------- helpers
    def _symbol_filter(self, symbol: str) -> dict:
        if symbol in self._filters:
            return self._filters[symbol]
        info = {"step": 0.001, "min_notional": 5.0, "tick": 0.0001}
        try:
            data = http_json(f"{FAPI}/fapi/v1/exchangeInfo")
            for s in data.get("symbols", []):
                if s.get("symbol") != symbol:
                    continue
                for f in s.get("filters", []):
                    if f.get("filterType") == "LOT_SIZE":
                        info["step"] = float(f["stepSize"])
                    elif f.get("filterType") == "MIN_NOTIONAL":
                        info["min_notional"] = float(f.get("notional", 5))
                    elif f.get("filterType") == "PRICE_FILTER":
                        info["tick"] = float(f["tickSize"])
                break
        except Exception as e:  # noqa: BLE001
            log(f"exchangeInfo failed: {e}")
        self._filters[symbol] = info
        return info

    def _mirror_equity(self, portfolio: str) -> float:
        """镜像组合的保证金余额，用于净值比。缓存 60 秒。"""
        now = time.time()
        hit = self._equity_cache.get(portfolio)
        if hit and now - hit[0] < 60:
            return hit[1]
        eq = 0.0
        try:
            from listen_copy import api  # 复用带 cookie 的会话
            j = api("/bapi/futures/v1/private/future/copy-trade/copy-portfolio/detail-list", "GET")
            for row in j.get("data") or []:
                if row.get("copyPortfolioId") == portfolio:
                    eq = float(row.get("marginBalance") or 0)
                    break
        except Exception as e:  # noqa: BLE001
            log(f"mirror equity failed: {e}")
        self._equity_cache[portfolio] = (now, eq)
        return eq

    @staticmethod
    def _floor_step(qty: float, step: float) -> float:
        if step <= 0:
            return qty
        n = int(qty / step + 1e-9)
        return round(n * step, 10)

    def _depth_vwap(self, symbol: str, side: str, qty: float) -> tuple[float, float]:
        """返回 (可成交均价, 可成交量)。buy 吃 asks，sell 吃 bids。"""
        try:
            d = http_json(f"{FAPI}/fapi/v1/depth?symbol={symbol}&limit=50")
        except Exception as e:  # noqa: BLE001
            log(f"depth failed {symbol}: {e}")
            return 0.0, 0.0
        levels = d.get("asks" if side == "BUY" else "bids") or []
        remain, cost, filled = qty, 0.0, 0.0
        for price_s, qty_s in levels:
            price, avail = float(price_s), float(qty_s)
            take = min(remain, avail)
            cost += take * price
            filled += take
            remain -= take
            if remain <= 1e-12:
                break
        if filled <= 0:
            return 0.0, 0.0
        return cost / filled, filled

    # ------------------------------------------------------------- process
    def _process(self, sig: dict) -> None:
        self._reset_daily()
        self.cfg = load_config()  # 每次读配置，GUI 改完即时生效
        self._stats["signals"] += 1

        targets = self.cfg.get("targets") or {}
        if targets:
            t = targets.get(sig.get("portfolio"))
            if not t or not t.get("enabled"):
                self._reject(sig, "not_target", "该组合未开启复制")
                return

        symbol = sig["symbol"]
        side = sig["side"]
        price = float(sig["price"] or 0)
        qty_mirror = float(sig["qty"] or 0)
        rp = float(sig.get("realized_pnl") or 0)
        kind = "open" if rp == 0 else "close"
        if sig.get("position_side") == "BOTH":
            pass

        age_ms = int(time.time() * 1000) - int(sig.get("trade_time") or 0)
        if age_ms > self.cfg["max_signal_age_s"] * 1000:
            self._reject(sig, "stale", f"信号已 {age_ms}ms")
            return

        # 1) 目标数量
        flt = self._symbol_filter(symbol)
        mode = self.cfg["ratio_mode"]
        if mode == "net_equity":
            eq_c = self._mirror_equity(sig["portfolio"])
            ratio = (self.cfg["exec_equity"] / eq_c) if eq_c > 0 else 0.0
            qty_target = qty_mirror * ratio * self.cfg["k"]
        elif mode == "fixed_multiple":
            ratio = self.cfg["k"]
            qty_target = qty_mirror * self.cfg["k"]
        else:  # fixed_margin
            ratio = 0.0
            notional = self.cfg["margin_per_trade"] * self.cfg["leverage"]
            qty_target = (notional / price) if price > 0 else 0.0

        # 2) 单笔上限 + 取整
        if price > 0:
            cap = self.cfg["max_order_notional"] / price
            qty_target = min(qty_target, cap)
        qty_target = self._floor_step(qty_target, flt["step"])
        notional = qty_target * price

        if qty_target <= 0 or notional < flt["min_notional"]:
            self._reject(sig, "too_small",
                         f"目标 {qty_target} 名义 {notional:.2f} < minNotional {flt['min_notional']}")
            return

        # 3) 盘口滑点
        book_side = "BUY" if side == "BUY" else "SELL"
        exec_price, filled = self._depth_vwap(symbol, book_side, qty_target)
        depth_ok = filled + 1e-12 >= qty_target
        slip_bps = 0.0
        if exec_price > 0 and price > 0:
            slip_bps = (exec_price - price) / price * 10000 * (1 if side == "BUY" else -1)

        # 4) 风控
        if abs(slip_bps) > self.cfg["max_slippage_pct"] * 100:
            self._reject(sig, "slippage",
                         f"滑点 {slip_bps:.1f}bps > {self.cfg['max_slippage_pct'] * 100:.0f}bps")
            return
        sym_after = self._exposure.get(symbol, 0.0) + (notional if kind == "open" else -notional)
        eq_x = self.cfg["exec_equity"]
        if eq_x > 0 and sym_after / eq_x * 100 > self.cfg["max_symbol_pct"]:
            self._reject(sig, "symbol_cap", f"{symbol} 占比 {sym_after / eq_x * 100:.1f}%")
            return
        total_after = self._total_exposure + (notional if kind == "open" else -notional)
        if total_after > self.cfg["max_total_notional"]:
            self._reject(sig, "total_cap", f"总敞口 {total_after:.1f}U")
            return

        # 5) 记录（影子：不真正下单）
        self._exposure[symbol] = sym_after
        self._total_exposure = max(0.0, total_after)
        self._stats["intents"] += 1
        rec = {
            "ts": int(time.time() * 1000),
            "kind": kind,
            "nickname": sig.get("nickname"),
            "portfolio": sig.get("portfolio"),
            "symbol": symbol,
            "side": side,
            "ps": sig.get("position_side"),
            "qty_mirror": qty_mirror,
            "price_signal": price,
            "ratio_mode": mode,
            "ratio": round(ratio, 4),
            "qty_target": qty_target,
            "notional": round(notional, 4),
            "exec_price_est": round(exec_price, 8),
            "slippage_bps": round(slip_bps, 1),
            "depth_ok": depth_ok,
            "age_ms": age_ms,
            "mode": "shadow",
        }
        with (DATA / f"intents-{datetime.now():%Y%m%d}.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

        log(f"SHADOW {kind.upper():5s} {symbol:10s} {side:4s} "
            f"mirror={qty_mirror:<10g} target={qty_target:<10g} "
            f"px={price:<10g} est={exec_price:<10g} slip={slip_bps:+.1f}bps "
            f"notional={notional:.2f}U")

    def _reject(self, sig: dict, reason: str, detail: str) -> None:
        self._stats["rejected"] += 1
        rec = {
            "ts": int(time.time() * 1000),
            "kind": "reject",
            "reason": reason,
            "detail": detail,
            "symbol": sig.get("symbol"),
            "nickname": sig.get("nickname"),
            "qty_mirror": sig.get("qty"),
            "price_signal": sig.get("price"),
        }
        with (DATA / f"intents-{datetime.now():%Y%m%d}.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        log(f"SKIP   {sig.get('symbol'):10s} {reason:10s} {detail}")

    def _reset_daily(self) -> None:
        today = datetime.now().date()
        if today != self._day:
            self._day = today
            self._exposure.clear()
            self._total_exposure = 0.0
