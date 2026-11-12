#!/usr/bin/env python3
"""自研策略 · 放量急拉后的高位上影线反转（做空）。

只吃已收盘的 K 线，五条同时成立才算信号：

    1. 放量       本根成交量 ≥ 前 60 根成交量中位数 × vol_mult
    2. 波动放大   本根真实波幅 TR ≥ 前 60 根 TR 中位数 × tr_mult
    3. 已被急拉   过去 pump_lookback 根内的最高价相对起点涨幅 ≥ pump_pct
    4. 上影线反转 收盘阴线、实体 ≤ 1/3 振幅、上影 ≥ 50% 振幅、下影 ≤ 25%
    5. 振幅够大   本根振幅 ≥ TR 中位数 × min_range_atr（避免小十字星误报）

基线用中位数而不是均值：拉升本身会放量，均值会被自己的量抬高，中位数不会。

命中后由 strategy_engine 以 IOC 限价做空，止盈/止损/超时/收盘前强平全在引擎里。

本文件是纯函数，不碰网络与账户，方便拿历史 K 线验证参数。
"""
from __future__ import annotations

from typing import Iterable, Sequence


def _median(xs: Sequence[float]) -> float:
    """中位数当基线：拉升本身的放量不会把自己的基准抬高，比均值稳。"""
    s = sorted(xs)
    n = len(s)
    if not n:
        return 0.0
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def _true_range(bar: dict, prev_close: float) -> float:
    h, l = float(bar["h"]), float(bar["l"])
    return max(h - l, abs(h - prev_close), abs(l - prev_close))


def evaluate(bars: Sequence[dict], cfg: dict) -> dict:
    """逐条判定，返回 {'hit': bool, 'reason': str, ...明细}。

    bars 按时间从旧到新，每根形如 {"t","o","h","l","c","v"}。
    """
    c = {
        "vol_mult": float(cfg.get("vol_mult", 6.0)),
        "tr_mult": float(cfg.get("tr_mult", 3.0)),
        "pump_pct": float(cfg.get("pump_pct", 2.0)),
        "pump_lookback": int(cfg.get("pump_lookback", 15)),
        "body_max_ratio": float(cfg.get("body_max_ratio", 0.34)),
        "upper_min_ratio": float(cfg.get("upper_min_ratio", 0.5)),
        "lower_max_ratio": float(cfg.get("lower_max_ratio", 0.25)),
        "min_range_atr": float(cfg.get("min_range_atr", 1.5)),
    }
    lookback = max(1, c["pump_lookback"])
    need = max(40, lookback + 2)
    if len(bars) < need:
        return {"hit": False, "reason": "warmup", "need": need, "have": len(bars)}

    last = bars[-1]
    o, h, l, close, vol = (float(last["o"]), float(last["h"]), float(last["l"]),
                           float(last["c"]), float(last["v"]))
    rng = h - l
    if rng <= 0:
        return {"hit": False, "reason": "flat_bar"}

    body = abs(close - o)
    upper = h - max(o, close)
    lower = min(o, close) - l

    window = min(60, len(bars) - 1)
    vol_base = _median([float(b["v"]) for b in bars[-1 - window:-1]])
    vol_ratio = vol / vol_base if vol_base > 0 else 0.0

    trs = []
    for i in range(len(bars) - window, len(bars)):
        prev_close = float(bars[i - 1]["c"])
        trs.append(_true_range(bars[i], prev_close))
    tr_base = _median(trs[:-1])
    tr_now = trs[-1]
    tr_ratio = tr_now / tr_base if tr_base > 0 else 0.0

    anchor = float(bars[-1 - lookback]["c"])
    peak = max(float(b["h"]) for b in bars[-lookback:])
    pump = (peak / anchor - 1.0) * 100 if anchor > 0 else 0.0

    checks = {
        "volume": vol_ratio >= c["vol_mult"],
        "volatility": tr_ratio >= c["tr_mult"],
        "pump": pump >= c["pump_pct"],
        "bearish_body": close < o,
        "small_body": body / rng <= c["body_max_ratio"],
        "upper_wick": upper / rng >= c["upper_min_ratio"],
        "lower_wick": lower / rng <= c["lower_max_ratio"],
        "range_big": tr_now >= tr_base * c["min_range_atr"],
    }
    detail = {
        "open": o, "high": h, "low": l, "close": close, "volume": vol,
        "vol_ratio": round(vol_ratio, 2),
        "tr_ratio": round(tr_ratio, 2),
        "pump_pct": round(pump, 2),
        "body_ratio": round(body / rng, 3),
        "upper_ratio": round(upper / rng, 3),
        "lower_ratio": round(lower / rng, 3),
        "range": round(rng, 8),
        "checks": checks,
    }
    if not all(checks.values()):
        return {"hit": False, "reason": "rule_miss", "detail": detail}
    return {"hit": True, "reason": "ok", "detail": detail}


def detect(bars: Iterable[dict], cfg: dict) -> dict | None:
    """命中返回明细，否则 None。"""
    bars = list(bars)
    res = evaluate(bars, cfg)
    return res["detail"] if res["hit"] else None
