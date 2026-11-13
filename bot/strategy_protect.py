"""交易所侧条件单：开仓/加仓后挂全平止盈止损（从 strategy_engine 拆出）。"""
from __future__ import annotations

import time

from logutil import log


class ProtectMixin:

    def _cancel_protect(self, symbol: str, pos: dict) -> None:
        ids = pos.pop("protect", None) or []
        if self.mode != "live":
            return
        ex = self._ex_client()
        for oid in ids:
            try:
                ex.cancel_algo(symbol, oid)
            except Exception:  # noqa: BLE001
                pass
        try:                          # 兜底扫一遍，避免挂单失败留下的残单
            ex.cancel_all_algo(symbol)
        except Exception:  # noqa: BLE001
            pass

    def _wait_no_algo(self, ex, symbol: str, timeout: float = 2.0) -> bool:
        """等交易所把条件单真正撤掉（撤单是异步的，立刻挂新单会 -4130）。"""
        end = time.time() + timeout
        while time.time() < end:
            try:
                if not ex.open_algo_orders(symbol):
                    return True
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.25)
        return False

    def _place_one_protect(self, ex, symbol: str, kind: str, price: float,
                           ps: str | None, qty: float | None = None,
                           working_type: str = "MARK_PRICE",
                           close_side: str = "BUY") -> int | None:
        """挂单个条件单，处理「旧单还没撤干净」(-4130) 与连接抖动。"""
        self._protect_err = ""
        last_msg = ""
        for attempt in range(1, 5):
            try:
                r = ex.stop_close(symbol, close_side, price, kind=kind,
                                  position_side=ps, qty=qty,
                                  working_type=working_type)
                return r.get("algoId") or r.get("orderId")
            except Exception as e:  # noqa: BLE001
                msg = str(e)
                last_msg = msg
                if "-4130" in msg:
                    try:
                        ex.cancel_all_algo(symbol)
                    except Exception:  # noqa: BLE001
                        pass
                    self._wait_no_algo(ex, symbol, 2.0)
                    time.sleep(0.2 * attempt)
                    continue
                # 连接类错误：订单可能实际已挂上，查一次再说
                try:
                    for row in ex.open_algo_orders(symbol):
                        trig = float(row.get("triggerPrice") or 0)
                        if (row.get("orderType") == kind
                                and abs(trig - price) <= max(price * 0.002, 1e-9)):
                            return row.get("algoId")
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(0.3)
        self._protect_err = last_msg
        log(f"⚠ {kind} 条件单失败 {symbol}: {last_msg}")
        return None

    def _mark_of(self, symbol: str, fallback: float = 0.0) -> float:
        try:
            bid, ask = self._book(symbol)
            if bid > 0 and ask > 0:
                return (bid + ask) / 2
        except Exception:  # noqa: BLE001
            pass
        return fallback

    def _place_protect(self, symbol: str, pos: dict) -> None:
        """开仓/加仓后，在交易所挂「全平」止盈止损条件单，进程挂了也有保护。"""
        if self.mode != "live" or not self.basket_on or not self.protect_on:
            return
        b = self.basket
        tp = float(b.get("basket_tp_pct", 0) or 0)
        hard = float(b.get("hard_stop_pct", 0) or 0)
        if tp <= 0 or hard <= 0:
            return
        avg = float(pos["avg"])
        side = str(pos.get("side") or "SHORT").upper()
        close_side = self._close_side(side)
        tick = self._flt(symbol)["tick"]
        sl_px = self._tick_round(self._sl_price(side, avg, hard), tick)
        tp_px = self._tick_round(self._tp_price(side, avg, tp), tick)
        ps = self._pside(side)
        ex = self._ex_client()
        # 加仓后均价变了：先把旧条件单撤干净，否则新单会被 -4130 拒
        try:
            n = ex.cancel_all_algo(symbol)
            if n:
                log(f"保护单 撤掉旧条件单 {n} 张")
            self._wait_no_algo(ex, symbol, 2.0)
        except Exception:  # noqa: BLE001
            pass
        ids = []
        sl_work = str(b.get("sl_working_type", "MARK_PRICE") or "MARK_PRICE")
        tp_work = str(b.get("tp_working_type", "CONTRACT_PRICE") or "CONTRACT_PRICE")
        sl_id = self._place_one_protect(ex, symbol, "STOP_MARKET", sl_px, ps,
                                       working_type=sl_work,
                                       close_side=close_side)
        if sl_id:
            ids.append(sl_id)
        elif "-2021" in str(getattr(self, "_protect_err", "")):
            mark = self._mark_of(symbol, avg)
            log(f"⚠ {symbol} 止损价 {sl_px:g} 已被击穿（-2021），立即市价平仓")
            self._notify(f"⚠ {symbol} 止损价已被击穿，立即市价平仓")
            self._close_partial(symbol, "basket_stop", mark, 1.0, reprotect=False)
            return
        tp_id = self._place_one_protect(ex, symbol, "TAKE_PROFIT_MARKET", tp_px, ps,
                                       qty=pos["qty"], working_type=tp_work,
                                       close_side=close_side)
        if tp_id:
            ids.append(tp_id)
        elif "-2021" in str(getattr(self, "_protect_err", "")):
            mark = self._mark_of(symbol, avg)
            log(f"⚠ {symbol} 止盈价 {tp_px:g} 已被击穿（-2021），立即市价止盈")
            self._close_partial(symbol, "basket_tp", mark, 1.0, reprotect=False)
            return
        pos["protect"] = ids if sl_id else []   # 止损没挂上就当作未保护，稍后重试
        if not sl_id:
            log(f"⚠ {symbol} 止损条件单未挂上，30 秒后重试")
        if ids:
            log(f"PROTECT {symbol:12s} 止损 {sl_px:g} / 止盈 {tp_px:g}（交易所条件单）")
            self._notify(f"🛡 {symbol} 已挂交易所条件单\n"
                         f"止损 {sl_px:g}（-{hard:g}%）/ 止盈 {tp_px:g}（+{tp:g}%）")

    # ------------------------------------------------------- 持仓持久化 / 对账
