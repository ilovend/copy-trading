#!/usr/bin/env python3
"""影子模式日报：看滑点、取整误差、被风控拒掉的比例。

用法：
    python bot/shadow_report.py            # 今天
    python bot/shadow_report.py 20261111   # 指定日期
"""
from __future__ import annotations

import json
import statistics
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "bot" / "data"


def main() -> None:
    day = sys.argv[1] if len(sys.argv) > 1 else datetime.now().strftime("%Y%m%d")
    path = DATA / f"intents-{day}.jsonl"
    if not path.exists():
        print(f"没有数据：{path}")
        return

    intents, rejects = [], []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        (rejects if r.get("kind") == "reject" else intents).append(r)

    print(f"=== {day} 影子日报 ===")
    print(f"意图 {len(intents)} 笔，被拒 {len(rejects)} 笔")

    if intents:
        slips = [r["slippage_bps"] for r in intents]
        notionals = [r["notional"] for r in intents]
        ages = [r["age_ms"] for r in intents]
        short = [r for r in intents if not r.get("depth_ok")]
        print("\n滑点 (bps，正=对你不利)")
        print(f"  均值 {statistics.mean(slips):+.1f}  中位 {statistics.median(slips):+.1f}  "
              f"最差 {max(slips):+.1f}  最好 {min(slips):+.1f}")
        print(f"\n单笔名义 均值 {statistics.mean(notionals):.2f}U  合计 {sum(notionals):.2f}U")
        print(f"信号年龄 均值 {statistics.mean(ages):.0f}ms  最大 {max(ages):.0f}ms")
        if short:
            print(f"盘口深度不足 {len(short)} 笔（目标量吃不满）")

        over = [s for s in slips if s > 10]
        print(f"滑点 >10bps 的比例：{len(over)}/{len(slips)} = {len(over)/len(slips)*100:.0f}%")

    if rejects:
        from collections import Counter
        c = Counter(r["reason"] for r in rejects)
        print("\n拒绝原因：")
        for k, v in c.most_common():
            print(f"  {k:12s} {v}")


if __name__ == "__main__":
    main()
