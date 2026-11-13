#!/usr/bin/env python3
"""实盘互斥锁：同一时刻只允许一个「会真实下单」的进程。

复制（copy_run）和自研策略（strategy_run）共用同一个执行账户，
如果同时以 live 运行，可能对同一币各下一单、互相干扰。
两边在 live 启动前都抢这个本地端口，抢不到就退出。

影子 / 干跑不占锁（它们不下单）。
"""
from __future__ import annotations

import socket

PORT = 47620
_sock: socket.socket | None = None


def acquire_trade_lock(owner: str) -> bool:
    global _sock
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", PORT))
        s.listen(1)
    except OSError:
        try:
            s.close()
        except Exception:  # noqa: BLE001
            pass
        print(f"[{owner}] ⛔ 已有实盘进程在运行（本地端口 {PORT} 被占用）。"
              f"复制与策略不能同时在 live 下单，请先停止另一个。", flush=True)
        return False
    _sock = s
    print(f"[{owner}] ✅ 已获取实盘互斥锁（端口 {PORT}）", flush=True)
    return True


def release_trade_lock() -> None:
    global _sock
    if _sock is not None:
        try:
            _sock.close()
        except Exception:  # noqa: BLE001
            pass
        _sock = None


def is_trade_lock_busy() -> bool:
    """只探测，不占锁。"""
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", PORT))
        s.listen(1)
        return False
    except OSError:
        return True
    finally:
        try:
            s.close()
        except Exception:  # noqa: BLE001
            pass
