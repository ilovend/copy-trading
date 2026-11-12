#!/usr/bin/env python3
"""逐笔校验：用 aggTrades 走一遍信号之后的真实价格路径，和 1m K 线的粗估对账。

为什么需要它：
    1m K 线只有一个 OHLC，同一根里「先摸止盈还是先摸止损」是不知道的；回放一律按
    「先止损」保守处理，剥头皮这种 0.3~0.5% 的级别误差会很大。这里用逐笔成交按时间
    顺序判断，才是真实路径。

用法：
    python bot/strategy_tick_check.py --preset scalp --symbols 10
    python bot/strategy_tick_check.py --preset swing --symbols 6 --hold 300

输出：
    每个信号的 1m 估 / 逐笔估 / 是否翻转，最后给汇总对比。
数据：
    GET /fapi/v1/klines       取 1m（1500 根，用于信号与 HTF）
    GET /fapi/v1/aggTrades    只取信号之后 max_hold 秒的逐笔，请求量很小
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bot"))

from copy_engine import http_json  # noqa: E402
import strategy_config as SC  # noqa: E402
import strategy_engine as SE  # noqa: E402
import strategy_run as RS  # noqa: E402
import strategy_volume_pinbar as SP  # noqa: E402

FAPI = "https://fapi.binance.com"
FEE = 0.10          # 往返 taker 手续费，%
PAUSE = 0.2         # 请求间隔，避免打到 -1003 限频


def get_list(path: str, timeout: int = 20, tries: int = 6) -> list:
    """请求并断言返回数组；被限频（-1003）就等一会儿重试。"""
    for i in range(tries):
        r = http_json(path, timeout=timeout)
        if isinstance(r, list):
            time.sleep(PAUSE)
            return r
        code = r.get("code") if isinstance(r, dict) else None
        if code == -1003:
            wait = 20 + i * 10
            print(f"    被限频，等 {wait}s…")
            time.sleep(wait)
            continue
        raise RuntimeError(f"API 返回异常: {str(r)[:160]}")
    return []


def fetch_klines(symbol: str, limit: int = 1500) -> list[dict]:
    rows = get_list(f"{FAPI}/fapi/v1/klines?symbol={symbol}&interval=1m&limit={limit}")
    return [{"t": int(k[0]), "o": float(k[1]), "h": float(k[2]),
             "l": float(k[3]), "c": float(k[4]), "v": float(k[5])} for k in rows]


def scan_universe(cfg: dict) -> list[str]:
    for i in range(6):
        try:
            return RS.scan_universe(cfg)
        except Exception as e:  # noqa: BLE001
            wait = 20 + i * 10
            print(f"  候选池扫描失败（{e}），等 {wait}s…")
            time.sleep(wait)
    return []


def fetch_agg(symbol: str, start_ms: int, end_ms: int) -> list[dict]:
    """取窗口内逐笔（自动翻页，1000 笔/次）。"""
    out: list[dict] = []
    from_id: int | None = None
    guard = 0
    while guard < 40:
        guard += 1
        if from_id is None:
            q = (f"symbol={symbol}&startTime={start_ms}&endTime={end_ms}&limit=1000")
        else:
            q = f"symbol={symbol}&fromId={from_id}&limit=1000"
        try:
            rows = get_list(f"{FAPI}/fapi/v1/aggTrades?{q}")
        except Exception as e:  # noqa: BLE001
            print(f"    aggTrades 失败 {symbol}: {e}")
            break
        if not rows:
            break
        out.extend(rows)
        if len(rows) < 1000:
            break
        last_t = int(rows[-1]["T"])
        if last_t > end_ms:
            break
        from_id = int(rows[-1]["a"]) + 1
    return [r for r in out if start_ms <= int(r["T"]) <= end_ms]


def sim_tick(trades: list[dict], entry: float, tp: float, sl: float) -> tuple[float, str]:
    """按逐笔顺序判断：先到哪个算哪个。entry 用信号 K 线收盘价。"""
    tp_px = entry * (1 - tp / 100)
    sl_px = entry * (1 + sl / 100)
    for t in trades:
        p = float(t["p"])
        if p >= sl_px:
            return -(p / entry - 1) * 100, "stop_loss"
        if p <= tp_px:
            return (1 - p / entry) * 100, "take_profit"
    if trades:
        last = float(trades[-1]["p"])
        return (1 - last / entry) * 100, "timeout"
    return 0.0, "no_trade"


def main() -> None:
    ap = argparse.ArgumentParser(description="自研策略逐笔校验")
    ap.add_argument("--preset", default="scalp", help="scalp / swing")
    ap.add_argument("--symbols", type=int, default=6, help="取候选池前 N 个币")
    ap.add_argument("--bars", type=int, default=1500, help="拉多少根 1m")
    ap.add_argument("--tp", type=float, default=0, help="覆盖止盈 %%（默认取预设）")
    ap.add_argument("--sl", type=float, default=0, help="覆盖止损 %%（默认取预设）")
    ap.add_argument("--hold", type=float, default=0, help="覆盖最长持仓秒数")
    args = ap.parse_args()

    cfg = SC.apply_preset(SC.load(), args.preset)
    trade, sig = cfg["trade"], cfg["signal"]
    tp = float(args.tp or trade.get("tp1_pct", 0.5))
    sl = float(args.sl or trade.get("sl_pct", 0.3))
    hold_s = float(args.hold or trade.get("max_hold_s", 180))
    trade = dict(trade, tp1_pct=tp, sl_pct=sl, max_hold_s=hold_s)
    hold_ms = int(hold_s * 1000)
    hold_bars = max(1, round(hold_ms / 60000))

    syms = scan_universe(cfg)[: args.symbols]
    setup = cfg.get("setup") or {}
    if setup.get("enabled"):
        warm = max(60, (int(setup.get("lookback", 12)) + 2)
                   * int(setup.get("htf_minutes", 30)))
        mode_txt = (f"两段式 {setup.get('htf_minutes')}m→1m 放量"
                    f"{setup.get('trig_vol_mult')}x")
    else:
        warm = max(60, (int(sig.get("htf_lookback", 12)) + 2)
                   * int(sig.get("htf_minutes", 15)) if sig.get("htf_enabled") else 60)
        mode_txt = "单根形态"
    print(f"预设 {args.preset} · {mode_txt} · {len(syms)} 个币 · 各 {args.bars} 根 1m · "
          f"TP{tp}% SL{sl}% 持仓≤{hold_ms / 1000:.0f}s · 预热 {warm} 根\n")

    rows = []
    for sym in syms:
        try:
            bars = fetch_klines(sym, limit=args.bars)
        except Exception as e:  # noqa: BLE001
            print(f"{sym:12s} K 线失败: {e}")
            continue
        sig_count = 0
        stage = SP.TwoStage(setup) if setup.get("enabled") else None
        for i in range(warm, len(bars) - 1):
            if stage is not None:
                if not stage.on_bar(bars[:i + 1]):
                    continue
            elif not SP.evaluate(bars[:i + 1], sig)["hit"]:
                continue
            sig_count += 1
            entry = float(bars[i]["c"])
            t0 = int(bars[i]["t"]) + 60_000
            trades = fetch_agg(sym, t0, t0 + hold_ms)
            tick_pnl, tick_reason = sim_tick(trades, entry, tp, sl)
            bar_pnl, bar_reason, _held = SE.simulate_exit(bars, i, trade, 60)
            both = (max(float(b["h"]) for b in bars[i + 1:i + 1 + hold_bars]) >= entry * (1 + sl / 100)
                    and min(float(b["l"]) for b in bars[i + 1:i + 1 + hold_bars]) <= entry * (1 - tp / 100))
            rows.append({
                "sym": sym, "t": bars[i]["t"], "entry": entry,
                "bar_pnl": bar_pnl, "bar": bar_reason,
                "tick_pnl": tick_pnl, "tick": tick_reason,
                "trades": len(trades), "both": both,
                "flip": (tick_reason != bar_reason),
            })
        if sig_count:
            print(f"  {sym:12s} 信号 {sig_count}")

    if not rows:
        print("\n样本内没有信号")
        return

    print(f"\n信号 {len(rows)} 个，逐笔窗口 {hold_ms / 1000:.0f}s\n")
    print(f"{'币':12s} {'时间':16s} {'笔数':>5} {'1m 估':>9} {'逐笔':>9} "
          f"{'差异':>7}  同根双触")
    if len(rows) <= 25:
        from datetime import datetime
        for r in rows:
            print(f"{r['sym']:12s} {datetime.fromtimestamp(r['t'] / 1000):%m-%d %H:%M:%S} "
                  f"{r['trades']:5d} {r['bar_pnl']:+8.3f}% {r['tick_pnl']:+8.3f}% "
                  f"{r['tick_pnl'] - r['bar_pnl']:+6.3f}  {'是' if r['both'] else ''}")

    def stat(key: str) -> tuple[float, float]:
        vals = [r[key] for r in rows]
        wins = sum(1 for v in vals if v > 0)
        return wins / len(vals) * 100, sum(vals) / len(vals)

    bar_wr, bar_avg = stat("bar_pnl")
    tick_wr, tick_avg = stat("tick_pnl")
    flips = sum(1 for r in rows if r["flip"])
    both = sum(1 for r in rows if r["both"])
    print(f"\n{'':14s} {'胜率':>6} {'毛均值':>8} {'扣费后':>8}")
    print(f"{'1m 粗估':14s} {bar_wr:5.0f}% {bar_avg:+8.3f}% {bar_avg - FEE:+8.3f}%")
    print(f"{'逐笔实测':14s} {tick_wr:5.0f}% {tick_avg:+8.3f}% {tick_avg - FEE:+8.3f}%")
    print(f"\n结论翻转 {flips}/{len(rows)} 个；1m 窗口内同时触及止盈止损 {both} 个")
    diff = tick_avg - bar_avg
    print(f"1m 粗估相对逐笔：{'高估' if diff < 0 else '低估'} {abs(diff):.3f}% / 笔")


if __name__ == "__main__":
    main()
