"""均价加仓模式：逆行加仓 + 回均价止盈（从 strategy_engine 拆出）。"""
from __future__ import annotations

import time

import symbol_filter as FILTER
from logutil import log


class BasketMixin:

    def _manage_basket(self, symbol: str, pos: dict, mark: float, now: float) -> None:
        """暴涨做空 + 逆行加仓 + 回均价止盈，带硬止损与时间止损。"""
        b = self.basket
        pnl = self._pnl_pct(pos.get("side", "SHORT"), pos["avg"], mark)
        pos["peak"] = max(pos["peak"], pnl)
        tp = float(b.get("basket_tp_pct", 0.5) or 0.5)
        hard = float(b.get("hard_stop_pct", 6.0) or 6.0)
        hold = float(b.get("max_hold_s", 3600) or 3600)
        if pnl >= tp:
            self._close_partial(symbol, "basket_tp", mark, 1.0)
            return
        if pnl <= -hard:
            self._close_partial(symbol, "basket_stop", mark, 1.0)
            return
        if now - pos["opened_at"] >= hold:
            self._close_partial(symbol, "basket_timeout", mark, 1.0)
            return
        step = float(b.get("add_step_pct", 1.26) or 1.26)
        max_adds = int(b.get("max_adds", 3) or 0)
        adds = len(pos["entries"]) - 1
        last = float(pos.get("last_add_pnl", 0.0))
        if adds < max_adds and (last - pnl) >= step - 1e-9:
            self._check_halt()
            if self._halt:          # 当日亏损已熔断：不再加仓放大风险
                return
            allowed, why = FILTER.is_allowed(symbol)
            if not allowed:
                if not pos.get("_add_blocked_logged"):
                    pos["_add_blocked_logged"] = True
                    log(f"{symbol:12s} 已被过滤（{why}），停止加仓")
                return
            if now - float(pos.get("last_add_try", 0) or 0) >= 30:
                pos["last_add_try"] = now
                self._basket_add(symbol, pos, mark, pnl)

    def _basket_add(self, symbol: str, pos: dict, mark: float, pnl: float) -> None:
        side = str(pos.get("side") or "SHORT").upper()
        b = self.basket
        notional = float(b.get("notional", self.trade.get("notional", 5)) or 5)
        flt = self._flt(symbol)
        qty = self._floor_step(notional / mark, flt["step"])
        mn = flt["min_notional"] * 1.01
        if qty <= 0 or qty * mark < mn:      # 最小名义优先，向上补一个步长
            qty = self._floor_step(mn / mark + flt["step"], flt["step"])
        if qty <= 0:
            return
        bid, ask = self._book(symbol)
        px = self._ioc_price(symbol, self._entry_side(side), bid, ask, mark)
        log(f"ADD?   {symbol:12s} 逆行加仓 #{len(pos['entries'])} qty={qty:g} px={px:g}")
        entry = px
        if self.mode == "live":
            try:
                self._ensure_leverage(symbol)
                resp = self._send(symbol, self._entry_side(side), qty, px, self._pside(side))
            except Exception as e:  # noqa: BLE001
                log(f"⚠ 加仓失败 {symbol}: {e}")
                return
            filled = float(resp.get("executedQty") or 0)
            if filled <= 0:
                log(f"⚠ 加仓未成交 {symbol}")
                return
            qty = filled
            entry = float(resp.get("avgPrice") or 0) or px
        pos["entries"].append({"qty": qty, "price": entry, "ts": time.time()})
        pos["qty"] = sum(e["qty"] for e in pos["entries"])
        pos["avg"] = sum(e["qty"] * e["price"] for e in pos["entries"]) / pos["qty"]
        pos["last_add_pnl"] = pnl
        self.stats["adds"] += 1
        self._record("add", symbol=symbol, qty=qty, price=entry,
                     avg=round(pos["avg"], 8), total_qty=pos["qty"],
                     notional=round(pos["qty"] * pos["avg"], 4),
                     entries=len(pos["entries"]), mode=self.mode, reason="basket")
        log(f"ADD    {symbol:12s} 共 {pos['qty']:g} 均价 {pos['avg']:g} "
            f"（{len(pos['entries'])} 笔）")
        dir_txt = "多" if side == "LONG" else "空"
        self._notify(f"➕ 均价加{dir_txt} {symbol}\n{qty:g} @ {entry:g}\n"
                     f"共 {pos['qty']:g} 均价 {pos['avg']:g}")
        self._cancel_protect(symbol, pos)
        self._place_protect(symbol, pos)
        self._save_state()

    # ------------------------------------------------------- 交易所侧条件单
