#!/usr/bin/env python3
"""合约过滤器：白名单 + 黑名单（手动/自动分级） + 自动拉黑。

统一存放于 bot/data/symbol-filter.json：
    {
      "whitelist": ["AKEUSDT", ...],
      "blacklist": {
        "SOPHUSDT": {
          "reason": "垂直拉升 1h +25.7% / 15m +10.1%",
          "until": "2026-11-15",          # 兼容旧版，按天
          "until_ts": 1794672000,         # 精确到期（秒）
          "level": "6h",                  # 1h / 6h / 1d / 手动
          "ts": 1794668400
        }
      },
      "strikes": {"SOPHUSDT": {"count": 1, "last_ts": 1794668400}}
    }

规则：
    1. 在黑名单（未过期）→ 拒绝
    2. 自动 Runaway 分级：
       - 轻度（1h<25% 且 15m<12%）→ 1h
       - 一般 Runaway → 6h
       - 极端（1h≥40% 或 15m≥20%）→ 1d
       - 24h 内重复触发 → 1d
    3. 手动拉黑默认永久；strikes 超过 24h 未触发自动归零
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timedelta
from pathlib import Path

PATH = Path(__file__).resolve().parent / "data" / "symbol-filter.json"
ALL_PATH = Path(__file__).resolve().parent / "data" / "symbols-all.json"

MILD_1H = 25.0
MILD_15M = 12.0
EXTREME_1H = 40.0
EXTREME_15M = 20.0


def list_all(force: bool = False, max_age_h: float = 24.0) -> list[str]:
    """全部可交易的 USDT 永续合约，按 24h 成交额降序。"""
    if not force:
        try:
            data = json.loads(ALL_PATH.read_text(encoding="utf-8"))
            if (time.time() - float(data.get("ts") or 0)) < max_age_h * 3600:
                syms = data.get("symbols") or []
                if syms:
                    return list(syms)
        except Exception:  # noqa: BLE001
            pass
    from net import http_json
    info = http_json("https://fapi.binance.com/fapi/v1/exchangeInfo", timeout=20)
    tick = {}
    try:
        for row in http_json("https://fapi.binance.com/fapi/v1/ticker/24hr", timeout=20):
            tick[row.get("symbol")] = float(row.get("quoteVolume") or 0)
    except Exception:  # noqa: BLE001
        pass
    syms = [s["symbol"] for s in info.get("symbols", [])
            if s.get("contractType") == "PERPETUAL"
            and s.get("quoteAsset") == "USDT"
            and s.get("status") == "TRADING"]
    syms.sort(key=lambda s: -tick.get(s, 0))
    try:
        ALL_PATH.parent.mkdir(parents=True, exist_ok=True)
        ALL_PATH.write_text(json.dumps({"ts": int(time.time()), "symbols": syms},
                                       ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    return syms


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def load() -> dict:
    try:
        data = json.loads(PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        data = {}
    return {
        "whitelist": list(data.get("whitelist") or []),
        "blacklist": dict(data.get("blacklist") or {}),
        "strikes": dict(data.get("strikes") or {}),
    }


def save(data: dict) -> None:
    PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(PATH)


def _clean(data: dict) -> dict:
    """丢掉已过期的黑名单；strikes 超过 24h 自动归零。"""
    now = time.time()
    today = _today()
    out = {}
    for s, v in data["blacklist"].items():
        ts = v.get("until_ts")
        if ts:
            if float(ts) > now:
                out[s] = v
        else:
            until = v.get("until")
            if not until or str(until) >= today:
                out[s] = v
    data["blacklist"] = out
    strikes = {}
    for s, h in (data.get("strikes") or {}).items():
        last = float(h.get("last_ts") or 0)
        if last and now - last <= 86400:
            strikes[s] = h
    data["strikes"] = strikes
    return data


def _level_from_minutes(minutes: int) -> str:
    if minutes >= 1440:
        return "1d"
    if minutes >= 360:
        return "6h"
    return "1h"


def remaining_text(entry: dict) -> str:
    """给 Web/GUI 用的剩余时间文本。"""
    ts = entry.get("until_ts")
    if ts:
        sec = int(float(ts) - time.time())
        if sec <= 0:
            return "已到期"
        if sec < 3600:
            return f"剩{max(1, sec // 60)}m"
        if sec < 86400:
            h, m = sec // 3600, (sec % 3600) // 60
            return f"剩{h}h" + (f"{m}m" if m else "")
        return f"剩{sec // 86400}d"
    until = str(entry.get("until") or "")
    if not until:
        return "永久"
    try:
        days = (datetime.strptime(until, "%Y-%m-%d").date()
                - datetime.now().date()).days
        return f"剩{max(days, 0)}d"
    except Exception:  # noqa: BLE001
        return until


def describe(symbol: str, entry: dict) -> dict:
    level = str(entry.get("level") or "")
    if not level:
        level = "1d" if entry.get("until") else "手动"
    return {"symbol": symbol, "reason": str(entry.get("reason") or "手动"),
            "until": str(entry.get("until") or ""),
            "until_ts": entry.get("until_ts") or 0,
            "level": level, "remaining": remaining_text(entry),
            "strikes": int((entry.get("strikes") or 0)),
            "auto": "垂直拉升" in str(entry.get("reason") or "")}


def is_allowed(symbol: str) -> tuple[bool, str]:
    data = _clean(load())
    b = data["blacklist"].get(symbol)
    if b:
        d = describe(symbol, b)
        return False, f"黑名单（{d['reason']}，{d['level']}档，{d['remaining']}）"
    wl = data.get("whitelist") or []
    if wl and symbol not in wl:
        return False, "不在白名单"
    return True, ""


def block(symbol: str, reason: str = "", days: int = 1,
          minutes: int | None = None, level: str = "") -> None:
    """手动/通用拉黑；minutes=0 表示永久。days 兼容旧调用。"""
    now = time.time()
    if minutes is None:
        minutes = int(days * 1440) if days > 0 else 0
    data = _clean(load())
    entry = {"reason": reason, "ts": int(now)}
    if minutes > 0:
        entry["until_ts"] = int(now + minutes * 60)
        entry["until"] = datetime.fromtimestamp(entry["until_ts"]).strftime("%Y-%m-%d")
        entry["level"] = level or _level_from_minutes(minutes)
    else:
        entry["until_ts"] = 0
        entry["until"] = ""
        entry["level"] = level or "手动"
    data["blacklist"][symbol] = entry
    # 手动拉黑重置自动升级计数
    data["strikes"].pop(symbol, None)
    save(data)


def block_auto(symbol: str, reason: str, chg_1h: float, chg_15m: float) -> tuple[str, int]:
    """Runaway 自动分级；返回 (档位, 分钟)。"""
    now = time.time()
    data = _clean(load())
    hist = data["strikes"].get(symbol) or {}
    last = float(hist.get("last_ts") or 0)
    count = int(hist.get("count") or 0)
    if now - last > 86400:
        count = 0
    count += 1
    extreme = chg_1h >= EXTREME_1H or chg_15m >= EXTREME_15M
    if count >= 2 or extreme:
        minutes, level = 1440, "1d"
    elif chg_1h < MILD_1H and chg_15m < MILD_15M:
        minutes, level = 60, "1h"
    else:
        minutes, level = 360, "6h"
    entry = {"reason": reason, "ts": int(now),
             "until_ts": int(now + minutes * 60),
             "until": datetime.fromtimestamp(now + minutes * 60).strftime("%Y-%m-%d"),
             "level": level, "strikes": count}
    data["blacklist"][symbol] = entry
    data["strikes"][symbol] = {"count": count, "last_ts": int(now)}
    save(data)
    return level, minutes


def unblock(symbol: str) -> bool:
    data = _clean(load())
    if symbol not in data["blacklist"]:
        return False
    data["blacklist"].pop(symbol, None)
    data["strikes"].pop(symbol, None)
    save(data)
    return True


def set_whitelist(symbols: list[str]) -> None:
    data = _clean(load())
    data["whitelist"] = [s.strip().upper() for s in symbols if s.strip()]
    save(data)


def clear_blacklist() -> int:
    data = _clean(load())
    n = len(data["blacklist"])
    data["blacklist"] = {}
    data["strikes"] = {}
    save(data)
    return n


def show() -> None:
    data = _clean(load())
    wl = data.get("whitelist") or []
    print(f"白名单（{len(wl)}）：{', '.join(wl) if wl else '空（不做限制）'}")
    bl = data.get("blacklist") or {}
    print(f"黑名单（{len(bl)}）：")
    for s, v in bl.items():
        d = describe(s, v)
        print(f"  {s:14s} {d['level']:3s}  {d['remaining']:8s}  {d['reason']}")


def main() -> None:
    ap = argparse.ArgumentParser(description="合约过滤器")
    ap.add_argument("--block", default="", help="拉黑一个币")
    ap.add_argument("--unblock", default="", help="解除黑名单")
    ap.add_argument("--reason", default="手动", help="拉黑原因")
    ap.add_argument("--days", type=int, default=1, help="拉黑天数，0=永久")
    ap.add_argument("--hours", type=float, default=None, help="拉黑小时数")
    ap.add_argument("--minutes", type=float, default=None, help="拉黑分钟数")
    ap.add_argument("--white", default=None, help="设置白名单（逗号分隔，空串清空）")
    ap.add_argument("--clear-black", action="store_true", help="清空黑名单")
    args = ap.parse_args()

    if args.block:
        sym = args.block.upper()
        if args.minutes is not None:
            block(sym, args.reason, minutes=int(args.minutes))
        elif args.hours is not None:
            block(sym, args.reason, minutes=int(args.hours * 60))
        else:
            block(sym, args.reason, days=args.days)
        print("已拉黑", sym)
    if args.unblock:
        print("已解除" if unblock(args.unblock.upper()) else "不在黑名单", args.unblock.upper())
    if args.white is not None:
        set_whitelist(args.white.split(","))
        print("白名单已更新")
    if args.clear_black:
        print(f"已清空 {clear_blacklist()} 个")
    if not any((args.block, args.unblock, args.white is not None, args.clear_black)):
        show()


if __name__ == "__main__":
    main()
