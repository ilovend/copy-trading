#!/usr/bin/env python3
"""合约过滤器：白名单 + 黑名单（手动） + 自动拉黑（垂直拉升，次日解除）。

统一存放于 bot/data/symbol-filter.json：
    {
      "whitelist": ["AKEUSDT", ...],          # 非空时只交易这些
      "blacklist": {"PLAYUSDT": {"reason": "...", "until": "2026-11-13"}}
    }

规则：
    1. 在黑名单（且未过期）→ 拒绝
    2. 白名单非空且不在白名单 → 拒绝
    3. 自动拉黑写入黑名单，until=当天，次日自动失效

用法：
    python bot/symbol_filter.py                       # 查看
    python bot/symbol_filter.py --block PLAYUSDT --reason 手动
    python bot/symbol_filter.py --unblock PLAYUSDT
    python bot/symbol_filter.py --white AKEUSDT,BARDUSDT   # 设置白名单
    python bot/symbol_filter.py --white ""                 # 清空白名单
    python bot/symbol_filter.py --clear-black              # 清空黑名单
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timedelta
from pathlib import Path

PATH = Path(__file__).resolve().parent / "data" / "symbol-filter.json"
ALL_PATH = Path(__file__).resolve().parent / "data" / "symbols-all.json"


def list_all(force: bool = False, max_age_h: float = 24.0) -> list[str]:
    """全部可交易的 USDT 永续合约，按 24h 成交额降序。

    结果缓存到 bot/data/symbols-all.json，默认 24 小时刷新一次。
    """
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
    return {"whitelist": list(data.get("whitelist") or []),
            "blacklist": dict(data.get("blacklist") or {})}


def save(data: dict) -> None:
    PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(PATH)


def _clean(data: dict) -> dict:
    """丢掉已过期的自动拉黑。"""
    today = _today()
    out = {s: v for s, v in data["blacklist"].items()
           if not v.get("until") or str(v["until"]) >= today}
    if len(out) != len(data["blacklist"]):
        data["blacklist"] = out
        save(data)
    return data


def is_allowed(symbol: str) -> tuple[bool, str]:
    data = _clean(load())
    b = data["blacklist"].get(symbol)
    if b:
        until = b.get("until") or "永久"
        return False, f"黑名单（{b.get('reason') or '手动'}，至 {until}）"
    wl = data.get("whitelist") or []
    if wl and symbol not in wl:
        return False, "不在白名单"
    return True, ""


def block(symbol: str, reason: str = "", days: int = 1) -> None:
    data = _clean(load())
    until = ""
    if days > 0:
        until = (datetime.now() + timedelta(days=days - 1)).strftime("%Y-%m-%d")
    data["blacklist"][symbol] = {"reason": reason, "until": until,
                                 "ts": int(time.time())}
    save(data)


def unblock(symbol: str) -> bool:
    data = _clean(load())
    if symbol not in data["blacklist"]:
        return False
    data["blacklist"].pop(symbol, None)
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
    save(data)
    return n


def show() -> None:
    data = _clean(load())
    wl = data.get("whitelist") or []
    print(f"白名单（{len(wl)}）：{', '.join(wl) if wl else '空（不做限制）'}")
    bl = data.get("blacklist") or {}
    print(f"黑名单（{len(bl)}）：")
    for s, v in bl.items():
        print(f"  {s:14s} 至 {v.get('until') or '永久':10s}  {v.get('reason', '')}")


def main() -> None:
    ap = argparse.ArgumentParser(description="合约过滤器")
    ap.add_argument("--block", default="", help="拉黑一个币")
    ap.add_argument("--unblock", default="", help="解除黑名单")
    ap.add_argument("--reason", default="手动", help="拉黑原因")
    ap.add_argument("--days", type=int, default=1, help="拉黑天数，0=永久")
    ap.add_argument("--white", default=None, help="设置白名单（逗号分隔，空串清空）")
    ap.add_argument("--clear-black", action="store_true", help="清空黑名单")
    args = ap.parse_args()

    if args.block:
        block(args.block.upper(), args.reason, args.days)
        print("已拉黑", args.block.upper())
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
