#!/usr/bin/env python3
"""复制运行器：监听成交 + 计算/下单，按配置决定影子、干跑还是实盘。

用法：
    python bot/copy_run.py
    python bot/copy_run.py --only 星辰社区-海

输出：
    bot/data/intents-YYYYMMDD.jsonl   每笔意图（含目标数量、估算成交价、滑点）
    bot/data/events-YYYYMMDD.jsonl    原始成交

统计：python bot/copy_report.py
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bot"))

import listen_copy as L  # noqa: E402
from copy_engine import CopyEngine  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="只监听这些带单员（名字/ID，逗号分隔）")
    args = ap.parse_args()

    engine = CopyEngine()
    L.set_signal_hook(engine.on_signal)
    cfg = engine.cfg
    exec_mode = cfg.get("mode", "shadow")
    from version import __version__
    print(f"[copy] v{__version__} 执行模式={exec_mode}  比例={cfg['ratio_mode']} k={cfg['k']} "
          f"权益={cfg['exec_equity']}U 单笔≤{cfg['max_order_notional']}U "
          f"滑点 BTC/ETH={cfg.get('slippage_btc_eth_pct', 0.5)}% "
          f"其他={cfg.get('slippage_default_pct', 1.5)}%", flush=True)

    tokens = L.parse_tokens(args.only)
    try:
        asyncio.run(L.run(tokens, False))
    except KeyboardInterrupt:
        print("[copy] bye", flush=True)


if __name__ == "__main__":
    main()
