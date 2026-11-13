#!/usr/bin/env python3
"""兼容入口：拉黑名单已合并进 symbol_filter.py（白名单 + 黑名单 + 自动拉黑）。

保留旧函数名，避免旧脚本/测试引用报错：
    is_banned / ban / release / clear_all / load
"""
from __future__ import annotations

from symbol_filter import (PATH, block, clear_blacklist, is_allowed, load,  # noqa: F401
                           main, set_whitelist, unblock)


def is_banned(symbol: str) -> bool:
    return not is_allowed(symbol)[0]


def ban(symbol: str, reason: str = "", max_bans: int = 0) -> bool:
    if max_bans and len(load().get("blacklist") or {}) >= max_bans:
        return False
    block(symbol, reason, days=1)
    return True


def release(symbol: str) -> bool:
    return unblock(symbol)


def clear_all() -> int:
    return clear_blacklist()


if __name__ == "__main__":
    main()
