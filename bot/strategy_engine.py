#!/usr/bin/env python3
"""自研策略执行引擎：风控 + 下单 + 持仓管理。

只做「执行」不做信号判断（信号在 strategy_volume_pinbar）。
和复制模块完全独立：自己的台账、自己的风控，互不影响；
唯一共享的是执行账户与 exec_client。

规则（不扛单、不隔夜）：
    · 入场：信号根收盘后立刻 IOC 限价做空（挂买一价下方一点）
    · 出场：止盈 / 止损 / 最长持仓 / 收盘前强平，哪个先到走哪个
    · 实盘平仓 IOC 不成交立刻转市价，避免挂着过夜
"""
from __future__ import annotations

import json
import time
from datetime import datetime, time as dtime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "bot" / "data"
DATA.mkdir(parents=True, exist_ok=True)

INTERVAL_S = {"1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600}


def log(msg: str) -> None:
    print(f"{datetime.now():%H:%M:%S} {msg}", flush=True)


class StrategyEngine:
    def __init__(self, cfg: dict, notify: bool | None = None) -> None:
        self.cfg = cfg
        self.mode = cfg.get("mode", "shadow")
        self.trade = cfg.get("trade") or {}
        self.interval = cfg.get("interval", "1m")
        self.interval_s = INTERVAL_S.get(self.interval, 60)
        self._notify_on = cfg.get("push", True) if notify is None else notify

        self.filters: dict[str, dict] = {}
        self._filters_ready = False
        self._ex = None
        self._hedge: bool | None = None
        self._lev_set: set[str] = set()

        self.positions: dict[str, dict] = {}
        self._cooldown: dict[str, float] = {}
        self._day = datetime.now().date()
        self._halt = ""
        self.realized_today = self._load_realized_today()
        self.stats = {"signals": 0, "opens": 0, "closes": 0, "wins": 0,
                      "pnl": 0.0, "rejects": 0}

    # ------------------------------------------------------------- 基础工具
    def _ex_client(self):
        if self._ex is None:
            from exec_client import ExecClient
            self._ex = ExecClient()
        return self._ex

    def _is_hedge(self) -> bool:
        if self._hedge is None:
            try:
                self._hedge = self._ex_client().position_mode()
            except Exception as e:  # noqa: BLE001
                log(f"持仓模式查询失败，按单向处理: {e}")
                self._hedge = False
        return self._hedge

    def load_filters(self) -> None:
        from copy_engine import http_json
        data = http_json("https://fapi.binance.com/fapi/v1/exchangeInfo", timeout=20)
        for s in data.get("symbols", []):
            info = {"step": 0.001, "tick": 0.0001, "min_notional": 5.0}
            for f in s.get("filters", []):
                t = f.get("filterType")
                if t == "LOT_SIZE":
                    info["step"] = float(f["stepSize"])
                elif t == "MIN_NOTIONAL":
                    info["min_notional"] = float(f.get("notional") or 5)
                elif t == "PRICE_FILTER":
                    info["tick"] = float(f["tickSize"])
            self.filters[s["symbol"]] = info
        self._filters_ready = True
        log(f"exchangeInfo 就绪（{len(self.filters)} 个交易对）")

    def _flt(self, symbol: str) -> dict:
        return self.filters.get(symbol) or {"step": 0.001, "tick": 0.0001, "min_notional": 5.0}

    def _book(self, symbol: str) -> tuple[float, float]:
        """返回 (bid, ask)；失败返回 (0,0)。"""
        from copy_engine import http_json
        try:
            d = http_json(f"https://fapi.binance.com/fapi/v1/depth?symbol={symbol}&limit=5")
            return float(d["bids"][0][0]), float(d["asks"][0][0])
        except Exception:  # noqa: BLE001
            return 0.0, 0.0

    @staticmethod
    def _tick_round(price: float, tick: float) -> float:
        if tick <= 0:
            return price
        return round(round(price / tick) * tick, 10)

    @staticmethod
    def _floor_step(qty: float, step: float) -> float:
        if step <= 0:
            return qty
        return round(int(qty / step + 1e-9) * step, 10)

    def _ioc_price(self, symbol: str, side: str, bid: float, ask: float,
                   fallback: float) -> float:
        buf = float(self.trade.get("ioc_buffer_pct", 0.05)) / 100.0
        tick = self._flt(symbol)["tick"]
        if side == "SELL":
            ref = bid or fallback
            px = self._tick_round(ref * (1 - buf), tick)
            if px > ref:                       # 确保是过价，能吃到买一
                px = self._tick_round(ref - tick, tick)
        else:
            ref = ask or fallback
            px = self._tick_round(ref * (1 + buf), tick)
            if px < ref:
                px = self._tick_round(ref + tick, tick)
        return max(px, tick)

    # ------------------------------------------------------------- 风控闸门
    def _cutoff_time(self) -> dtime:
        raw = str(self.trade.get("cutoff") or "23:55")
        try:
            h, m = raw.split(":")
            return dtime(int(h), int(m))
        except Exception:  # noqa: BLE001
            return dtime(23, 55)

    def _past_cutoff(self) -> bool:
        return datetime.now().time() >= self._cutoff_time()

    def _near_cutoff(self) -> bool:
        now = datetime.now()
        cutoff = datetime.combine(now.date(), self._cutoff_time())
        return (cutoff - now).total_seconds() <= float(self.trade.get("max_hold_s", 180))

    def _reset_daily(self) -> None:
        today = datetime.now().date()
        if today != self._day:
            self._day = today
            self.realized_today = 0.0
            self._halt = ""
            log("跨日：已实现盈亏与熔断状态重置")

    def _load_realized_today(self) -> float:
        path = DATA / f"strategy-{datetime.now():%Y%m%d}.jsonl"
        total = 0.0
        if not path.exists():
            return 0.0
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if r.get("kind") == "close":
                total += float(r.get("pnl") or 0)
        return total

    def _record(self, kind: str, **fields) -> None:
        rec = {"ts": int(time.time() * 1000), "kind": kind, **fields}
        with (DATA / f"strategy-{datetime.now():%Y%m%d}.jsonl").open("a",
                                                                     encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def _notify(self, text: str) -> None:
        if not self._notify_on:
            return
        try:
            from listen_copy import tg_send_async
            tg_send_async(text)
        except Exception:  # noqa: BLE001
            pass

    def _reject(self, symbol: str, reason: str, detail: str, **extra) -> None:
        self.stats["rejects"] += 1
        self._record("reject", symbol=symbol, reason=reason, detail=detail, **extra)
        log(f"SKIP   {symbol:12s} {reason:14s} {detail}")

    # ------------------------------------------------------------- 开仓
    def cooldown_ok(self, symbol: str) -> bool:
        bars = int((self.cfg.get("signal") or {}).get("cooldown_bars", 0))
        last = self._cooldown.get(symbol, 0.0)
        return (time.time() - last) >= bars * self.interval_s

    def open_short(self, symbol: str, detail: dict, signal_price: float) -> bool:
        self._reset_daily()
        self.stats["signals"] += 1

        if self.mode not in ("shadow", "live_dry", "live"):
            self._reject(symbol, "bad_mode", f"未知模式 {self.mode}")
            return False
        if self._past_cutoff():
            self._reject(symbol, "cutoff", "已过收盘时间，不再开仓")
            return False
        if self._near_cutoff():
            self._reject(symbol, "near_cutoff", "离强平时间太近，开了也拿不住")
            return False
        max_loss = float(self.trade.get("max_daily_loss", 0) or 0)
        if self._halt or (max_loss > 0 and self.realized_today <= -max_loss):
            self._halt = self._halt or f"当日亏损已达 {self.realized_today:.2f}U"
            self._reject(symbol, "daily_loss", self._halt)
            return False
        if len(self.positions) >= int(self.trade.get("max_positions", 1) or 1):
            self._reject(symbol, "max_positions", f"已持有 {len(self.positions)} 个仓位")
            return False
        if symbol in self.positions:
            self._reject(symbol, "dup_symbol", "该币已有策略仓位")
            return False
        if not self.cooldown_ok(symbol):
            self._reject(symbol, "cooldown", "冷却中")
            return False
        if signal_price <= 0:
            self._reject(symbol, "bad_price", f"信号价 {signal_price}")
            return False

        # 实盘要确认账户在该币上是空的（复制模块可能同时持仓）
        if self.mode == "live":
            try:
                amt = self._ex_client().position(symbol, "SHORT" if self._is_hedge() else None)
            except Exception as e:  # noqa: BLE001
                self._reject(symbol, "position_api", f"查持仓失败: {e}")
                return False
            if abs(amt) > 1e-12:
                self._reject(symbol, "symbol_busy", f"账户已有持仓 {amt:g}，跳过")
                return False

        bid, ask = self._book(symbol)
        max_slip = float(self.trade.get("max_slippage_pct", 0.3) or 0.3)
        if bid > 0:
            slip = (signal_price - bid) / signal_price * 100
            if slip > max_slip:
                self._reject(symbol, "slippage",
                             f"买一 {bid:g} 比信号价低 {slip:.2f}% > {max_slip:g}%")
                return False

        notional = float(self.trade.get("notional", 10.0) or 10.0)
        flt = self._flt(symbol)
        qty = self._floor_step(notional / signal_price, flt["step"])
        mn = flt["min_notional"] * 1.01
        if qty * signal_price < mn:
            qty = self._floor_step(mn / signal_price + flt["step"], flt["step"]) \
                if flt["step"] > 0 else mn / signal_price
        if qty <= 0:
            self._reject(symbol, "qty_zero", "计算数量为 0")
            return False
        if qty * signal_price > notional * 1.5:
            self._reject(symbol, "too_small_notional",
                         f"最小名义 {flt['min_notional']:g}U 超过单笔 {notional:g}U 的 1.5 倍")
            return False

        px = self._ioc_price(symbol, "SELL", bid, ask, signal_price)
        self._cooldown[symbol] = time.time()      # 真正要下单了才计入冷却
        params = {"symbol": symbol, "side": "SELL", "qty": qty, "price": px,
                  "hedge": self._is_hedge()}
        log(f"OPEN?  {symbol:12s} SHORT qty={qty:g} px={px:g} "
            f"名义={qty * signal_price:.2f}U 模式={self.mode}")
        self._record("open_attempt", symbol=symbol, qty=qty, price=px,
                     signal_price=signal_price, notional=round(qty * signal_price, 4),
                     mode=self.mode, signal=detail)

        if self.mode == "live":
            try:
                self._ensure_leverage(symbol)
                resp = self._send(symbol, "SELL", qty, px, "SHORT")
            except Exception as e:  # noqa: BLE001
                self._reject(symbol, "order_failed", str(e)[:160])
                self._notify(f"⚠️ 策略开仓失败 {symbol}\n{str(e)[:150]}")
                return False
            filled = float(resp.get("executedQty") or 0)
            if filled <= 0:
                self._reject(symbol, "unfilled", f"status={resp.get('status')}")
                return False
            entry = float(resp.get("avgPrice") or 0) or px
            qty = filled
        else:
            entry = px if bid > 0 else signal_price

        pos = {
            "symbol": symbol, "qty": qty, "entry": entry,
            "opened": time.time(), "mode": self.mode,
            "peak": 0.0, "detail": detail,
        }
        self.positions[symbol] = pos
        self.stats["opens"] += 1
        self._record("open", symbol=symbol, qty=qty, entry=entry,
                     notional=round(qty * entry, 4), mode=self.mode,
                     signal=detail)
        log(f"OPEN   {symbol:12s} SHORT {qty:g} @ {entry:g} "
            f"({qty * entry:.2f}U) TP{self.trade.get('tp_pct')}% "
            f"SL{self.trade.get('sl_pct')}%")
        self._notify(f"🔻 策略开空 {symbol}\n{qty:g} @ {entry:g}  {qty * entry:.1f}U\n"
                     f"放量 {detail.get('vol_ratio')}x  TR {detail.get('tr_ratio')}x  "
                     f"上影 {detail.get('upper_ratio', 0) * 100:.0f}%")
        return True

    def _ensure_leverage(self, symbol: str) -> None:
        if not self.trade.get("manage_leverage") or symbol in self._lev_set:
            return
        lev = int(self.trade.get("leverage", 10) or 10)
        try:
            self._ex_client().set_leverage(symbol, lev)
        except Exception as e:  # noqa: BLE001
            log(f"⚠ 杠杆设置失败 {symbol}: {e}")
        self._lev_set.add(symbol)

    def _send(self, symbol: str, side: str, qty: float, px: float,
              ps: str, reduce_only: bool = False, market: bool = False) -> dict:
        ex = self._ex_client()
        hedge = self._is_hedge()
        if market:
            return ex.order(symbol, side, qty, order_type="MARKET",
                            position_side=ps if hedge else None,
                            reduce_only=reduce_only and not hedge)
        return ex.order(symbol, side, qty, order_type="LIMIT", price=px,
                        time_in_force="IOC",
                        position_side=ps if hedge else None,
                        reduce_only=reduce_only and not hedge)

    # ------------------------------------------------------------- 持仓管理
    def manage(self, prices: dict[str, float]) -> None:
        if not self.positions:
            return
        tp = float(self.trade.get("tp_pct", 0.8) or 0.8)
        sl = float(self.trade.get("sl_pct", 0.5) or 0.5)
        max_hold = float(self.trade.get("max_hold_s", 180) or 180)
        now = time.time()
        for symbol, pos in list(self.positions.items()):
            if self._past_cutoff():
                self.close(symbol, "cutoff", prices.get(symbol) or pos["entry"])
                continue
            mark = float(prices.get(symbol) or 0)
            if mark <= 0:
                continue
            pnl_pct = (pos["entry"] - mark) / pos["entry"] * 100
            if pnl_pct > pos["peak"]:
                pos["peak"] = pnl_pct
            if pnl_pct >= tp:
                self.close(symbol, "take_profit", mark)
            elif pnl_pct <= -sl:
                self.close(symbol, "stop_loss", mark)
            elif now - pos["opened"] >= max_hold:
                self.close(symbol, "timeout", mark)

    def close(self, symbol: str, reason: str, mark: float) -> None:
        pos = self.positions.get(symbol)
        if not pos:
            return
        qty = pos["qty"]
        entry = pos["entry"]
        log(f"CLOSE? {symbol:12s} {reason} mark={mark:g} "
            f"pnl≈{(entry - mark) / entry * 100:+.2f}%")
        exit_price = mark
        if pos["mode"] == "live":
            bid, ask = self._book(symbol)
            px = self._ioc_price(symbol, "BUY", bid, ask, mark)
            try:
                resp = self._send(symbol, "BUY", qty, px, "SHORT", reduce_only=True)
                filled = float(resp.get("executedQty") or 0)
                if filled <= 0:
                    log(f"⚠ 平仓 IOC 未成交 {symbol}，转市价")
                    resp = self._send(symbol, "BUY", qty, 0, "SHORT",
                                      reduce_only=True, market=True)
                    filled = float(resp.get("executedQty") or 0)
                if filled <= 0:
                    self._reject(symbol, "close_failed",
                                 f"平仓未成交 status={resp.get('status')}")
                    self._notify(f"🚨 策略平仓失败 {symbol}，请手动处理")
                    return
                exit_price = float(resp.get("avgPrice") or 0) or px
                qty = filled
            except Exception as e:  # noqa: BLE001
                self._reject(symbol, "close_failed", str(e)[:160])
                self._notify(f"🚨 策略平仓失败 {symbol}\n{str(e)[:150]}")
                return

        pnl = (entry - exit_price) * qty
        held = time.time() - pos["opened"]
        self.realized_today += pnl
        self.stats["closes"] += 1
        self.stats["pnl"] += pnl
        if pnl > 0:
            self.stats["wins"] += 1
        self.positions.pop(symbol, None)
        self._cooldown[symbol] = time.time()
        self._record("close", symbol=symbol, reason=reason, qty=qty,
                     entry=entry, exit=exit_price, pnl=round(pnl, 4),
                     pnl_pct=round((entry - exit_price) / entry * 100, 3),
                     held_s=round(held, 1), peak_pct=round(pos.get("peak", 0), 3),
                     mode=pos["mode"])
        log(f"CLOSE  {symbol:12s} {reason:12s} {qty:g} @ {exit_price:g} "
            f"pnl={pnl:+.3f}U ({held:.0f}s) 今日 {self.realized_today:+.2f}U")
        self._notify(f"{'✅' if pnl >= 0 else '❌'} 策略平空 {symbol} {reason}\n"
                     f"{qty:g} @ {exit_price:g}\n"
                     f"盈亏 {pnl:+.3f}U（{held:.0f}s）")
        self._check_halt()

    def _check_halt(self) -> None:
        max_loss = float(self.trade.get("max_daily_loss", 0) or 0)
        if max_loss > 0 and self.realized_today <= -max_loss and not self._halt:
            self._halt = f"当日亏损 {self.realized_today:.2f}U 达到上限 {max_loss:g}U"
            log(f"🛑 {self._halt}，停止开仓")
            self._notify(f"🛑 策略熔断\n{self._halt}\n不再开新仓，已有仓位照常管理")

    def close_all(self, prices: dict[str, float], reason: str = "shutdown") -> None:
        for symbol in list(self.positions):
            self.close(symbol, reason, prices.get(symbol) or self.positions[symbol]["entry"])

    def summary(self) -> str:
        s = self.stats
        wr = (s["wins"] / s["closes"] * 100) if s["closes"] else 0.0
        return (f"信号 {s['signals']}  开仓 {s['opens']}  平仓 {s['closes']}  "
                f"胜率 {wr:.0f}%  今日 {self.realized_today:+.2f}U  拒绝 {s['rejects']}")
