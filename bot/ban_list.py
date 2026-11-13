#!/usr/bin/env python3
"""每日拉黑名单：垂直拉升的币当天不再交易，次日自动解除。

用法：
    python bot/ban_list.py            # 查看今天的拉黑名单
    python bot/ban_list.py --clear AKEUSDT
    python bot/ban_list.py --clear-all
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from pathlib import Path

PATH = Path(__file__).resolve().parent / "data" / "strategy-bans.json"


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def load() -> dict[str, dict]:
    """返回今天的名单；日期不是今天则视为空（自动解除）。"""
    try:
        data = json.loads(PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    if data.get("date") != _today():
        return {}
    return dict(data.get("symbols") or {})


def save(symbols: dict[str, dict]) -> None:
    PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps({"date": _today(), "symbols": symbols},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(PATH)


def is_banned(symbol: str) -> bool:
    return symbol in load()


def ban(symbol: str, reason: str = "", max_bans: int = 0) -> bool:
    """拉黑；名额已满返回 False（但调用方仍应跳过这一单）。"""
    syms = load()
    if symbol in syms:
        return True
    if max_bans > 0 and len(syms) >= max_bans:
        return False
    syms[symbol] = {"reason": reason, "ts": int(time.time())}
    save(syms)
    return True


def release(symbol: str) -> bool:
    syms = load()
    if symbol not in syms:
        return False
    syms.pop(symbol, None)
    save(syms)
    return True


def clear_all() -> int:
    n = len(load())
    save({})
    return n


def main() -> None:
    ap = argparse.ArgumentParser(description="每日拉黑名单")
    ap.add_argument("--clear", default="", help="解除某个币")
    ap.add_argument("--clear-all", action="store_true", help="清空今天全部")
    args = ap.parse_args()
    if args.clear:
        print("解除" if release(args.clear) else "不在名单里", args.clear)
        return
    if args.clear_all:
        print(f"已清空 {clear_all()} 个")
        return
    syms = load()
    print(f"今天（{_today()}）拉黑 {len(syms)} 个：")
    for s, v in syms.items():
        t = datetime.fromtimestamp(v.get("ts", 0)).strftime("%H:%M")
        print(f"  {s:14s} {t}  {v.get('reason', '')}")


if __name__ == "__main__":
    main()
