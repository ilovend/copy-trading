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
from strategy_config import load as load_cfg  # noqa: E402
from strategy_engine import (INTERVAL_S, StrategyEngine, ladder_of, log,  # noqa: E402
                             simulate_exit)

FAPI = "https://fapi.binance.com"
WS = "wss://fstream.binance.com/stream"
BARS_KEEP = 120


def scan_universe(cfg: dict) -> list[str]:
    """涨幅榜：24h 涨幅 + 成交额过滤，取前 N。"""
    from copy_engine import http_json
    u = cfg.get("universe") or {}
    top_n = int(u.get("top_n", 15) or 15)
    min_chg = float(u.get("min_change_pct", 3.0) or 0)
    min_qv = float(u.get("min_quote_volume", 50e6) or 0)
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
    cand.sort(key=lambda x: x[0], reverse=True)
    return [sym for _, _, sym in cand[:top_n]]


def fetch_bars(symbol: str, interval: str, limit: int = BARS_KEEP) -> list[dict]:
    from copy_engine import http_json
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

    # ------------------------------------------------------------- 数据准备
    def prefill(self, symbols: list[str]) -> None:
        for sym in symbols:
            if sym in self.bars:
                continue
            try:
                bars = fetch_bars(sym, self.interval)
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
                res = SP.evaluate(bars, self.cfg.get("signal") or {})
                if not res["hit"]:
                    continue
                log(f"信号   {symbol:12s} 放量 {res['detail']['vol_ratio']}x "
                    f"TR {res['detail']['tr_ratio']}x 上影 "
                    f"{res['detail']['upper_ratio'] * 100:.0f}%")
                self.engine.open_short(symbol, res["detail"], bar["c"])
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
                    log("策略已收盘退出（不隔夜）")
                    self._stop.set()
                    return
                now = time.time()
                if now - self._last_summary >= 300:
                    self._last_summary = now
                    if self.engine.stats["signals"] or self.engine.positions:
                        log(f"统计   {self.engine.summary()}")
            except Exception as e:  # noqa: BLE001
                log(f"持仓管理异常: {e}")
            time.sleep(1.0)

    # ------------------------------------------------------------- WS
    def _handle(self, msg: dict) -> None:
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
        self.bars[symbol].append(bar)
        try:
            self.signals.put_nowait((symbol, bar))
        except queue.Full:
            log("信号队列已满，丢弃一根 K 线")

    async def _stream_loop(self) -> None:
        backoff = 1
        while not self._stop.is_set():
            if not self.universe:
                await asyncio.sleep(1)
                continue
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
            try:
                await asyncio.to_thread(self._scan_and_prefill)
            except Exception as e:  # noqa: BLE001
                log(f"涨榜扫描失败: {e}")
            refresh = float((self.cfg.get("universe") or {}).get("refresh_min", 5) or 5)
            await asyncio.sleep(max(30.0, refresh * 60))

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

        threading.Thread(target=self._signal_loop, daemon=True).start()
        threading.Thread(target=self._manage_loop, daemon=True).start()
        uni_task = asyncio.create_task(self._universe_loop())
        try:
            await self._stream_loop()
        finally:
            self._stop.set()
            uni_task.cancel()
            if self.engine.positions:
                log("停止中，平掉策略持仓")
                await asyncio.to_thread(self.engine.close_all,
                                        dict(self.prices), "shutdown")
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
    bar_s = INTERVAL_S.get(cfg.get("interval", "1m"), 60)
    hold_bars = max(1, int(round(float(trade.get("max_hold_s", 180) or 180) / bar_s)))
    print(f"回放 {len(syms)} 个币 × 最近 {bars_per_symbol} 根 "
          f"{cfg.get('interval')}\n"
          f"  止损 {trade.get('sl_pct')}% · 阶梯 {ladder_of(trade)} · "
          f"追踪 {trade.get('trail_activate_pct')}%/{trade.get('trail_gap_pct')}pp · "
          f"最长 {hold_bars} 根（不含加仓）\n")
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
        for i in range(60, len(bars) - 1):
            if not SP.evaluate(bars[:i + 1], cfg.get("signal") or {})["hit"]:
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
        print("（粗估：同根同时触及按止损算，未扣手续费与滑点，未模拟加仓）")
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
    args = ap.parse_args()

    cfg = load_cfg()
    if args.mode:
        cfg["mode"] = args.mode
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

    runner = Runner(cfg)
    try:
        asyncio.run(runner.run())
    except KeyboardInterrupt:
        log("bye")
    finally:
        try:
            from version import __version__
            from listen_copy import tg_send
            tg_send(f"🔴 自研策略已停止 v{__version__}\n{runner.engine.summary()}")
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    main()
