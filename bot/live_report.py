#!/usr/bin/env python3
"""实盘/影子当日分析：读策略台账 strategy-YYYYMMDD.jsonl。

用法：
    python bot/live_report.py              # 今天
    python bot/live_report.py --date 20261112 --follow   # 每 30 秒刷新

输出：
    信号/拒绝、maker 成交率、入场滑点、出场原因、盈亏、胜率、持仓时长、最大回撤
"""
from __future__ import annotations

import argparse
import json
import statistics
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "bot" / "data"


def load(day: str) -> list[dict]:
    path = DATA / f"strategy-{day}.jsonl"
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except Exception:  # noqa: BLE001
            continue
    return rows


def report(day: str) -> None:
    rows = load(day)
    print(f"=== 策略实盘分析 · {day} ===")
    if not rows:
        print("今天还没有任何记录（进程没跑，或者还没出信号）")
        return

    opens = [r for r in rows if r["kind"] == "open"]
    adds = [r for r in rows if r["kind"] == "add"]
    parts = [r for r in rows if r["kind"] == "close_partial"]
    closes = [r for r in rows if r["kind"] == "close"]
    rejects = [r for r in rows if r["kind"] == "reject"]

    # maker 成交率
    unfilled = [r for r in rejects if r.get("reason") == "maker_unfilled"]
    attempted = len(opens) + len(unfilled)
    fill_rate = len(opens) / attempted * 100 if attempted else 0

    # 入场滑点：entry 相对信号价（做空，正=更差）
    slips = []
    for r in opens:
        sig = r.get("signal") or {}
        ref = float(sig.get("close") or 0)
        entry = float(r.get("entry") or 0)
        if ref > 0 and entry > 0:
            slips.append((entry / ref - 1) * 100)

    pnl = sum(float(r.get("pnl") or 0) for r in parts)
    wins = sum(1 for r in closes if float(r.get("pnl_total") or 0) > 0)
    losses = len(closes) - wins
    holds = [float(r.get("held_s") or 0) for r in closes]
    reasons = Counter(r.get("reason") for r in closes)

    # 逐笔累计最大回撤
    curve, acc, peak, max_dd = [], 0.0, 0.0, 0.0
    for r in parts:
        acc += float(r.get("pnl") or 0)
        curve.append(acc)
        peak = max(peak, acc)
        max_dd = min(max_dd, acc - peak)

    print(f"信号：开仓 {len(opens)} · 加仓 {len(adds)} · 平仓 {len(closes)} · "
          f"拒绝 {len(rejects)}")
    print(f"maker 成交率：{len(opens)}/{attempted} = "
          f"{fill_rate:.0f}%" if attempted else "maker 成交率：暂无挂单记录")
    if slips:
        print(f"入场滑点（相对信号价）：中位 {statistics.median(slips):+.3f}%  "
              f"最差 {max(slips):+.3f}%")
    print(f"已实现盈亏：{pnl:+.3f}U   胜/负：{wins}/{losses}   "
          f"最大回撤：{max_dd:+.3f}U")
    if holds:
        print(f"持仓时长：中位 {statistics.median(holds):.0f}s  最长 {max(holds):.0f}s")
    if reasons:
        print(f"出场原因：{dict(reasons)}")
    if rejects:
        top = Counter(r.get("reason") for r in rejects).most_common(6)
        print(f"拒绝原因：{dict(top)}")

    # 每条仓位明细
    if closes:
        print("\n明细（最近的 10 个回合）：")
        for r in closes[-10:]:
            print(f"  {r.get('symbol'):12s} {r.get('reason'):16s} "
                  f"{float(r.get('pnl_total') or 0):+7.3f}U "
                  f"{float(r.get('entries') or 0):.0f} 笔 "
                  f"{float(r.get('held_s') or 0):.0f}s")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=datetime.now().strftime("%Y%m%d"))
    ap.add_argument("--follow", action="store_true", help="每 30 秒刷新")
    args = ap.parse_args()
    while True:
        print("\033[2J\033[H", end="")
        report(args.date)
        if not args.follow:
            return
        time.sleep(30)


if __name__ == "__main__":
    main()
