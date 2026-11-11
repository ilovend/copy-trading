#!/usr/bin/env python3
"""影子执行引擎：真实计算「本应下的单」，但不发单。

修复要点（v0.2）：
    1. 滑点只拒「不利方向」，有利滑点放行
    2. 平仓前校验执行持仓，不会平出反向仓
    3. 平仓碎单 1.5 秒内聚合成一笔
    4. 启动预热 exchangeInfo，首单不再慢几秒
    5. 信号年龄做下限保护（避免时钟漂移导致负数）

输入：listen_copy 的成交事件（signal hook）
输出：bot/data/intents-YYYYMMDD.jsonl
配置：gui/copy_config.json
"""
from __future__ import annotations

import json
import queue
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "bot" / "data"
DATA.mkdir(parents=True, exist_ok=True)
CONFIG_PATH = ROOT / "gui" / "copy_config.json"
FAPI = "https://fapi.binance.com"

DEFAULTS = {
    "mode": "shadow",
    "ratio_mode": "net_equity",
    "k": 1.0,
    "exec_equity": 50.0,
    "margin_per_trade": 5.0,
    "leverage": 20,
    "max_order_notional": 10.0,
    "max_symbol_pct": 25.0,
    "max_total_notional": 40.0,
    "max_slippage_pct": 0.5,
    "max_signal_age_s": 3.0,
    "agg_window_ms": 1500,
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


def http_json(url: str, timeout: int = 10):
    req = urllib.request.Request(url, headers={"User-Agent": "copy-engine/0.2"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


class CopyEngine:
    def __init__(self) -> None:
        self.cfg = load_config()
        self.q: queue.Queue = queue.Queue()
        self._filters: dict[str, dict] = {}
        self._filters_ready = False
        self._equity_cache: dict[str, tuple[float, float]] = {}
        self._exposure: dict[str, float] = {}
        self._total_exposure = 0.0
        self._pos: dict[tuple[str, str], float] = {}
        self._pending: dict[tuple, dict] = {}
        self._day = datetime.now().date()
        self._stats = {"signals": 0, "intents": 0, "rejected": 0}
        threading.Thread(target=self._prewarm, daemon=True).start()
        threading.Thread(target=self._worker, daemon=True).start()

    # ------------------------------------------------------------- signal
    def on_signal(self, sig: dict) -> None:
        self.q.put(sig)

    def _worker(self) -> None:
        while True:
            try:
                sig = self.q.get(timeout=0.2)
                self._buffer(sig)
            except queue.Empty:
                pass
            self._flush_due()

    def _buffer(self, sig: dict) -> None:
        """开仓立即处理；平仓碎单在窗口内聚合。"""
        rp = float(sig.get("realized_pnl") or 0)
        if rp == 0:
            self._process(sig)
            return
        key = (sig.get("symbol"), sig.get("position_side"), sig.get("side"))
        now = time.time()
        item = self._pending.get(key)
        if item:
            item["sig"]["qty"] += float(sig.get("qty") or 0)
            item["sig"]["price"] = sig.get("price")
            item["sig"]["recv"] = sig.get("recv")
        else:
            self._pending[key] = {
                "sig": dict(sig),
                "deadline": now + self.cfg["agg_window_ms"] / 1000.0,
            }

    def _flush_due(self) -> None:
        now = time.time()
        for key, item in list(self._pending.items()):
            if now >= item["deadline"]:
                self._pending.pop(key, None)
                try:
                    self._process(item["sig"])
                except Exception as e:  # noqa: BLE001
                    log(f"engine error: {e}")

    # ------------------------------------------------------------- helpers
    def _prewarm(self) -> None:
        try:
            data = http_json(f"{FAPI}/fapi/v1/exchangeInfo", timeout=20)
            for s in data.get("symbols", []):
                info = {"step": 0.001, "min_notional": 5.0, "tick": 0.0001}
                for f in s.get("filters", []):
                    t = f.get("filterType")
                    if t == "LOT_SIZE":
                        info["step"] = float(f["stepSize"])
                    elif t == "MIN_NOTIONAL":
                        info["min_notional"] = float(f.get("notional", 5))
                    elif t == "PRICE_FILTER":
                        info["tick"] = float(f["tickSize"])
                self._filters[s["symbol"]] = info
            self._filters_ready = True
            log(f"exchangeInfo 预热完成（{len(self._filters)} 个交易对）")
        except Exception as e:  # noqa: BLE001
            log(f"exchangeInfo 预热失败（首单会慢）: {e}")

    def _symbol_filter(self, symbol: str) -> dict:
        if symbol in self._filters:
            return self._filters[symbol]
        return {"step": 0.001, "min_notional": 5.0, "tick": 0.0001}

    def _mirror_equity(self, portfolio: str) -> float:
        now = time.time()
        hit = self._equity_cache.get(portfolio)
        if hit and now - hit[0] < 60:
            return hit[1]
        eq = 0.0
        try:
            from listen_copy import api
            j = api("/bapi/futures/v1/private/future/copy-trade/copy-portfolio/detail-list", "GET")
            for row in j.get("data") or []:
                if row.get("copyPortfolioId") == portfolio:
                    eq = float(row.get("marginBalance") or 0)
                    break
        except Exception as e:  # noqa: BLE001
            log(f"mirror equity failed: {e}")
        self._equity_cache[portfolio] = (now, eq)
        return eq

    def _exec_position(self, symbol: str, ps: str | None) -> float:
        """执行账户当前持仓。

        影子模式读本地台账；实盘要改成查 /fapi/v2/positionRisk，
        在那之前宁可报错也不要拿假数据下单。
        """
        if self.cfg.get("mode") == "live":
            raise RuntimeError("live 模式尚未接持仓查询，禁止静默下单")
        return self._pos.get((symbol, ps or "BOTH"), 0.0)

    @staticmethod
    def _floor_step(qty: float, step: float) -> float:
        if step <= 0:
            return qty
        n = int(qty / step + 1e-9)
        return round(n * step, 10)

    @staticmethod
    def _ceil_step(qty: float, step: float) -> float:
        if step <= 0:
            return qty
        n = int(qty / step - 1e-9)
        if n * step < qty:
            n += 1
        return round(n * step, 10)

    def _book_estimate(self, symbol: str, side: str, qty: float) -> dict:
        """盘口估算：返回 VWAP、点差、是否可信。

        薄盘口（如新上币）深度数据噪声极大，VWAP 会和对手价差好几个百分点，
        这种结果不能用来判断滑点，标记 reliable=False。
        """
        out = {"exec_price": 0.0, "filled": 0.0, "spread_bps": 0.0,
               "dev_bps": 0.0, "reliable": False}
        try:
            d = http_json(f"{FAPI}/fapi/v1/depth?symbol={symbol}&limit=50")
        except Exception as e:  # noqa: BLE001
            log(f"depth failed {symbol}: {e}")
            return out
        bids = [(float(p), float(q)) for p, q in (d.get("bids") or [])]
        asks = [(float(p), float(q)) for p, q in (d.get("asks") or [])]
        if not bids or not asks:
            return out
        best_bid, best_ask = bids[0][0], asks[0][0]
        mid = (best_bid + best_ask) / 2 or 0
        out["spread_bps"] = (best_ask - best_bid) / mid * 10000 if mid else 0.0

        levels = asks if side == "BUY" else bids
        remain, cost, filled = qty, 0.0, 0.0
        for price, avail in levels:
            take = min(remain, avail)
            cost += take * price
            filled += take
            remain -= take
            if remain <= 1e-12:
                break
        if filled <= 0:
            return out
        vwap = cost / filled
        ref = best_ask if side == "BUY" else best_bid
        dev_bps = abs(vwap - ref) / ref * 10000 if ref else 0.0
        out.update({"exec_price": vwap, "filled": filled, "dev_bps": dev_bps,
                    "reliable": filled + 1e-12 >= qty and dev_bps <= 50})
        return out

    # ------------------------------------------------------------- process
    def _process(self, sig: dict) -> None:
        self._reset_daily()
        self.cfg = load_config()
        self._stats["signals"] += 1

        targets = self.cfg.get("targets") or {}
        if targets:
            t = targets.get(sig.get("portfolio"))
            if not t or not t.get("enabled"):
                self._reject(sig, "not_target", "该组合未开启复制")
                return

        symbol = sig["symbol"]
        side = sig["side"]
        ps = sig.get("position_side")
        price = float(sig["price"] or 0)
        qty_mirror = float(sig["qty"] or 0)
        rp = float(sig.get("realized_pnl") or 0)
        kind = "open" if rp == 0 else "close"

        age_ms = max(0, int(time.time() * 1000) - int(sig.get("trade_time") or 0))
        if age_ms > self.cfg["max_signal_age_s"] * 1000:
            self._reject(sig, "stale", f"信号已 {age_ms}ms")
            return

        flt = self._symbol_filter(symbol)
        mode = self.cfg["ratio_mode"]
        if mode == "net_equity":
            eq_c = self._mirror_equity(sig["portfolio"])
            ratio = (self.cfg["exec_equity"] / eq_c) if eq_c > 0 else 0.0
            qty_target = qty_mirror * ratio * self.cfg["k"]
        elif mode == "fixed_multiple":
            ratio = self.cfg["k"]
            qty_target = qty_mirror * self.cfg["k"]
        else:
            ratio = 0.0
            notional = self.cfg["margin_per_trade"] * self.cfg["leverage"]
            qty_target = (notional / price) if price > 0 else 0.0

        if price > 0:
            qty_target = min(qty_target, self.cfg["max_order_notional"] / price)

        try:
            held = self._exec_position(symbol, ps)
        except Exception as e:  # noqa: BLE001
            self._reject(sig, "no_position_api", str(e))
            return
        if kind == "close":
            if abs(held) <= 1e-12:
                self._reject(sig, "no_position", "执行账户没有该仓位，跳过平仓")
                return
            qty_target = min(qty_target, abs(held))

        # 平仓向上取整，避免留下残仓；开仓向下取整，宁可少开
        if kind == "close":
            qty_target = self._ceil_step(min(qty_target, abs(held)), flt["step"])
        else:
            qty_target = self._floor_step(qty_target, flt["step"])
        notional = qty_target * price
        if qty_target <= 0 or notional < flt["min_notional"]:
            self._reject(sig, "too_small",
                         f"目标 {qty_target} 名义 {notional:.2f} < minNotional {flt['min_notional']}")
            return

        book_side = "BUY" if side == "BUY" else "SELL"
        book = self._book_estimate(symbol, book_side, qty_target)
        exec_price = book["exec_price"]
        depth_ok = book["reliable"]
        slip_bps = 0.0
        if exec_price > 0 and price > 0:
            slip_bps = (exec_price - price) / price * 10000 * (1 if side == "BUY" else -1)

        # 薄盘口/深度不足：开仓直接放弃，平仓照走（后面走 IOC）
        if not book["reliable"] and kind == "open":
            self._reject(sig, "thin_book",
                         f"盘口不可信 spread={book['spread_bps']:.0f}bps "
                         f"dev={book['dev_bps']:.0f}bps filled={book['filled']:.4g}/{qty_target:.4g}")
            return

        # 开仓：不利滑点超过上限就放弃；有利滑点放行
        # 平仓：不拦（宁可滑点也要走掉），只在日志里标注
        over = slip_bps > self.cfg["max_slippage_pct"] * 100
        if over and kind == "open":
            self._reject(sig, "slippage",
                         f"不利滑点 {slip_bps:.1f}bps > {self.cfg['max_slippage_pct'] * 100:.0f}bps")
            return
        slip_note = "（平仓超滑点仍执行）" if over else ""

        eq_x = self.cfg["exec_equity"]
        sign = 1 if side == "BUY" else -1
        after_pos = held + sign * qty_target
        sym_after = abs(after_pos) * price
        if eq_x > 0 and sym_after / eq_x * 100 > self.cfg["max_symbol_pct"]:
            self._reject(sig, "symbol_cap", f"{symbol} 占比 {sym_after / eq_x * 100:.1f}%")
            return
        total_after = self._total_exposure + (notional if kind == "open" else -notional)
        if total_after > self.cfg["max_total_notional"]:
            self._reject(sig, "total_cap", f"总敞口 {total_after:.1f}U")
            return

        self._pos[(symbol, ps or "BOTH")] = after_pos
        self._exposure[symbol] = self._exposure.get(symbol, 0.0) + (notional if kind == "open" else -notional)
        self._total_exposure = max(0.0, total_after)
        self._stats["intents"] += 1

        rec = {
            "ts": int(time.time() * 1000),
            "kind": kind,
            "nickname": sig.get("nickname"),
            "portfolio": sig.get("portfolio"),
            "symbol": symbol,
            "side": side,
            "ps": ps,
            "qty_mirror": qty_mirror,
            "price_signal": price,
            "ratio_mode": mode,
            "ratio": round(ratio, 4),
            "qty_target": qty_target,
            "notional": round(notional, 4),
            "exec_price_est": round(exec_price, 8),
            "slippage_bps": round(slip_bps, 1),
            "depth_ok": depth_ok,
            "spread_bps": round(book["spread_bps"], 1),
            "dev_bps": round(book["dev_bps"], 1),
            "age_ms": age_ms,
            "held_after": round(after_pos, 8),
            "mode": "shadow",
        }
        with (DATA / f"intents-{datetime.now():%Y%m%d}.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

        log(f"SHADOW {kind.upper():5s} {symbol:10s} {side:4s} "
            f"mirror={qty_mirror:<10g} target={qty_target:<10g} "
            f"px={price:<10g} est={exec_price:<10g} slip={slip_bps:+.1f}bps "
            f"notional={notional:.2f}U hold={after_pos:g}{slip_note}")

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
        log(f"SKIP   {sig.get('symbol'):10s} {reason:12s} {detail}")

    def _reset_daily(self) -> None:
        today = datetime.now().date()
        if today != self._day:
            self._day = today
            self._exposure.clear()
            self._total_exposure = 0.0
