#!/usr/bin/env python3
"""影子模式：跑完整监听 + 复制计算，但不下单。

用法：
    python bot/shadow_copy.py
    python bot/shadow_copy.py --only 星辰社区-海

输出：
    bot/data/intents-YYYYMMDD.jsonl   每笔意图（含目标数量、估算成交价、滑点）
    bot/data/events-YYYYMMDD.jsonl    原始成交

统计：python bot/shadow_report.py
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
    print(f"[shadow] mode={cfg['ratio_mode']} k={cfg['k']} exec_equity={cfg['exec_equity']}U "
          f"max_order={cfg['max_order_notional']}U slippage_cap={cfg['max_slippage_pct']}%",
          flush=True)

    tokens = L.parse_tokens(args.only)
    try:
        asyncio.run(L.run(tokens, False))
    except KeyboardInterrupt:
        print("[shadow] bye", flush=True)


if __name__ == "__main__":
    main()
