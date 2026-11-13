"""公开示例策略：只演示插件接口，不含任何实盘阈值。"""
from __future__ import annotations

FIELDS = (
    ("示例参数", (
        ("快线周期", "example.fast"),
        ("慢线周期", "example.slow"),
    )),
)

SPEC = {
    "id": "example",
    "name": "示例策略：均线交叉",
    "tab": "示例策略",
    "desc": "公开示例：快慢均线交叉，仅用于演示插件加载与 GUI 跑通，不构成交易建议。",
    "kind": "plugin",
    "fields": FIELDS,
    "presets": ("example",),
}


def _ma(values, n):
    if len(values) < n or n <= 0:
        return None
    return sum(values[-n:]) / n


def signal(bars, cfg):
    sec = cfg.get("example") if isinstance(cfg.get("example"), dict) else cfg
    fast = int(sec.get("fast", 20) or 20)
    slow = int(sec.get("slow", 60) or 60)
    closes = [float(b["c"]) for b in bars]
    if len(closes) < max(fast, slow) + 2:
        return None
    f_now, s_now = _ma(closes, fast), _ma(closes, slow)
    f_pre, s_pre = _ma(closes[:-1], fast), _ma(closes[:-1], slow)
    if None in (f_now, s_now, f_pre, s_pre):
        return None
    side = None
    if f_pre <= s_pre and f_now > s_now:
        side = "LONG"
    elif f_pre >= s_pre and f_now < s_now:
        side = "SHORT"
    if not side:
        return None
    c1h = closes[-min(60, len(closes) - 1)]
    c15 = closes[-min(15, len(closes) - 1)]
    return {"side": side, "chg_1h": (closes[-1] / c1h - 1) * 100,
            "chg_15m": (closes[-1] / c15 - 1) * 100,
            "ma_fast": round(f_now, 8), "ma_slow": round(s_now, 8)}


def simulate_basket(bars, i, basket, bar_s, side):
    """极简演示回放：固定止盈止损，不含加仓。"""
    tp = float(basket.get("basket_tp_pct", 4) or 4)
    sl = float(basket.get("hard_stop_pct", 6) or 6)
    hold = max(1, int(float(basket.get("max_hold_s", 3600) or 3600) / bar_s))
    entry = float(bars[i]["c"])
    for j in range(i + 1, min(i + 1 + hold, len(bars))):
        hi, lo = float(bars[j]["h"]), float(bars[j]["l"])
        if side == "SHORT":
            if (hi / entry - 1) * 100 >= sl:
                return -sl, "stop", 0, j - i
            if (lo / entry - 1) * 100 <= -tp:
                return tp, "tp", 0, j - i
        else:
            if (lo / entry - 1) * 100 <= -sl:
                return -sl, "stop", 0, j - i
            if (hi / entry - 1) * 100 >= tp:
                return tp, "tp", 0, j - i
    return 0.0, "timeout", 0, hold
