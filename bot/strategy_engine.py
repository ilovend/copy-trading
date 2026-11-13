#!/usr/bin/env python3
"""自研策略执行引擎：加仓 / 分批止盈 / 追踪止盈 / 风控。

只做「执行」不做信号判断（信号在 strategy_volume_pinbar）。
和复制模块完全独立：自己的台账、自己的风控，互不影响；
唯一共享的是执行账户与 exec_client。

一笔仓位由多次加仓组成：
    · 首笔：信号触发立刻 IOC 做空
    · 加仓：add_window_s 内再次出信号，且价格不低于上一笔，最多 max_adds 次
    · 出场：止损整仓走；止盈按阶梯分批平；剩余仓位追踪止盈
    · 另外还有最长持仓 / 收盘前强平，不扛单、不隔夜
"""
from __future__ import annotations

import json
import time
from datetime import datetime, time as dtime
import os
from pathlib import Path

import symbol_filter as FILTER
from strategy_basket import BasketMixin
from strategy_protect import ProtectMixin

ROOT = Path(__file__).resolve().parents[1]
DATA = Path(os.environ.get("STRATEGY_DATA_DIR") or (ROOT / "bot" / "data"))
DATA.mkdir(parents=True, exist_ok=True)

INTERVAL_S = {"1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600}


from logutil import log, set_log_file  # noqa: E402

STATE_PATH = Path(__file__).resolve().parent / "data" / "strategy-open.json"


def ladder_of(trade: dict) -> list[tuple[float, float]]:
    """止盈阶梯：[(触发盈利%, 平掉当前仓位的比例), ...]。"""
    out = []
    for i in (1, 2):
        level = float(trade.get(f"tp{i}_pct", 0) or 0)
        frac = float(trade.get(f"tp{i}_frac", 0) or 0)
        if level > 0 and frac > 0:
            out.append((level, min(frac, 1.0)))
    return out


def simulate_exit(bars: list[dict], start: int, trade: dict,
                  bar_seconds: int = 60) -> tuple[float, str, int]:
    """回放用的粗估：同一根 K 线同时触及止损和止盈时按止损算。

    返回 (相对开仓名义的盈亏%, 出场原因, 持仓根数)。
    """
    entry = float(bars[start]["c"])
    sl = float(trade.get("sl_pct", 0.5) or 0)
    ladder = ladder_of(trade)
    trail_act = float(trade.get("trail_activate_pct", 0) or 0)
    trail_gap = float(trade.get("trail_gap_pct", 0) or 0)
    hold_bars = max(1, int(round(float(trade.get("max_hold_s", 180) or 180)
                                 / max(bar_seconds, 1))))
    remaining = 1.0
    realized = 0.0
    peak = 0.0
    trail_on = False
    trail_level = 0.0
    done = [False] * len(ladder)
    reason = "timeout"
    held = 0
    for j in range(start + 1, min(start + 1 + hold_bars, len(bars))):
        held = j - start
        hi, lo, cl = float(bars[j]["h"]), float(bars[j]["l"]), float(bars[j]["c"])
        if (hi / entry - 1) * 100 >= sl:
            realized -= sl * remaining          # 做空被止损：负收益
            return realized, "stop_loss", held
        pnl_best = (entry - lo) / entry * 100
        pnl_close = (entry - cl) / entry * 100
        peak = max(peak, pnl_best)
        for idx, (level, frac) in enumerate(ladder):
            if done[idx] or pnl_best < level:
                continue
            done[idx] = True
            take = remaining * frac
            realized += level * take
            remaining -= take
            reason = f"tp{idx + 1}"
        if remaining <= 1e-9:
            return realized, reason, held
        if trail_act > 0:
            if not trail_on and peak >= trail_act:
                trail_on = True
                trail_level = max(trail_act, peak - trail_gap)
            elif trail_on:
                trail_level = max(trail_level, peak - trail_gap)
            if trail_on and pnl_close <= trail_level:
                # 触发线只是「该走了」，实际成交用收盘价（更保守，避免高估）
                realized += min(trail_level, pnl_close) * remaining
                return realized, "trail", held
    last = float(bars[min(start + hold_bars, len(bars) - 1)]["c"])
    realized += (entry - last) / entry * 100 * remaining
    return realized, reason, held


class StrategyEngine(BasketMixin, ProtectMixin):
    def __init__(self, cfg: dict, notify: bool | None = None) -> None:
        self.cfg = cfg
        self.mode = cfg.get("mode", "shadow")
        self.trade = cfg.get("trade") or {}
        self.interval = cfg.get("interval", "1m")
        self.interval_s = INTERVAL_S.get(self.interval, 60)
        self._notify_on = cfg.get("push", True) if notify is None else notify

        self.filters: dict[str, dict] = {}
        self._ex = None
        self._hedge: bool | None = None
        self._lev_set: set[str] = set()
        self.basket = cfg.get("basket") or {}
        self.basket_on = bool(self.basket.get("enabled"))
        # 安全兜底：fade 且止损设成 ≥50%（等于没有止损）时，强制启用均价止盈止损
        _sid = str(cfg.get("strategy") or "").lower()
        _sl = float(self.trade.get("sl_pct", 0) or 0)
        if (_sid == "fade" or (not _sid and (cfg.get("fade") or {}).get("enabled"))) \
                and _sl >= 50:
            self.basket_on = True
        self.protect_on = bool(self.basket.get("protect", True))
        self._symbol_stop: dict[str, dict] = {}

        self.positions: dict[str, dict] = {}
        self._cooldown: dict[str, float] = {}
        self._day = datetime.now().date()
        self._halt = ""
        self.realized_today = self._load_realized_today()
        self.stats = {"signals": 0, "opens": 0, "adds": 0, "closes": 0,
                      "wins": 0, "pnl": 0.0, "rejects": 0}

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
        from net import http_json
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
        log(f"exchangeInfo 就绪（{len(self.filters)} 个交易对）")

    def _flt(self, symbol: str) -> dict:
        return self.filters.get(symbol) or {"step": 0.001, "tick": 0.0001,
                                            "min_notional": 5.0}

    def _book(self, symbol: str) -> tuple[float, float]:
        from net import http_json
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
            if px > ref:
                px = self._tick_round(ref - tick, tick)
        else:
            ref = ask or fallback
            px = self._tick_round(ref * (1 + buf), tick)
            if px < ref:
                px = self._tick_round(ref + tick, tick)
        return max(px, tick)

    # ------------------------------------------------------------- 风控闸门
    def _cutoff_time(self) -> dtime | None:
        """返回强平时间；返回 None 表示不设收盘（全天候）。"""
        raw = str(self.trade.get("cutoff") or "").strip().lower()
        if raw in ("", "off", "none", "0", "false", "全天"):
            return None
        try:
            h, m = raw.split(":")
            return dtime(int(h), int(m))
        except Exception:  # noqa: BLE001
            return dtime(23, 55)

    def _past_cutoff(self) -> bool:
        cutoff = self._cutoff_time()
        return False if cutoff is None else datetime.now().time() >= cutoff

    def _near_cutoff(self) -> bool:
        cutoff_t = self._cutoff_time()
        if cutoff_t is None:
            return False
        now = datetime.now()
        cutoff = datetime.combine(now.date(), cutoff_t)
        return (cutoff - now).total_seconds() <= float(self.trade.get("max_hold_s", 180))

    def _reset_daily(self) -> None:
        today = datetime.now().date()
        if today != self._day:
            self._day = today
            self.realized_today = 0.0
            self._halt = ""
            log("跨日：已实现盈亏与熔断状态重置")

    def _income_today(self) -> float:
        """交易所当日已实现盈亏（含手续费与资金费）。"""
        start = int(datetime.now().replace(hour=0, minute=0, second=0,
                                           microsecond=0).timestamp() * 1000)
        rows = self._ex_client().income(start, 1000)
        total = sum(float(r.get("income") or 0) for r in rows
                    if r.get("incomeType") in ("REALIZED_PNL", "COMMISSION", "FUNDING_FEE"))
        return round(total, 6)

    def _load_realized_today(self) -> float:
        # 实盘以交易所流水为准（准确、含费用、不受本地测试台账影响）
        if self.mode == "live":
            try:
                return self._income_today()
            except Exception as e:  # noqa: BLE001
                log(f"⚠ 读交易所今日盈亏失败，回退台账: {str(e)[:80]}")
        elif self.mode == "live_dry":
            return 0.0          # 干跑只统计本次进程内的模拟盈亏，避免旧台账干扰
        return self._ledger_realized()   # 影子模式保留重启续算（研究用）

    def _ledger_realized(self) -> float:
        """从台账重建今日已实现盈亏（按 symbol 分回合，避免重复计算）。

        一次仓位可能先有若干 close_partial，最后一条 close 的 pnl_total 已包含它们；
        所以每个 symbol 用「累积 partial → 遇到 close 用 pnl_total 结算」的方式。
        """
        path = DATA / f"strategy-{datetime.now():%Y%m%d}.jsonl"
        total = 0.0
        partial: dict[str, float] = {}
        if not path.exists():
            return 0.0
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            sym = str(r.get("symbol") or "")
            kind = r.get("kind")
            if kind == "close_partial":
                partial[sym] = partial.get(sym, 0.0) + float(r.get("pnl") or 0)
            elif kind == "close":
                done = r.get("pnl_total")
                total += float(done) if done is not None else partial.get(sym, 0.0)
                partial[sym] = 0.0
        total += sum(partial.values())
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
            from notify import tg_send_async
            tg_send_async(text)
        except Exception:  # noqa: BLE001
            pass

    def _reject(self, symbol: str, reason: str, detail: str, **extra) -> None:
        self.stats["rejects"] += 1
        self._record("reject", symbol=symbol, reason=reason, detail=detail, **extra)
        log(f"SKIP   {symbol:12s} {reason:14s} {detail}")

    def cooldown_ok(self, symbol: str) -> bool:
        bars = int((self.cfg.get("signal") or {}).get("cooldown_bars", 0))
        last = self._cooldown.get(symbol, 0.0)
        return (time.time() - last) >= bars * self.interval_s

    # ------------------------------------------------------------- 开仓 / 加仓
    def _episode_guards(self, symbol: str) -> str:
        """新开一轮仓位前的公共闸门，返回拒绝原因（空串=通过）。"""
        if self._past_cutoff():
            return "已过收盘时间，不再开仓"
        if self._near_cutoff():
            return "离强平时间太近，开了也拿不住"
        max_loss = float(self.trade.get("max_daily_loss", 0) or 0)
        if self._halt or (max_loss > 0 and self.realized_today <= -max_loss):
            self._halt = self._halt or f"当日亏损已达 {self.realized_today:.2f}U"
            return self._halt
        return ""


    # ------------------------------------------------------------- 方向辅助
    @staticmethod
    def _entry_side(side: str) -> str:
        return "SELL" if side == "SHORT" else "BUY"

    @staticmethod
    def _close_side(side: str) -> str:
        return "BUY" if side == "SHORT" else "SELL"

    def _pside(self, side: str) -> str | None:
        return side if self._is_hedge() else None

    @staticmethod
    def _pnl_pct(side: str, avg: float, price: float) -> float:
        return ((avg - price) if side == "SHORT" else (price - avg)) / avg * 100

    @staticmethod
    def _pnl_u(side: str, avg: float, price: float, qty: float) -> float:
        return ((avg - price) if side == "SHORT" else (price - avg)) * qty

    @staticmethod
    def _tp_price(side: str, avg: float, pct: float) -> float:
        return avg * (1 - pct / 100) if side == "SHORT" else avg * (1 + pct / 100)

    @staticmethod
    def _sl_price(side: str, avg: float, pct: float) -> float:
        return avg * (1 + pct / 100) if side == "SHORT" else avg * (1 - pct / 100)

    def open_short(self, symbol: str, detail: dict, signal_price: float) -> bool:
        self._reset_daily()
        self.stats["signals"] += 1
        side = str(detail.get("side") or "SHORT").upper()
        if side not in ("SHORT", "LONG"):
            side = "SHORT"
        ok, why = FILTER.is_allowed(symbol)
        if not ok:
            self._reject(symbol, "filtered", why)
            return False
        rw = self.cfg.get("runaway") or {}
        if rw.get("enabled", True) and side == "SHORT":
            c1h = float(detail.get("chg_1h") or 0)
            c15 = float(detail.get("chg_15m") or 0)
            lim1 = float(rw.get("entry_1h_pct", 20) or 20)
            lim15 = float(rw.get("entry_15m_pct", 10) or 10)
            if c1h >= lim1 or c15 >= lim15:
                why = f"1h {c1h:+.1f}% / 15m {c15:+.1f}%"
                cap = int(rw.get("max_bans", 0) or 0)
                today_black = len(FILTER.load().get("blacklist") or {})
                if cap <= 0 or today_black < cap:
                    FILTER.block(symbol, f"垂直拉升 {why}", days=1)
                    ok = True
                else:
                    ok = False
                self._reject(symbol, "runaway" if ok else "runaway_full",
                             f"垂直拉升 {why}，{'' if ok else '拉黑名额已满，'}今日不做")
                log(f"⛔ {symbol:12s} 垂直拉升 {why} → "
                    f"{'今日拉黑' if ok else '名额已满，跳过本单'}")
                self._notify(f"⛔ {symbol} 垂直拉升 {why}\n"
                             f"{'今日拉黑，不再交易该币' if ok else '拉黑名额已满，本单同样跳过'}")
                return False
        if self.mode not in ("shadow", "live_dry", "live"):
            self._reject(symbol, "bad_mode", f"未知模式 {self.mode}")
            return False
        if signal_price <= 0:
            self._reject(symbol, "bad_price", f"信号价 {signal_price}")
            return False
        pos = self.positions.get(symbol)
        if pos:
            return self._add(symbol, pos, detail, signal_price)

        reason = self._episode_guards(symbol)
        if reason:
            self._reject(symbol, "gate", reason)
            return False
        if len(self.positions) >= int(self.trade.get("max_positions", 1) or 1):
            self._reject(symbol, "max_positions", f"已持有 {len(self.positions)} 个仓位")
            return False
        if not self.cooldown_ok(symbol):
            self._reject(symbol, "cooldown", "冷却中")
            return False
        guard = self._symbol_stop.get(symbol)
        if guard and bool(self.trade.get("post_stop_guard", True)):
            gpx = float(guard.get("price") or 0)
            blocked = (signal_price >= gpx) if side == "SHORT" else (signal_price <= gpx)
            if blocked:
                self._reject(symbol, "post_stop",
                             f"上次止损 {gpx:g}，价格还没回到止损价另一侧，不追")
                return False
            self._symbol_stop.pop(symbol, None)
        if self.mode == "live":
            try:
                amt = self._ex_client().position(symbol, self._pside(side))
            except Exception as e:  # noqa: BLE001
                self._reject(symbol, "position_api", f"查持仓失败: {e}")
                return False
            if abs(amt) > 1e-12:
                self._reject(symbol, "symbol_busy", f"账户已有持仓 {amt:g}，跳过")
                return False

        return self._enter(symbol, detail, signal_price, add=False)

    def _add(self, symbol: str, pos: dict, detail: dict, signal_price: float) -> bool:
        side = str(pos.get("side") or "SHORT").upper()
        window = float(self.trade.get("add_window_s", 0) or 0)
        max_adds = int(self.trade.get("max_adds", 0) or 0)
        if max_adds <= 0 or window <= 0:
            self._reject(symbol, "add_disabled", "配置里没开加仓")
            return False
        age = time.time() - pos["opened_at"]
        if age > window:
            self._reject(symbol, "add_window", f"距首笔 {age:.0f}s > {window:g}s")
            return False
        if len(pos["entries"]) > max_adds:
            self._reject(symbol, "max_adds", f"已加 {len(pos['entries']) - 1}/{max_adds} 次")
            return False
        reason = self._episode_guards(symbol)
        if reason:
            self._reject(symbol, "gate", reason)
            return False
        min_gain = float(self.trade.get("add_min_gain_pct", 0) or 0)
        last_px = pos["entries"][-1]["price"]
        too_low = signal_price < last_px * (1 + min_gain / 100.0)
        too_high = signal_price > last_px * (1 - min_gain / 100.0)
        if (side == "SHORT" and too_low) or (side == "LONG" and too_high):
            self._reject(symbol, "add_price",
                         f"信号价 {signal_price:g} 不在允许加仓方向（上一笔 {last_px:g}）")
            return False
        log(f"ADD?   {symbol:12s} 第 {len(pos['entries'])} 次加仓 "
            f"距今 {age:.0f}s 均价 {pos['avg']:g}")
        return self._enter(symbol, {**detail, "side": side}, signal_price, add=True)

    def _enter(self, symbol: str, detail: dict, signal_price: float, add: bool) -> bool:
        side = str(detail.get("side") or "SHORT").upper()
        dir_txt = "多" if side == "LONG" else "空"
        bid, ask = self._book(symbol)
        max_slip = float(self.trade.get("max_slippage_pct", 0.3) or 0.3)
        if bid > 0:
            slip = (signal_price - bid) / signal_price * 100
            if slip > max_slip:
                self._reject(symbol, "slippage",
                             f"买一 {bid:g} 比信号价低 {slip:.2f}% > {max_slip:g}%")
                return False

        _bn = self.basket.get("notional") if self.basket_on else None
        notional = float(_bn or self.trade.get("notional", 10.0) or 10.0)
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

        px = self._ioc_price(symbol, self._entry_side(side), bid, ask, signal_price)
        maker_entry = (self.mode == "live" and ask > 0
                       and str(self.trade.get("entry_mode", "taker")) == "maker")
        if maker_entry:
            # 做空挂在卖一价、做多挂在买一价（挂在会立即成交的一侧就等于吃单）
            base_px = ask if side == "SHORT" else bid
            px = max(self._tick_round(base_px, flt["tick"]), flt["tick"])
        self._cooldown[symbol] = time.time()
        log(f"{'ADD ' if add else 'OPEN'}?  {symbol:12s} {side:5s} qty={qty:g} px={px:g} "
            f"名义={qty * signal_price:.2f}U 模式={self.mode}"
            f"{' 被动挂单' if maker_entry else ''}")

        entry = px if bid > 0 else signal_price
        if self.mode == "live":
            try:
                self._ensure_leverage(symbol)
                if maker_entry:
                    resp = self._send(symbol, self._entry_side(side), qty, px, self._pside(side), tif="GTC")
                    oid = resp.get("orderId")
                    timeout = float(self.trade.get("entry_timeout_s", 5) or 5)
                    filled, fill_px = self._await_fill(symbol, oid, timeout, px, qty)
                    if filled <= 0:
                        self._reject(symbol, "maker_unfilled",
                                     f"被动挂单 {timeout:g}s 未成交，已撤单")
                        return False
                    entry = fill_px or px
                    qty = filled
                else:
                    resp = self._send(symbol, self._entry_side(side), qty, px, self._pside(side))
                    filled = float(resp.get("executedQty") or 0)
                    if filled <= 0:
                        self._reject(symbol, "unfilled", f"status={resp.get('status')}")
                        return False
                    entry = float(resp.get("avgPrice") or 0) or px
                    qty = filled
            except Exception as e:  # noqa: BLE001
                self._reject(symbol, "order_failed", str(e)[:160])
                self._notify(f"⚠️ 策略{'加仓' if add else '开仓'}失败 {symbol}\n{str(e)[:150]}")
                return False

        pos = self.positions.get(symbol)
        if pos is None:
            pos = {"symbol": symbol, "mode": self.mode, "entries": [], "qty": 0.0,
                   "avg": entry, "opened_at": time.time(), "peak": 0.0,
                   "trail_on": False, "trail_level": 0.0, "tp_done": [False, False],
                   "realized": 0.0, "parts": 0, "detail": detail,
                   "side": side, "last_add_pnl": 0.0}
            self.positions[symbol] = pos
        pos["entries"].append({"qty": qty, "price": entry, "ts": time.time()})
        pos["qty"] = sum(e["qty"] for e in pos["entries"])
        pos["avg"] = sum(e["qty"] * e["price"] for e in pos["entries"]) / pos["qty"]
        if (self.mode == "live" and not self.basket_on
                and str(self.trade.get("tp_mode", "taker")) == "maker"):
            self._place_tp(symbol, pos)

        if add:
            self.stats["adds"] += 1
            self._record("add", symbol=symbol, qty=qty, price=entry,
                         avg=round(pos["avg"], 8), total_qty=pos["qty"],
                         notional=round(pos["qty"] * pos["avg"], 4),
                         entries=len(pos["entries"]), mode=self.mode, signal=detail)
            log(f"ADD    {symbol:12s} {side:5s} {qty:g} @ {entry:g} "
                f"→ 共 {pos['qty']:g} 均价 {pos['avg']:g}")
            self._notify(f"➕ 策略加{dir_txt} {symbol}\n{qty:g} @ {entry:g}\n"
                         f"共 {pos['qty']:g} 均价 {pos['avg']:g}")
        else:
            self.stats["opens"] += 1
            self._record("open", symbol=symbol, qty=qty, entry=entry,
                         notional=round(qty * entry, 4), mode=self.mode, signal=detail)
            log(f"OPEN   {symbol:12s} {side:5s} {qty:g} @ {entry:g} ({qty * entry:.2f}U) "
                f"SL{self.trade.get('sl_pct')}% 阶梯 {ladder_of(self.trade)} "
                f"追踪 {self.trade.get('trail_activate_pct')}/"
                f"{self.trade.get('trail_gap_pct')}")
            tr_txt = f"TR {detail['tr_ratio']}x  " if detail.get("tr_ratio") else ""
            if detail.get("chg_1h") is not None:
                head = (f"🔻 策略开{dir_txt} {symbol}\n{qty:g} @ {entry:g}  {qty * entry:.1f}U\n"
                        f"1h {detail['chg_1h']:+.1f}%  15m {detail['chg_15m']:+.1f}%")
            else:
                head = (f"🔻 策略开{dir_txt} {symbol}\n{qty:g} @ {entry:g}  {qty * entry:.1f}U\n"
                        f"放量 {detail.get('vol_ratio')}x  {tr_txt}"
                        f"上影 {(detail.get('upper_ratio') or 0) * 100:.0f}%")
            self._notify(head)
        self._save_state()
        if self.basket_on:
            self._place_protect(symbol, pos)
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
              ps: str, reduce_only: bool = False, market: bool = False,
              tif: str = "IOC") -> dict:
        ex = self._ex_client()
        hedge = self._is_hedge()
        if market:
            return ex.order(symbol, side, qty, order_type="MARKET",
                            position_side=ps if hedge else None,
                            reduce_only=reduce_only and not hedge)
        return ex.order(symbol, side, qty, order_type="LIMIT", price=px,
                        time_in_force=tif,
                        position_side=ps if hedge else None,
                        reduce_only=reduce_only and not hedge)

    # ------------------------------------------------------- maker 挂单管理
    def _await_fill(self, symbol: str, order_id, timeout_s: float,
                    fallback_px: float, fallback_qty: float) -> tuple[float, float]:
        """等被动挂单成交；超时撤单并把已成交的部分也算回来。"""
        deadline = time.time() + max(timeout_s, 0.5)
        while time.time() < deadline:
            time.sleep(0.3)
            st = self._order_status(symbol, order_id)
            if st == "FILLED":
                return self._filled_of(symbol, order_id) or (fallback_qty, fallback_px)
            if st in ("CANCELED", "EXPIRED", "REJECTED"):
                return 0.0, 0.0
        try:
            self._ex_client().cancel(symbol, order_id)
        except Exception:  # noqa: BLE001
            pass
        filled = self._filled_of(symbol, order_id)
        if filled and filled[0] > 0:
            return filled
        # 兜底：撤单与成交存在竞态，订单状态可能查不到，但仓位已经开了
        try:
            for r in self._ex_client().positions():
                if r.get("symbol") == symbol:
                    amt = abs(float(r.get("positionAmt") or 0))
                    if amt > 0:
                        px = float(r.get("entryPrice") or 0) or fallback_px
                        log(f"⚠ {symbol} 撤单时已成交 {amt:g} @ {px:g}，按成交处理")
                        return amt, px
        except Exception:  # noqa: BLE001
            pass
        return 0.0, 0.0

    def _order_status(self, symbol: str, order_id) -> str:
        if not order_id:
            return ""
        try:
            o = self._ex_client().query_order(symbol, int(order_id))
            return str(o.get("status") or "")
        except Exception:  # noqa: BLE001
            return ""

    def _filled_of(self, symbol: str, order_id) -> tuple[float, float] | None:
        try:
            o = self._ex_client().query_order(symbol, int(order_id))
        except Exception:  # noqa: BLE001
            return None
        qty = float(o.get("executedQty") or 0)
        if qty <= 0:
            return None
        px = float(o.get("avgPrice") or 0) or float(o.get("price") or 0)
        return qty, px

    def _place_tp(self, symbol: str, pos: dict) -> None:
        """止盈挂被动限价单：价格到不了就一直挂着，到点或止损时撤掉。"""
        tp = float(self.trade.get("tp1_pct", 0) or 0)
        if tp <= 0 or pos.get("tp_order") or pos.get("qty", 0) <= 0:
            return
        side = str(pos.get("side") or "SHORT").upper()
        px = self._tick_round(self._tp_price(side, pos["avg"], tp), self._flt(symbol)["tick"])
        try:
            r = self._send(symbol, self._close_side(side), pos["qty"], px,
                           self._pside(side), reduce_only=True, tif="GTC")
            pos["tp_order"] = r.get("orderId")
            pos["tp_price"] = px
            log(f"TP挂单 {symbol:12s} {self._close_side(side)} {pos['qty']:g} @ {px:g}（maker 等待成交）")
        except Exception as e:  # noqa: BLE001
            log(f"⚠ 止盈挂单失败 {symbol}: {e}（该仓改走 taker 止盈）")
            pos["tp_order"] = None

    def _cancel_tp(self, symbol: str, pos: dict) -> None:
        oid = pos.pop("tp_order", None)
        pos.pop("tp_price", None)
        if not oid:
            return
        try:
            self._ex_client().cancel(symbol, int(oid))
        except Exception:  # noqa: BLE001
            pass

    def _settle_maker_tp(self, symbol: str, pos: dict) -> None:
        """限价止盈单成交：落账并收尾。"""
        qty = pos["qty"]
        exit_price = float(pos.get("tp_price") or 0) or pos["avg"]
        pnl = self._pnl_u(pos.get("side", "SHORT"), pos["avg"], exit_price, qty)
        pos["qty"] = 0.0
        pos["realized"] += pnl
        pos["parts"] += 1
        pos["tp_order"] = None
        self.realized_today += pnl
        self.stats["pnl"] += pnl
        self._record("close_partial", symbol=symbol, reason="take_profit_maker",
                     qty=qty, entry=round(pos["avg"], 8), exit=exit_price,
                     pnl=round(pnl, 4),
                     pnl_pct=round(self._pnl_pct(pos.get("side", "SHORT"), pos["avg"], exit_price), 3),
                     remaining=0.0, mode=pos["mode"])
        log(f"PART   {symbol:12s} tp(maker) {qty:g} @ {exit_price:g} "
            f"pnl={pnl:+.3f}U 今日 {self.realized_today:+.2f}U")
        self._finalize(symbol, pos, "take_profit_maker")

    # ------------------------------------------------------------- 持仓管理
    def manage(self, prices: dict[str, float]) -> None:
        if not self.positions:
            return
        self._reconcile()
        if not self.positions:
            return
        sl = float(self.trade.get("sl_pct", 0.5) or 0.5)
        max_hold = float(self.trade.get("max_hold_s", 180) or 180)
        trail_act = float(self.trade.get("trail_activate_pct", 0) or 0)
        trail_gap = float(self.trade.get("trail_gap_pct", 0) or 0)
        ladder = ladder_of(self.trade)
        now = time.time()
        for symbol, pos in list(self.positions.items()):
            if pos.get("tp_order"):
                st = self._order_status(symbol, pos["tp_order"])
                if st == "FILLED":
                    self._settle_maker_tp(symbol, pos)
                    continue
                if st in ("CANCELED", "EXPIRED", "REJECTED"):
                    pos["tp_order"] = None
                    pos.pop("tp_price", None)
            if self._past_cutoff():
                self._close_partial(symbol, "cutoff",
                                    prices.get(symbol) or pos["avg"], 1.0)
                continue
            mark = float(prices.get(symbol) or 0)
            if mark <= 0:
                continue
            if self.basket_on:
                self._manage_basket(symbol, pos, mark, now)
                continue
            pnl = self._pnl_pct(pos.get("side", "SHORT"), pos["avg"], mark)
            pos["peak"] = max(pos["peak"], pnl)

            if pnl <= -sl:
                self._close_partial(symbol, "stop_loss", mark, 1.0)
                continue

            acted = False
            for idx, (level, frac) in enumerate(ladder):
                if pos["tp_done"][idx] or pnl < level:
                    continue
                if self._close_partial(symbol, f"tp{idx + 1}", mark, frac):
                    pos["tp_done"][idx] = True
                acted = True
                break
            if acted:
                continue

            if trail_act > 0:
                if not pos["trail_on"] and pnl >= trail_act:
                    pos["trail_on"] = True
                    pos["trail_level"] = pos["peak"] - trail_gap
                    log(f"TRAIL  {symbol:12s} 追踪止盈开启：峰值 {pos['peak']:.2f}% "
                        f"回撤 {trail_gap:g}pp → 触发线 {pos['trail_level']:.2f}%")
                if pos["trail_on"]:
                    # 峰值持续上移，触发线跟着上移（只上不下）
                    pos["trail_level"] = max(pos["trail_level"], pos["peak"] - trail_gap)
                    if pnl <= pos["trail_level"]:
                        self._close_partial(symbol, "trail", mark, 1.0)
                        continue

            if now - pos["opened_at"] >= max_hold:
                self._close_partial(symbol, "timeout", mark, 1.0)

    # ------------------------------------------------------- 均价摊平（复刻）
    def _save_state(self) -> None:
        """把策略持仓写到磁盘，重启后能接管（避免重启变成裸仓）。"""
        if self.mode != "live":
            return
        try:
            STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            data = {s: {"qty": p["qty"], "avg": p["avg"], "side": p.get("side", "SHORT"),
                        "opened_at": p["opened_at"], "entries": p["entries"]}
                    for s, p in self.positions.items()}
            tmp = STATE_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(STATE_PATH)
        except Exception:  # noqa: BLE001
            pass

    def restore_open(self) -> None:
        """启动时接管上次未平的策略持仓（先与交易所对账）。"""
        if self.mode != "live" or not STATE_PATH.exists():
            return
        try:
            saved = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return
        try:
            live = {r["symbol"]: r for r in self._ex_client().positions()}
        except Exception as e:  # noqa: BLE001
            log(f"⚠ 恢复持仓失败（查交易所）: {e}")
            return
        for sym, p in saved.items():
            r = live.get(sym)
            amt = abs(float(r.get("positionAmt") or 0)) if r else 0
            if amt <= 0:
                log(f"恢复：{sym} 交易所已无持仓，跳过")
                continue
            entry = float(r.get("entryPrice") or 0) or float(p.get("avg") or 0)
            qty = amt
            side = "SHORT" if float(r.get("positionAmt") or 0) < 0 else "LONG"
            saved_entries = [e for e in (p.get("entries") or [])
                             if float(e.get("qty") or 0) > 0
                             and float(e.get("price") or 0) > 0]
            if saved_entries and abs(sum(float(e["qty"]) for e in saved_entries) - qty) \
                    <= max(1e-9, qty * 0.01):
                entries = [{"qty": float(e["qty"]), "price": float(e["price"]),
                            "ts": float(e.get("ts") or time.time())}
                           for e in saved_entries]
                avg = sum(e["qty"] * e["price"] for e in entries) / sum(
                    e["qty"] for e in entries)
            else:
                entries = [{"qty": qty, "price": entry, "ts": time.time()}]
                avg = entry
            self.positions[sym] = {
                "symbol": sym, "mode": self.mode, "side": side,
                "entries": entries,
                "qty": qty, "avg": avg,
                "opened_at": float(p.get("opened_at") or time.time()),
                "peak": 0.0, "trail_on": False, "trail_level": 0.0,
                "tp_done": [False, False], "realized": 0.0, "parts": 0,
                "detail": {}, "last_add_pnl": 0.0,
            }
            held = (time.time() - self.positions[sym]["opened_at"]) / 60
            self._place_protect(sym, self.positions[sym])
            log(f"⚠ 接管未平持仓 {sym} {qty:g} @ {entry:g}（已持有 {held:.0f} 分钟）")
            self._notify(f"⚠️ 重启接管持仓\n{sym} 空 {qty:g} @ {entry:g}\n"
                         f"已持有 {held:.0f} 分钟，继续执行止盈/止损/超时")
        self._save_state()

    def _settle_external(self, symbol: str) -> None:
        """仓位已被交易所条件单平掉：清状态、回填真实盈亏。"""
        pos = self.positions.pop(symbol, None)
        if not pos:
            return
        self._cancel_protect(symbol, pos)
        pnl = None
        try:
            rows = self._ex_client().user_trades(
                symbol, int(float(pos.get("opened_at") or time.time()) * 1000))
            pnl = (sum(float(r.get("realizedPnl") or 0)
                       - float(r.get("commission") or 0) for r in rows)
                   - float(pos.get("realized") or 0))
        except Exception:  # noqa: BLE001
            pnl = None
        if pnl is not None:
            self.realized_today += pnl
            self.stats["pnl"] += pnl
        self.stats["closes"] += 1
        if (pnl or 0) > 0:
            self.stats["wins"] += 1
        if pnl is None:
            reason, note = "external", "交易所已无持仓"
        elif pnl > 0:
            reason, note = "exchange_tp", "交易所止盈条件单成交"
        else:
            reason, note = "exchange_sl", "交易所止损条件单成交"
            hard = float(self.basket.get("hard_stop_pct", 0) or 0)
            self._symbol_stop[symbol] = {
                "price": self._sl_price(str(pos.get("side") or "SHORT"),
                                        float(pos["avg"]), hard),
                "ts": time.time(),
            }
        self._record("close", symbol=symbol, reason=reason, qty=0.0,
                     pnl_total=round(pnl, 6) if pnl is not None else 0.0,
                     entries=len(pos.get("entries") or []), notes=note)
        log(f"CLOSE  {symbol:12s} {reason:12s} "
            f"{('%.3f' % pnl) if pnl is not None else '?'}U（交易所已平）")
        self._notify(f"✅ {symbol} 由交易所条件单平仓\n"
                     f"{reason} 净盈亏 {pnl:+.3f}U" if pnl is not None
                     else f"✅ {symbol} 交易所已平，状态已清理")
        self._save_state()

    def sweep_orphan_algo(self) -> None:
        """清理没有对应持仓的残留条件单（手动平仓/进程停掉时留下的）。"""
        if self.mode != "live":
            return
        try:
            ex = self._ex_client()
            rows = ex.open_algo_orders()
            live = {p["symbol"]: abs(float(p.get("positionAmt") or 0))
                    for p in ex.positions()}
        except Exception as e:  # noqa: BLE001
            log(f"清理残留条件单失败: {e}")
            return
        n = 0
        for r in rows:
            sym = str(r.get("symbol") or "")
            if not sym or live.get(sym, 0) > 0 or sym in self.positions:
                continue
            try:
                ex.cancel_algo(sym, r.get("algoId"))
                n += 1
            except Exception:  # noqa: BLE001
                pass
        if n:
            log(f"清理残留条件单 {n} 张（无对应持仓）")

    def _reconcile(self) -> None:
        """交易所已无持仓（手动平/强平）时，清理本地状态，避免幽灵仓位。"""
        if self.mode != "live" or not self.positions:
            return
        now = time.time()
        if now - float(getattr(self, "_last_recon", 0) or 0) < 10:
            return
        self._last_recon = now
        try:
            live = {r["symbol"]: r for r in self._ex_client().positions()}
        except Exception:  # noqa: BLE001
            return
        for sym in list(self.positions):
            r = live.get(sym)
            amt = abs(float(r.get("positionAmt") or 0)) if r else 0
            if amt > 0:
                pos = self.positions[sym]
                pos.pop("_miss", None)
                if abs(amt - float(pos["qty"])) > max(1e-9, float(pos["qty"]) * 0.005):
                    pos["_qty_miss"] = int(pos.get("_qty_miss", 0)) + 1
                    if pos["_qty_miss"] >= 2:
                        px = float(r.get("entryPrice") or 0) or pos["avg"]
                        log(f"⚠ {sym} 交易所持仓 {amt:g} 与本地 {pos['qty']:g} 不一致，"
                            f"按交易所校正")
                        pos["qty"] = amt
                        if px > 0:
                            pos["avg"] = px
                        pos.pop("_qty_miss", None)
                        self._place_protect(sym, pos)
                else:
                    pos.pop("_qty_miss", None)
                continue
            pos = self.positions[sym]
            pos["_miss"] = int(pos.get("_miss", 0)) + 1
            if pos["_miss"] < 2:      # 刚成交时仓位接口可能延迟，再查一次
                log(f"⚠ {sym} 暂时查不到持仓（{pos['_miss']}/2），10 秒后复查")
                continue
            self._cancel_protect(sym, pos)
            # 用真实成交回填盈亏：交易所条件单成交时引擎只看到「仓位没了」
            pnl = None
            try:
                rows = self._ex_client().user_trades(
                    sym, int(float(pos.get("opened_at") or now) * 1000))
                pnl = (sum(float(r.get("realizedPnl") or 0)
                           - float(r.get("commission") or 0) for r in rows)
                       - float(pos.get("realized") or 0))
            except Exception:  # noqa: BLE001
                pnl = None
            if pnl is None:
                reason, note = "external", "交易所已无持仓（未能取回成交明细）"
            elif pnl > 0:
                reason, note = "exchange_tp", "交易所止盈条件单成交"
            elif pnl < 0:
                reason, note = "exchange_sl", "交易所止损条件单成交"
            else:
                reason, note = "external", "交易所已无持仓"
            self.positions.pop(sym)
            self.stats["closes"] += 1
            if (pnl or 0) > 0:
                self.stats["wins"] += 1
            if pnl:
                self.realized_today += pnl
            self._record("close", symbol=sym, reason=reason, qty=0.0,
                         pnl_total=round(pnl, 6) if pnl is not None else 0.0,
                         entries=len(pos.get("entries") or []), notes=note)
            log(f"⚠ {sym} {note}：{('%.3f' % pnl) if pnl is not None else '?'}U，清理状态")
            self._notify(f"⚠️ {sym} 已被交易所平掉（{reason}）\n"
                         f"净盈亏 {pnl:+.3f}U" if pnl is not None
                         else f"⚠️ {sym} 交易所以无持仓，已清理状态")
        self._save_state()

    def _close_partial(self, symbol: str, reason: str, mark: float,
                       frac: float, reprotect: bool = True) -> bool:
        pos = self.positions.get(symbol)
        if not pos:
            return False
        if pos.get("tp_order"):
            self._cancel_tp(symbol, pos)     # 主动平仓前先撤掉止盈挂单
        self._cancel_protect(symbol, pos)
        remaining = float(pos["qty"])
        flt = self._flt(symbol)
        qty = remaining if frac >= 0.999 else self._floor_step(remaining * frac, flt["step"])
        if qty <= 0:
            qty = remaining
        if remaining - qty < flt["step"] * 0.5:
            qty = remaining                      # 不留碎单

        side = str(pos.get("side") or "SHORT").upper()
        log(f"CLOSE? {symbol:12s} {reason:8s} qty={qty:g}/{remaining:g} mark={mark:g} "
            f"pnl≈{self._pnl_pct(side, pos['avg'], mark):+.2f}%")
        exit_price = mark
        if pos["mode"] == "live":
            bid, ask = self._book(symbol)
            px = self._ioc_price(symbol, self._close_side(side), bid, ask, mark)
            try:
                resp = self._send(symbol, self._close_side(side), qty, px, self._pside(side), reduce_only=True)
                filled = float(resp.get("executedQty") or 0)
                if filled <= 0:
                    log(f"⚠ 平仓 IOC 未成交 {symbol}，转市价")
                    resp = self._send(symbol, self._close_side(side), qty, 0,
                                      self._pside(side), reduce_only=True,
                                      market=True)
                    filled = float(resp.get("executedQty") or 0)
                if filled <= 0:
                    self._reject(symbol, "close_failed",
                                 f"平仓未成交 status={resp.get('status')}")
                    self._notify(f"🚨 策略平仓失败 {symbol}，请手动处理")
                    if self.basket_on and reprotect:
                        self._place_protect(symbol, pos)   # 恢复保护单
                    return False
                exit_price = float(resp.get("avgPrice") or 0) or px
                qty = filled
            except Exception as e:  # noqa: BLE001
                msg = str(e)
                if "-2022" in msg or "ReduceOnly" in msg:
                    # 交易所条件单先一步平掉了，引擎再平就会被 reduceOnly 拒
                    log(f"⚠ {symbol} 平仓被拒（仓位已被条件单平掉），转入对账")
                    self._settle_external(symbol)
                    return True
                self._reject(symbol, "close_failed", msg[:160])
                self._notify(f"🚨 策略平仓失败 {symbol}\n{msg[:150]}")
                if self.basket_on and reprotect:
                    self._place_protect(symbol, pos)       # 恢复保护单
                return False

        if reason in ("stop_loss", "basket_stop"):
            self._symbol_stop[symbol] = {"price": exit_price, "ts": time.time()}

        pnl = self._pnl_u(pos.get("side", "SHORT"), pos["avg"], exit_price, qty)
        pos["qty"] = max(0.0, remaining - qty)
        pos["realized"] += pnl
        pos["parts"] += 1
        self.realized_today += pnl
        self.stats["pnl"] += pnl

        self._record("close_partial", symbol=symbol, reason=reason, qty=qty,
                     entry=round(pos["avg"], 8), exit=exit_price,
                     pnl=round(pnl, 4),
                     pnl_pct=round(self._pnl_pct(pos.get("side", "SHORT"), pos["avg"], exit_price), 3),
                     remaining=round(pos["qty"], 8), mode=pos["mode"])
        log(f"PART   {symbol:12s} {reason:8s} {qty:g} @ {exit_price:g} "
            f"pnl={pnl:+.3f}U 剩余 {pos['qty']:g} 今日 {self.realized_today:+.2f}U")

        if pos["qty"] <= 1e-12:
            self._finalize(symbol, pos, reason)
        elif self.basket_on and reprotect:
            self._place_protect(symbol, pos)               # 剩余仓位重新挂保护
        return True

    def _finalize(self, symbol: str, pos: dict, reason: str) -> None:
        held = time.time() - pos["opened_at"]
        total = pos["realized"]
        self._cancel_protect(symbol, pos)
        self.positions.pop(symbol, None)
        self._save_state()
        self._cooldown[symbol] = time.time()
        self.stats["closes"] += 1
        if total > 0:
            self.stats["wins"] += 1
        # 每一笔平仓都已按 close_partial 记账，这里 pnl 恒为 0，避免重启后重复累加
        self._record("close", symbol=symbol, reason=reason, pnl=0.0,
                     pnl_total=round(total, 4), parts=pos["parts"],
                     entries=len(pos["entries"]), avg=round(pos["avg"], 8),
                     held_s=round(held, 1), peak_pct=round(pos["peak"], 3),
                     mode=pos["mode"])
        log(f"CLOSE  {symbol:12s} {reason:10s} 合计 pnl={total:+.3f}U "
            f"({held:.0f}s, {pos['parts']} 笔) 今日 {self.realized_today:+.2f}U")
        self._notify(f"{'✅' if total >= 0 else '❌'} 策略平空 {symbol}（{reason}）\n"
                     f"合计 {total:+.3f}U（{held:.0f}s，{pos['parts']} 笔）")
        self._check_halt()

    def _check_halt(self) -> None:
        max_loss = float(self.trade.get("max_daily_loss", 0) or 0)
        if max_loss > 0 and self.realized_today <= -max_loss and not self._halt:
            self._halt = f"当日亏损 {self.realized_today:.2f}U 达到上限 {max_loss:g}U"
            log(f"🛑 {self._halt}，停止开仓")
            self._notify(f"🛑 策略熔断\n{self._halt}\n不再开新仓，已有仓位照常管理")

    def close_all(self, prices: dict[str, float], reason: str = "shutdown") -> None:
        for symbol in list(self.positions):
            self._close_partial(symbol, reason,
                                prices.get(symbol) or self.positions[symbol]["avg"], 1.0)

    def summary(self) -> str:
        s = self.stats
        wr = (s["wins"] / s["closes"] * 100) if s["closes"] else 0.0
        return (f"信号 {s['signals']}  开仓 {s['opens']}  加仓 {s['adds']}  "
                f"平仓 {s['closes']}  胜率 {wr:.0f}%  "
                f"今日 {self.realized_today:+.2f}U  拒绝 {s['rejects']}")
