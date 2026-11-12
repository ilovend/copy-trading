#!/usr/bin/env python3
"""WebSocket 探针：判断某个币安 WS 地址到底有没有推数据。

背景：币安换 WS 入口时，旧地址**照样握手成功、日志显示已连接，但一条不推**。
只看「连接成功」会误判，必须数消息条数。

用法：
    python bot/ws_probe.py                 # 用 btcusdt 测所有候选入口
    python bot/ws_probe.py --stream akeusdt@kline_1m
    python bot/ws_probe.py --url "wss://fstream.binance.com/market/stream?streams=btcusdt@kline_1m"

最快拿到「正确地址」的办法：打开 binance.com 合约页面 → F12 → Network → WS，
点开连接看 Request URL，直接复制过来测。
"""
from __future__ import annotations

import argparse
import asyncio
import time

import websockets

FUTURES = "wss://fstream.binance.com"
CANDIDATES = [
    ("合约·新入口 /market/stream", FUTURES + "/market/stream?streams={s}"),
    ("合约·旧组合流 /stream", FUTURES + "/stream?streams={s}"),
    ("合约·旧单流 /ws", FUTURES + "/ws/{s}"),
    ("现货·单流", "wss://stream.binance.com:9443/ws/{s}"),
    ("合约测试网", "wss://fstream.binancefuture.com/ws/{s}"),
]


async def probe(url: str, seconds: float) -> tuple[int, str]:
    n = 0
    first = ""
    try:
        async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
            t0 = time.time()
            while time.time() - t0 < seconds:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=2)
                except asyncio.TimeoutError:
                    continue
                n += 1
                if not first:
                    first = raw[:110]
    except Exception as e:  # noqa: BLE001
        return -1, f"{type(e).__name__}: {e}"
    return n, first


async def main() -> None:
    ap = argparse.ArgumentParser(description="币安 WebSocket 探针")
    ap.add_argument("--stream", default="btcusdt@kline_1m")
    ap.add_argument("--url", default="")
    ap.add_argument("--seconds", type=float, default=8)
    args = ap.parse_args()

    if args.url:
        n, first = await probe(args.url, args.seconds)
        print(f"{args.seconds:.0f}s 收到 {n} 条" if n >= 0 else f"连接失败：{first}")
        if n > 0:
            print("首条:", first)
        return

    print(f"探针流：{args.stream} · 每个 {args.seconds:.0f}s\n")
    for label, tpl in CANDIDATES:
        url = tpl.format(s=args.stream)
        n, first = await probe(url, args.seconds)
        if n < 0:
            print(f"  {label:24s} 连接失败  {first}")
        else:
            flag = "✅" if n > 0 else "❌"
            print(f"  {label:24s} {flag} {n:3d} 条  {first[:70]}")
    print("\n结论：只有 ✅ 的入口可用；❌ 表示握手成功但不推数据（入口已迁移）。")


if __name__ == "__main__":
    asyncio.run(main())
