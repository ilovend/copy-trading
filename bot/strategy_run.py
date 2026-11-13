#!/usr/bin/env python3
"""自研策略运行器：扫涨幅榜 → 订阅 K 线 → 检测信号 → 做空 → 快速平仓。

用法：
    python bot/strategy_run.py              # 按 gui/strategy_config.json 跑
    python bot/strategy_run.py --scan       # 只打印当前候选币，退出
    python bot/strategy_run.py --probe      # 拉最近 K 线，看现在有没有信号，不下单
    python bot/strategy_run.py --mode live  # 覆盖模式（默认读配置）

输出：
    bot/data/strategy-YYYYMMDD.jsonl   信号 / 开仓 / 平仓 / 拒绝
    bot/data/strategy-raw-YYYYMMDD.jsonl  原始 WS（排障，默认关）
"""
from __future__ import annotations

import argparse
import asyncio
import json
import queue
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bot"))

import websockets  # noqa: E402

import strategy_volume_pinbar as SP  # noqa: E402
import strategy_fade as SF  # noqa: E402
from strategy_config import CONFIG_PATH, load as load_cfg  # noqa: E402
from strategy_engine import (INTERVAL_S, StrategyEngine, ladder_of, log,  # noqa: E402
                             set_log_file, simulate_exit)

STATUS_PATH = ROOT / "bot" / "data" / "strategy-status.json"

FAPI = "https://fapi.binance.com"
# 币安 2026 起把合约行情 WS 迁到 /market/stream；老的 /ws、/stream 仍能握手但不推数据
WS = "wss://fstream.binance.com/market/stream"
BARS_KEEP = 1500       # 25 小时 1m，够聚合出 HTF 结构
LOCK_PORT = 47613      # 单实例锁：端口被占说明已有策略在跑
_LOCK_SOCK = None


def acquire_single_instance() -> None:
    """防止重复启动（GUI 连点、GUI + 命令行并存）导致双份下单。"""
    global _LOCK_SOCK
    import socket
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", LOCK_PORT))
        s.listen(1)
    except OSError:
        sys.exit(f"已有策略实例在运行（本地端口 {LOCK_PORT} 被占用）。"
                 f"先停止旧进程再启动。")
    _LOCK_SOCK = s


def scan_universe(cfg: dict) -> list[str]:
    """候选池：默认按 24h 涨幅排序取前 N；剥头皮预设可按成交额排序。"""
    from net import http_json
    u = cfg.get("universe") or {}
    top_n = int(u.get("top_n", 15) or 15)
    min_chg = float(u.get("min_change_pct", 3.0) or 0)
    min_qv = float(u.get("min_quote_volume", 50e6) or 0)
    sort_by = str(u.get("sort_by") or "change").lower()
    rows = http_json(f"{FAPI}/fapi/v1/ticker/24hr", timeout=20)
    cand = []
    for r in rows:
        sym = str(r.get("symbol") or "")
        if not sym.endswith("USDT") or "_" in sym:
            continue
        try:
            chg = float(r.get("priceChangePercent") or 0)
            qv = float(r.get("quoteVolume") or 0)
            last = float(r.get("lastPrice") or 0)
        except (TypeError, ValueError):
            continue
        if last <= 0 or qv < min_qv or chg < min_chg:
            continue
        cand.append((chg, qv, sym))
    if sort_by == "volume":
        cand.sort(key=lambda x: x[1], reverse=True)
    else:
        cand.sort(key=lambda x: x[0], reverse=True)
    return [sym for _, _, sym in cand[:top_n]]


def fetch_bars(symbol: str, interval: str, limit: int = BARS_KEEP) -> list[dict]:
    from net import http_json
    rows = http_json(f"{FAPI}/fapi/v1/klines?symbol={symbol}&interval={interval}"
                     f"&limit={limit}", timeout=15)
    out = []
    for k in rows:
        out.append({"t": int(k[0]), "o": float(k[1]), "h": float(k[2]),
                    "l": float(k[3]), "c": float(k[4]), "v": float(k[5]),
                    "closed": True})
    return out


class Runner:
    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.engine = StrategyEngine(cfg)
        self.interval = cfg.get("interval", "1m")
        self.universe: list[str] = []
        self.bars: dict[str, deque] = {}
        self.prices: dict[str, float] = {}
        self.signals: queue.Queue = queue.Queue(maxsize=100)
        self._stop = threading.Event()
        self._reconnect = threading.Event()
        self._last_summary = 0.0
        self._last_beat = time.time()
        self._last_msg = time.time()
        self._cutoff_logged = False
        self._last_status = 0.0
        self._rest_enabled = False
        self._last_sig_t: dict[str, int] = {}
        self.setup_cfg = cfg.get("setup") or {}
        self.fade_cfg = cfg.get("fade") or {}
        self.stages: dict[str, SP.TwoStage] = {}

    # ------------------------------------------------------------- 数据准备
    def prefill(self, symbols: list[str]) -> None:
        for sym in symbols:
            if sym in self.bars:
                continue
            try:
                bars = fetch_bars(sym, self.interval)
                # 丢掉未收盘的当前根，避免它收盘时因时间戳相同被跳过
                if bars:
                    step_ms = INTERVAL_S.get(self.interval, 60) * 1000
                    if int(bars[-1]["t"]) + step_ms > time.time() * 1000:
                        bars = bars[:-1]
                self.bars[sym] = deque(bars, maxlen=BARS_KEEP)
            except Exception as e:  # noqa: BLE001
                log(f"预取 {sym} 失败: {e}")

    def _scan_and_prefill(self) -> None:
        new = scan_universe(self.cfg)
        added = [s for s in new if s not in self.bars]
        if set(new) != set(self.universe):
            log(f"候选池更新（{len(new)}）：{', '.join(new)}")
            self.universe = new
            self._reconnect.set()
        if added:
            self.prefill(added)

    # ------------------------------------------------------------- 信号线程
    def _signal_loop(self) -> None:
        while not self._stop.is_set():
            try:
                symbol, bar = self.signals.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                bars = list(self.bars.get(symbol) or [])
                if not bars or bars[-1].get("t") != bar["t"]:
                    continue
                sig = None
                if self.fade_cfg.get("enabled"):
                    # 已有仓位时不重复开首仓（加仓由引擎按逆行触发）
                    if symbol not in self.engine.positions:
                        sig = SF.fade_signal(bars, {**self.fade_cfg, "direction": self.cfg.get("direction", "short")})
                elif self.setup_cfg.get("enabled"):
                    stage = self.stages.get(symbol)
                    if stage is None:
                        stage = SP.TwoStage(self.setup_cfg)
                        self.stages[symbol] = stage
                    sig = stage.on_bar(bars)
                else:
                    res = SP.evaluate(bars, self.cfg.get("signal") or {})
                    if res["hit"]:
                        sig = res["detail"]
                if not sig:
                    continue
                if sig.get("chg_1h") is not None:
                    log(f"信号   {symbol:12s} 1h {sig['chg_1h']:+.1f}% "
                        f"15m {sig['chg_15m']:+.1f}% → {sig.get('side', 'SHORT')}")
                else:
                    setup = sig.get("setup") or {}
                    log(f"信号   {symbol:12s} 放量 {sig.get('vol_ratio')}x "
                        f"上影 {(sig.get('upper_ratio') or 0) * 100:.0f}% "
                        f"预备 {setup.get('bars', '-')} 根HTF 位置 {setup.get('pos', '-')} "
                        f"偏离 {setup.get('ext_atr', '-')}ATR")
                self.engine.open_short(symbol, sig, bar["c"])
            except Exception as e:  # noqa: BLE001
                log(f"信号处理异常: {e}")

    # ------------------------------------------------------------- 持仓线程
    def _manage_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.engine.manage(dict(self.prices))
                if self.engine._past_cutoff():
                    if self.engine.positions:
                        log("到收盘时间，强平全部策略仓位")
                        self.engine.close_all(dict(self.prices), "cutoff")
                    if not self._cutoff_logged:
                        self._cutoff_logged = True
                        log("已收盘：今晚不再开仓，明天 00:00 后自动恢复")
                        try:
                            from notify import tg_send
                            tg_send("🌙 策略收盘：持仓已平，今晚不再开仓，明天自动恢复")
                        except Exception:  # noqa: BLE001
                            pass
                else:
                    self._cutoff_logged = False
                now = time.time()
                hb = float(self.cfg.get("heartbeat_min", 0) or 0)
                if hb > 0 and now - self._last_beat >= hb * 60:
                    self._last_beat = now
                    try:
                        from notify import tg_send
                        tg_send(f"💓 策略存活 {datetime.now():%H:%M}\n"
                                f"候选 {len(self.universe)} · 持仓 {len(self.engine.positions)} · "
                                f"今日 {self.engine.realized_today:+.2f}U\n"
                                f"{self.engine.summary()}")
                    except Exception:  # noqa: BLE001
                        pass
                if now - self._last_status >= 30:
                    self._last_status = now
                    self._write_status(now)
                    if now - getattr(self, "_last_status_log", 0) >= 600:
                        self._last_status_log = now
                        log(f"状态   候选 {len(self.universe)} · bars {len(self.bars)} · "
                            f"WS {now - self._last_msg:.0f}s 前 · 队列 {self.signals.qsize()} · "
                            f"信号 {self.engine.stats['signals']} · "
                            f"拒绝 {self.engine.stats['rejects']}")
                if now - self._last_summary >= 300:
                    self._last_summary = now
                    if self.engine.stats["signals"] or self.engine.positions:
                        log(f"统计   {self.engine.summary()}")
            except Exception as e:  # noqa: BLE001
                log(f"持仓管理异常: {e}")
            time.sleep(1.0)

    def _write_status(self, now: float) -> None:
        latest = 0
        for b in self.bars.values():
            if b:
                latest = max(latest, int(b[-1]["t"]))
        payload = {
            "ts": int(now), "time": datetime.now().strftime("%H:%M:%S"),
            "mode": self.engine.mode, "universe": len(self.universe),
            "bars": len(self.bars), "positions": len(self.engine.positions),
            "queue": self.signals.qsize(),
            "ws_age_s": round(now - self._last_msg, 1),
            "bar_age_s": round(now - latest / 1000, 1) if latest else None,
            "stats": dict(self.engine.stats),
            "realized_today": round(self.engine.realized_today, 3),
        }
        try:
            tmp = STATUS_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(STATUS_PATH)
        except Exception:  # noqa: BLE001
            pass

    def _feed_bar(self, symbol: str, bar: dict) -> None:
        """只把「新收盘」的 K 线送进信号队列，避免同一根重复触发。"""
        t = int(bar["t"])
        if t <= int(self._last_sig_t.get(symbol, 0)):
            return
        self._last_sig_t[symbol] = t
        try:
            self.signals.put_nowait((symbol, bar))
        except queue.Full:
            log("信号队列已满，丢弃一根 K 线")

    # ------------------------------------------------------------- REST 兜底
    async def _rest_loop(self) -> None:
        """WS 收不到数据时（代理/网络问题），用 REST 轮询补 K 线与价格。"""
        from net import http_json
        step = INTERVAL_S.get(self.interval, 60) * 1000
        while not self._stop.is_set():
            try:
                prices = await asyncio.to_thread(
                    http_json, f"{FAPI}/fapi/v1/ticker/price", 10)
                if isinstance(prices, list):
                    for row in prices:
                        try:
                            self.prices[row["symbol"]] = float(row["price"])
                        except Exception:  # noqa: BLE001
                            continue
            except Exception as e:  # noqa: BLE001
                log(f"REST 价格轮询失败: {e}")
            for sym in list(self.universe):
                if self._stop.is_set():
                    return
                try:
                    rows = await asyncio.to_thread(
                        http_json,
                        f"{FAPI}/fapi/v1/klines?symbol={sym}"
                        f"&interval={self.interval}&limit=3", 10)
                except Exception:  # noqa: BLE001
                    continue
                if not isinstance(rows, list) or not rows:
                    continue
                try:
                    self.prices[sym] = float(rows[-1][4])
                except Exception:  # noqa: BLE001
                    pass
                dq = self.bars.get(sym)
                if dq is None:
                    dq = deque(maxlen=BARS_KEEP)
                    self.bars[sym] = dq
                last_t = int(dq[-1]["t"]) if dq else 0
                now_ms = time.time() * 1000
                for k in rows:
                    t = int(k[0])
                    if t + step > now_ms:
                        continue
                    bar = {"t": t, "o": float(k[1]), "h": float(k[2]),
                           "l": float(k[3]), "c": float(k[4]), "v": float(k[5]),
                           "closed": True}
                    if t == last_t and dq:
                        dq[-1] = bar          # 同一根从未收盘变收盘：就地更新
                        self._feed_bar(sym, bar)
                        continue
                    if t <= last_t:
                        continue
                    dq.append(bar)
                    last_t = t
                    self._feed_bar(sym, bar)
            await asyncio.sleep(float(self.cfg.get("rest_poll_s", 5) or 5))

    # ------------------------------------------------------------- WS
    def _handle(self, msg: dict) -> None:
        self._last_msg = time.time()
        data = msg.get("data", msg)
        if isinstance(data, list):
            for row in data:
                try:
                    self.prices[row["s"]] = float(row["p"])
                except Exception:  # noqa: BLE001
                    continue
            return
        if not isinstance(data, dict) or data.get("e") != "kline":
            return
        k = data.get("k") or {}
        if not k.get("x"):
            return
        symbol = k.get("s")
        if symbol not in self.bars:
            self.bars[symbol] = deque(maxlen=BARS_KEEP)
        bar = {"t": int(k["t"]), "o": float(k["o"]), "h": float(k["h"]),
               "l": float(k["l"]), "c": float(k["c"]), "v": float(k["v"]),
               "closed": True}
        dq = self.bars[symbol]
        if dq and int(dq[-1]["t"]) == bar["t"]:
            dq[-1] = bar
        else:
            dq.append(bar)
        self._feed_bar(symbol, bar)

    async def _stream_loop(self) -> None:
        backoff = 1
        silent_failures = 0
        while not self._stop.is_set():
            if not self.universe:
                await asyncio.sleep(1)
                continue
            if self._rest_enabled and silent_failures >= 3:
                log("WS 连续静默 3 次，判定当前网络不可用，改由 REST 轮询供数")
                while not self._stop.is_set():
                    await asyncio.sleep(30)
                return
            streams = [f"{s.lower()}@kline_{self.interval}" for s in self.universe]
            streams.append("!markPrice@arr@1s")
            url = f"{WS}?streams={'/'.join(streams)}"
            self._reconnect.clear()
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=20,
                                              max_queue=4096) as ws:
                    backoff = 1
                    log(f"WS 已连接：{len(self.universe)} 个候选 · {self.interval} · "
                        f"模式={self.engine.mode}")
                    while not self._stop.is_set() and not self._reconnect.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=2)
                        except asyncio.TimeoutError:
                            if time.time() - self._last_msg > 90:
                                silent_failures += 1
                                log(f"WS 静默 {time.time() - self._last_msg:.0f}s"
                                    f"（第 {silent_failures} 次），重连")
                                break
                            continue
                        try:
                            self._handle(json.loads(raw))
                        except Exception as e:  # noqa: BLE001
                            log(f"消息解析失败: {e}")
            except Exception as e:  # noqa: BLE001
                if self._stop.is_set():
                    break
                log(f"WS 断开: {type(e).__name__}: {e}  重连 {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def _universe_loop(self) -> None:
        while not self._stop.is_set():
            # 启动时 run() 已经扫过一次，先等一个周期再重扫，避免重复订阅
            refresh = float((self.cfg.get("universe") or {}).get("refresh_min", 5) or 5)
            await asyncio.sleep(max(30.0, refresh * 60))
            if self._stop.is_set():
                return
            try:
                await asyncio.to_thread(self._scan_and_prefill)
            except Exception as e:  # noqa: BLE001
                log(f"涨榜扫描失败: {e}")

    # ------------------------------------------------------------- 主流程
    async def run(self) -> None:
        from version import __version__
        trade = self.engine.trade
        log(f"自研策略 v{__version__} 启动：模式={self.engine.mode} "
            f"周期={self.interval} 单笔={trade.get('notional')}U "
            f"止损={trade.get('sl_pct')}% 阶梯={ladder_of(trade)} "
            f"追踪={trade.get('trail_activate_pct')}%/{trade.get('trail_gap_pct')}pp "
            f"加仓={trade.get('max_adds')}次/{trade.get('add_window_s')}s "
            f"最长持仓={trade.get('max_hold_s')}s")
        await asyncio.to_thread(self.engine.load_filters)
        await asyncio.to_thread(self._scan_and_prefill)
        await asyncio.to_thread(self.engine.restore_open)
        await asyncio.to_thread(self.engine.sweep_orphan_algo)

        try:
            from notify import tg_send
            trade = self.engine.trade
            pool = len(self.universe)
            if self.fade_cfg.get("enabled"):
                sig_txt = (f"暴涨做空 1h≥{self.fade_cfg.get('entry_pct')}% / "
                           f"15m≥{self.fade_cfg.get('entry_15m_pct')}%")
            elif self.setup_cfg.get("enabled"):
                sig_txt = f"两段式 {self.setup_cfg.get('htf_minutes')}m→1m 放量"
            else:
                sig_txt = "单根 pinbar"
            tg_send(f"🚀 自研策略已启动\n"
                    f"模式 {self.engine.mode} · {self.interval} · 候选 {pool} 个\n"
                    f"信号 {sig_txt}\n"
                    f"单笔 {trade.get('notional')}U · 最多加仓 {self.cfg.get('basket', {}).get('max_adds', 0)} 次 · "
                    f"同时 {trade.get('max_positions')} 个币\n"
                    f"日亏上限 {trade.get('max_daily_loss')}U · 收盘 {trade.get('cutoff')} 强平")
        except Exception:  # noqa: BLE001
            pass

        threading.Thread(target=self._signal_loop, daemon=True).start()
        threading.Thread(target=self._manage_loop, daemon=True).start()
        uni_task = asyncio.create_task(self._universe_loop())
        rest_task = None
        feed = str(self.cfg.get("feed", "auto") or "auto").lower()
        self._rest_enabled = feed in ("rest", "auto")
        if feed in ("rest", "auto"):
            rest_task = asyncio.create_task(self._rest_loop())
            log(f"REST 兜底已开启：每 {self.cfg.get('rest_poll_s', 5)}s 轮询 "
                f"{len(self.universe)} 个候选")
        try:
            if feed == "rest":
                await rest_task
            else:
                await self._stream_loop()
        finally:
            self._stop.set()
            uni_task.cancel()
            if rest_task:
                rest_task.cancel()
            if self.engine.positions:
                log("停止中，平掉策略持仓")
                await asyncio.to_thread(self.engine.close_all,
                                        dict(self.prices), "shutdown")
            await asyncio.to_thread(self.engine.sweep_orphan_algo)
            log(f"策略已停止。{self.engine.summary()}")


def probe(cfg: dict) -> None:
    """拉最近 K 线，逐根回放最近 12 根，看当前有没有信号（不下单）。"""
    syms = scan_universe(cfg)
    if not syms:
        print("候选池为空（涨幅/成交额门槛可能太高）")
        return
    print(f"候选 {len(syms)} 个：{', '.join(syms)}\n")
    hits = 0
    for sym in syms:
        try:
            bars = fetch_bars(sym, cfg.get("interval", "1m"))
        except Exception as e:  # noqa: BLE001
            print(f"{sym:14s} 取 K 线失败: {e}")
            continue
        last = bars[-1]
        res = SP.evaluate(bars, cfg.get("signal") or {})
        chg = (last["c"] / last["o"] - 1) * 100
        flag = "★ 命中" if res["hit"] else "  -   "
        reason = "" if res["hit"] else res.get("reason", "")
        extra = ""
        if res.get("detail"):
            d = res["detail"]
            extra = (f"量{d['vol_ratio']}x TR{d['tr_ratio']}x 实体{d['body_ratio']} "
                     f"上影{d['upper_ratio']}")
        print(f"{flag} {sym:14s} 收{last['c']:<12g} 本根{chg:+.2f}%  {extra} {reason}")
        hits += int(bool(res["hit"]))
    print(f"\n命中 {hits}/{len(syms)}")


def replay(cfg: dict, bars_per_symbol: int) -> None:
    """按当前参数回放最近 N 根 K 线，统计信号频率与假想盈亏（不下单）。

    出场按引擎同一套规则粗估（阶梯止盈 + 追踪止盈 + 止损 + 超时）：
    同一根 K 线同时触及止损和止盈时按止损算，不含手续费与滑点，也不模拟加仓。
    """
    syms = scan_universe(cfg)
    if not syms:
        print("候选池为空")
        return
    from strategy_engine import ladder_of
    trade = cfg.get("trade") or {}
    sig = cfg.get("signal") or {}
    setup = cfg.get("setup") or {}
    fade = dict(cfg.get("fade") or {})
    fade["direction"] = str(cfg.get("direction", "short") or "short")
    basket = cfg.get("basket") or {}
    bar_s = INTERVAL_S.get(cfg.get("interval", "1m"), 60)
    hold_bars = max(1, int(round(float(trade.get("max_hold_s", 180) or 180) / bar_s)))
    if fade.get("enabled"):
        warm = max(65, int(fade.get("lookback_min", 60)) + 2)
    elif setup.get("enabled"):
        warm = max(60, (int(setup.get("lookback", 12)) + 2) * int(setup.get("htf_minutes", 30)))
    else:
        warm = max(60, (int(sig.get("htf_lookback", 12)) + 2) * int(sig.get("htf_minutes", 15))
                   if sig.get("htf_enabled") else 60)
    if fade.get("enabled"):
        mode_txt = (f"暴涨做空 1h≥{fade.get('entry_pct')}% / 15m≥{fade.get('entry_15m_pct')}% "
                    f"+ 均价加仓 {basket.get('add_step_pct')}%×{basket.get('max_adds')} "
                    f"止盈 {basket.get('basket_tp_pct')}% 硬止损 {basket.get('hard_stop_pct')}%")
    elif setup.get("enabled"):
        mode_txt = (f"两段式 {setup.get('htf_minutes')}m→1m 放量{setup.get('trig_vol_mult')}x"
                    f"（预备 {setup.get('arm_bars')} 根有效）")
    else:
        mode_txt = (f"单根形态 HTF {'开' if sig.get('htf_enabled') else '关'} "
                    f"{sig.get('htf_minutes')}m/{sig.get('htf_high_zone')}/{sig.get('htf_ext_atr')}ATR")
    print(f"回放 {len(syms)} 个币 × 最近 {bars_per_symbol} 根 "
          f"{cfg.get('interval')}（预热 {warm} 根）\n"
          f"  信号：{mode_txt}\n"
          f"  最长持仓 {float(trade.get('max_hold_s', 180) or 180):.0f}s\n")
    total = wins = 0
    pnls: list[float] = []
    for sym in syms:
        try:
            bars = fetch_bars(sym, cfg.get("interval", "1m"), limit=bars_per_symbol)
        except Exception as e:  # noqa: BLE001
            print(f"{sym:14s} 取数失败: {e}")
            continue
        hits = 0
        sym_pnl = 0.0
        stage = SP.TwoStage(setup) if setup.get("enabled") else None
        block_until = 0
        for i in range(warm, len(bars) - 1):
            if fade.get("enabled"):
                if i < block_until:
                    continue
                sig_f = SF.fade_signal(bars[:i + 1], fade)
                if not sig_f:
                    continue
                hits += 1
                pnl, _how, _adds, held = SF.simulate_basket(
                    bars, i, basket, bar_s, sig_f.get("side", "SHORT"))
                block_until = i + max(1, held) + 1
            else:
                if stage is not None:
                    if not stage.on_bar(bars[:i + 1]):
                        continue
                elif not SP.evaluate(bars[:i + 1], sig)["hit"]:
                    continue
                hits += 1
                pnl, _how, _held = simulate_exit(bars, i, trade, bar_s)
            pnls.append(pnl)
            sym_pnl += pnl
            wins += int(pnl > 0)
        total += hits
        if hits:
            print(f"{sym:14s} 信号 {hits:3d}  合计 {sym_pnl:+.2f}%")
    print()
    if total:
        print(f"信号 {total} 笔 · 胜率 {wins / total * 100:.0f}% · "
              f"均值 {sum(pnls) / total:+.3f}% · 合计 {sum(pnls):+.2f}%")
        print("（粗估：同根同时触及按止损优先；未扣手续费与滑点；加仓按步长近似）")
    else:
        print("样本内没有信号：门槛偏高，或这段行情没有这种形态")


def main() -> None:
    ap = argparse.ArgumentParser(description="自研策略：放量急拉 + 上影反转做空")
    ap.add_argument("--scan", action="store_true", help="只打印候选池")
    ap.add_argument("--probe", action="store_true", help="拉 K 线试算，不下单")
    ap.add_argument("--replay", type=int, default=0,
                    help="回放最近 N 根 K 线，统计信号与假想盈亏")
    ap.add_argument("--mode", default="", help="覆盖模式 shadow/live_dry/live")
    ap.add_argument("--interval", default="", help="覆盖 K 线周期")
    ap.add_argument("--top", type=int, default=0, help="覆盖候选池数量")
    ap.add_argument("--preset", default="", help="覆盖参数预设：scalp / swing")
    ap.add_argument("--direction", default="", choices=["short", "long", "both"],
                    help="覆盖方向：short / long / both")
    ap.add_argument("--entry-pct", type=float, default=0.0, help="覆盖 1h 阈值 %%")
    ap.add_argument("--entry-15m-pct", type=float, default=0.0, help="覆盖 15m 阈值 %%")
    args = ap.parse_args()

    cfg = load_cfg()
    src = "文件" if Path(CONFIG_PATH).exists() else "默认值"
    print(f"配置来源：{src} · 预设 {args.preset or '（沿用文件）'} · "
          f"模式 {cfg.get('mode')} · 周期 {cfg.get('interval')}", flush=True)
    if args.preset:
        from strategy_config import PRESETS, apply_preset
        if args.preset not in PRESETS:
            sys.exit(f"未知预设 {args.preset}，可选：{', '.join(PRESETS)}")
        cfg = apply_preset(cfg, args.preset)
    if args.mode:
        cfg["mode"] = args.mode
    if args.direction:
        cfg["direction"] = args.direction
    if args.entry_pct:
        cfg.setdefault("fade", {})["entry_pct"] = args.entry_pct
    if args.entry_15m_pct:
        cfg.setdefault("fade", {})["entry_15m_pct"] = args.entry_15m_pct
    if args.interval:
        cfg["interval"] = args.interval
    if args.top:
        cfg["universe"]["top_n"] = args.top

    if args.scan:
        syms = scan_universe(cfg)
        print(f"\n候选池（{len(syms)}）：")
        for s in syms:
            print(" ", s)
        return
    if args.probe:
        probe(cfg)
        return
    if args.replay:
        replay(cfg, args.replay)
        return

    if cfg.get("mode") == "live":
        from trade_lock import acquire_trade_lock
        if not acquire_trade_lock("strategy"):
            sys.exit(1)

    runner = Runner(cfg)
    acquire_single_instance()
    set_log_file(ROOT / "bot" / "data" /
                 f"strategy-run-{datetime.now():%Y%m%d}.log")
    try:
        asyncio.run(runner.run())
    except KeyboardInterrupt:
        log("bye")
    finally:
        try:
            from version import __version__
            from notify import tg_send
            tg_send(f"🔴 自研策略已停止 v{__version__}\n{runner.engine.summary()}")
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    main()
