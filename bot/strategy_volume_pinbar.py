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


def aggregate_htf(bars_1m: Sequence[dict], minutes: int) -> list[dict]:
    """把 1m 聚合成 minutes 周期的 K 线，只保留已完成的桶。

    最后一个桶通常是「信号所在的半根」，直接丢掉——宁可少一根也不要用残缺结构。
    """
    if minutes <= 1:
        return list(bars_1m)[:-1]
    span = minutes * 60_000
    out: list[dict] = []
    cur: dict | None = None
    for b in bars_1m:
        k = int(b["t"]) // span
        if cur is None or cur["_k"] != k:
            if cur is not None:
                out.append(cur)
            cur = {"_k": k, "o": float(b["o"]), "h": float(b["h"]),
                   "l": float(b["l"]), "c": float(b["c"])}
        else:
            cur["h"] = max(cur["h"], float(b["h"]))
            cur["l"] = min(cur["l"], float(b["l"]))
            cur["c"] = float(b["c"])
    if cur is not None:
        out.append(cur)
    # 最后一个桶里装着信号那根 1m，通常还没走完，丢掉
    return out[:-1] if out else out


def htf_context(bars_1m: Sequence[dict], cfg: dict) -> dict | None:
    """长周期位置：信号那根 K 线的高点，落在 HTF 区间的什么位置。

    返回 None 表示 HTF 数据还不够（预热），此时不开仓。
    """
    minutes = int(cfg.get("htf_minutes", 15) or 15)
    lookback = int(cfg.get("htf_lookback", 12) or 12)
    ema_n = int(cfg.get("htf_ema", 12) or 12)
    htf = aggregate_htf(bars_1m, minutes)
    need = max(lookback, ema_n) + 2
    if len(htf) < need:
        return None
    recent = htf[-lookback:]
    hi = max(b["h"] for b in recent)
    lo = min(b["l"] for b in recent)
    rng = hi - lo
    if rng <= 0:
        return None
    ema = htf[0]["c"]
    k = 2.0 / (ema_n + 1)
    for b in htf[1:]:
        ema = b["c"] * k + ema * (1 - k)
    trs = []
    for j in range(max(1, len(htf) - 15), len(htf)):
        trs.append(_true_range(htf[j], float(htf[j - 1]["c"])))
    atr = sum(trs) / len(trs) if trs else 0.0
    last = bars_1m[-1]
    pos = (float(last["h"]) - lo) / rng
    dist = ((float(last["c"]) - ema) / atr) if atr > 0 else 0.0
    return {"pos": round(pos, 3), "dist_atr": round(dist, 2),
            "range_high": hi, "range_low": lo, "ema": ema, "atr": atr,
            "bars": len(htf)}


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

    # ---- 长周期位置：这根 K 线在 HTF 区间的哪个位置、偏离均值多远
    if cfg.get("htf_enabled", False):
        ctx = htf_context(bars, cfg)
        if ctx is None:
            detail["why"] = "no_htf"
            return {"hit": False, "reason": "no_htf", "detail": detail}
        detail["mtf"] = ctx
        zone = float(cfg.get("htf_high_zone", 0.75))
        ext = float(cfg.get("htf_ext_atr", 0.8))
        if ctx["pos"] < zone:
            return {"hit": False, "reason": "htf_position", "detail": detail}
        if ctx["dist_atr"] < ext:
            return {"hit": False, "reason": "htf_extension", "detail": detail}
    return {"hit": True, "reason": "ok", "detail": detail}


def detect(bars: Iterable[dict], cfg: dict) -> dict | None:
    """命中返回明细，否则 None。"""
    bars = list(bars)
    res = evaluate(bars, cfg)
    return res["detail"] if res["hit"] else None


# --------------------------------------------------------------- 两段式信号
def _ema(values: Sequence[float], n: int) -> float:
    if not values:
        return 0.0
    k = 2.0 / (n + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


def _atr(bars: Sequence[dict], n: int = 14) -> float:
    if len(bars) < 2:
        return 0.0
    trs = []
    for j in range(max(1, len(bars) - n), len(bars)):
        trs.append(_true_range(bars[j], float(bars[j - 1]["c"])))
    return sum(trs) / len(trs) if trs else 0.0


def setup_of(bars_1m: Sequence[dict], cfg: dict) -> dict | None:
    """30m（可调）收盘时判断：这根长周期 K 线是否值得「预备」。

    条件：收盘站上最近 lookback 根 HTF 区间的高位，且相对 HTF 均线明显偏离。
    """
    minutes = int(cfg.get("htf_minutes", 30) or 30)
    lookback = int(cfg.get("lookback", 12) or 12)
    ema_n = int(cfg.get("ema", 12) or 12)
    htf = aggregate_htf(bars_1m, minutes)
    if len(htf) < max(lookback, ema_n) + 2:
        return None
    recent = htf[-lookback:]
    hi = max(b["h"] for b in recent)
    lo = min(b["l"] for b in recent)
    rng = hi - lo
    if rng <= 0:
        return None
    atr = _atr(htf, 14)
    if atr <= 0:
        return None
    ema = _ema([b["c"] for b in htf], ema_n)
    last = htf[-1]
    pos = (float(last["h"]) - lo) / rng
    ext = (float(last["c"]) - ema) / atr
    detail = {"pos": round(pos, 3), "ext_atr": round(ext, 2),
              "range_high": hi, "range_low": lo,
              "close": float(last["c"]), "high": float(last["h"]),
              "atr": atr, "bars": len(htf)}
    if pos >= float(cfg.get("high_zone", 0.8)) and ext >= float(cfg.get("ext_atr", 1.0)):
        return detail
    return None


def trigger_of(bars_1m: Sequence[dict], cfg: dict, armed: dict) -> dict | None:
    """1m 收盘：急剧放量 + 价格还没跌离预备位太远（做空）。"""
    last = bars_1m[-1]
    o, h, l, c, v = (float(last["o"]), float(last["h"]), float(last["l"]),
                     float(last["c"]), float(last["v"]))
    window = min(60, len(bars_1m) - 1)
    base = _median([float(b["v"]) for b in bars_1m[-1 - window:-1]])
    vol_r = v / base if base > 0 else 0.0
    if vol_r < float(cfg.get("trig_vol_mult", 3.0)):
        return None
    high = float(armed.get("high") or 0)
    if high > 0:
        drop = (high - c) / high * 100
        if drop > float(cfg.get("trig_max_drop_pct", 0.5)):
            return None
    if cfg.get("trig_bearish", True) and c >= o:
        return None
    rng = h - l
    upper = (h - max(o, c)) / rng if rng > 0 else 0.0
    return {"vol_ratio": round(vol_r, 2),
            "upper_ratio": round(upper, 3),
            "close": c, "high": h,
            "body_ratio": round(abs(c - o) / rng, 3) if rng > 0 else 0.0,
            "drop_from_setup_pct": round((high - c) / high * 100, 2) if high else 0.0}


class TwoStage:
    """两段式：长周期收盘判「预备」，短周期放量才触发，预备有有效期。"""

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.bucket: int | None = None
        self.armed: dict | None = None

    def on_bar(self, bars_1m: Sequence[dict]) -> dict | None:
        if not bars_1m:
            return None
        last = bars_1m[-1]
        minutes = int(self.cfg.get("htf_minutes", 30) or 30)
        bucket = int(last["t"]) // (minutes * 60_000)

        # 跨进新的 HTF 桶 = 上一根 HTF 刚收盘 → 重新评估预备
        if self.bucket is not None and bucket != self.bucket:
            info = setup_of(bars_1m, self.cfg)
            if info:
                arm_bars = int(self.cfg.get("arm_bars", 2) or 2)
                self.armed = {"until": int(last["t"]) + arm_bars * minutes * 60_000,
                              "high": info["high"], "close": info["close"],
                              "setup": info, "at": int(last["t"])}
        self.bucket = bucket

        if self.armed is None:
            return None
        if int(last["t"]) > int(self.armed["until"]):
            self.armed = None
            return None
        trg = trigger_of(bars_1m, self.cfg, self.armed)
        if not trg:
            return None
        out = dict(trg)
        out["setup"] = self.armed["setup"]
        out["armed_for_s"] = round((int(last["t"]) - self.armed["at"]) / 1000)
        self.armed = None
        return out
