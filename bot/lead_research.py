#!/usr/bin/env python3
"""带单员行为研究：拉公开成交流，反推选币偏好与开平仓习惯。

数据源（公开，无需登录）：
    POST /bapi/futures/v1/friendly/future/copy-trade/lead-portfolio/trade-history
    body {"portfolioId": <leadPortfolioId>, "pageNumber": N, "pageSize": 100}

用法：
    python bot/lead_research.py --lead 4908633203782592768
    python bot/lead_research.py --lead 4908633203782592768 --context 15

输出：成交概况、币种集中度、回合（持仓）统计、加仓节奏、时段分布；
      带 --context 时再抽若干回合，拉 K 线看开仓前的涨跌幅与逆行深度。
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bot"))

import listen_copy as L  # noqa: E402
from net import http_json  # noqa: E402

DATA = ROOT / "bot" / "data"
PATH = "/bapi/futures/v1/friendly/future/copy-trade/lead-portfolio/trade-history"
FAPI = "https://fapi.binance.com"


def fetch_all(lead: str, refresh: bool = False, max_pages: int = 60) -> list[dict]:
    cache = DATA / f"lead-{lead}.json"
    if cache.exists() and not refresh:
        try:
            rows = json.loads(cache.read_text(encoding="utf-8"))
            if len(rows) > 200:          # 太少说明是旧的分页 bug，重拉
                print(f"用缓存 {cache.name}：{len(rows)} 笔（加 --refresh 可重拉）")
                return rows
        except Exception:  # noqa: BLE001
            pass
    out: list[dict] = []
    DATA.mkdir(parents=True, exist_ok=True)
    total = 0
    for page in range(1, max_pages + 1):
        # 注意：pageNumber > 1 必须带 timeRange，否则返回空
        body = {"portfolioId": lead, "pageNumber": page, "pageSize": 100,
                "timeRange": "90D"}
        rows: list[dict] = []
        for attempt in range(1, 6):
            try:
                j = L.api(PATH, "POST", body)
            except Exception as e:  # noqa: BLE001
                print(f"  第 {page} 页异常({attempt}): {e}")
                time.sleep(4 * attempt)
                continue
            code = j.get("code")
            data = j.get("data") or {}
            if code in (None, "000000"):
                rows = data.get("list") or []
                total = int(data.get("total") or total)
                break
            wait = 4 * attempt
            print(f"  第 {page} 页 {code}，{wait}s 后重试")
            time.sleep(wait)
        if not rows:
            print(f"  第 {page} 页没有数据，停止")
            break
        out.extend(rows)
        cache.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
        print(f"  第 {page} 页：{len(rows)} 笔（累计 {len(out)}/{total}）")
        if total and len(out) >= total:
            break
        if len(rows) < 100:
            break
        time.sleep(1.5)
    return out


def build_episodes(fills: list[dict]) -> list[dict]:
    """按币种把成交串成回合：realizedProfit==0 开/加，!=0 平/减。"""
    by_sym: dict[str, list[dict]] = defaultdict(list)
    for f in fills:
        by_sym[f["symbol"]].append(f)
    episodes = []
    for sym, rows in by_sym.items():
        rows.sort(key=lambda x: x["time"])
        cur = None
        for f in rows:
            is_open = float(f.get("realizedProfit") or 0) == 0
            if is_open:
                if cur is None:
                    cur = {"symbol": sym, "opens": [], "closes": []}
                    episodes.append(cur)
                cur["opens"].append(f)
            else:
                if cur is None:
                    continue
                cur["closes"].append(f)
                entry_qty = sum(float(x["qty"]) for x in cur["opens"])
                exit_qty = sum(float(x["qty"]) for x in cur["closes"])
                if exit_qty >= entry_qty * 0.999:
                    cur = None
    for e in episodes:
        e["start"] = e["opens"][0]["time"]
        e["end"] = e["closes"][-1]["time"] if e["closes"] else e["start"]
    return episodes


def report(fills: list[dict], episodes: list[dict]) -> list[dict]:
    print(f"\n=== 成交概况 ===")
    t0 = min(f["time"] for f in fills)
    t1 = max(f["time"] for f in fills)
    print(f"成交 {len(fills)} 笔 · {datetime.fromtimestamp(t0/1000):%Y-%m-%d %H:%M} → "
          f"{datetime.fromtimestamp(t1/1000):%Y-%m-%d %H:%M}")
    syms = Counter(f["symbol"] for f in fills)
    notional = Counter()
    for f in fills:
        notional[f["symbol"]] += float(f.get("quantity") or 0)
    print(f"币种 {len(syms)} 个；前 10 按成交笔数：")
    for s, n in syms.most_common(10):
        print(f"  {s:14s} {n:4d} 笔   名义合计 {notional[s]:>10.0f}U")
    print(f"前 3 个币占成交笔数 {sum(n for _, n in syms.most_common(3))/len(fills)*100:.0f}%")

    print(f"\n=== 回合（{len(episodes)} 个）===")
    holds, adds, pnls = [], [], []
    for e in episodes:
        holds.append((e["end"] - e["start"]) / 60000)
        adds.append(len(e["opens"]))
        pnls.append(sum(float(x.get("realizedProfit") or 0) for x in e["closes"]))
    if not pnls:
        return
    wins = sum(1 for p in pnls if p > 0)
    print(f"胜率 {wins/len(pnls)*100:.0f}%  合计盈亏 {sum(pnls):+.2f}U  "
          f"均值 {statistics.mean(pnls):+.3f}U  中位 {statistics.median(pnls):+.3f}U")
    print(f"持仓时长 中位 {statistics.median(holds):.1f} 分钟  "
          f"最长 {max(holds):.0f} 分钟")
    print(f"每回合开仓笔数 中位 {statistics.median(adds):.0f}  "
          f"最多 {max(adds)}  分布 {dict(sorted(Counter(adds).items()))}")

    # 加仓节奏：同一回合影内相邻开仓的间隔与价格步长
    gaps, steps = [], []
    for e in episodes:
        ops = sorted(e["opens"], key=lambda x: x["time"])
        for a, b in zip(ops, ops[1:]):
            gaps.append((b["time"] - a["time"]) / 1000)
            pa, pb = float(a["price"]), float(b["price"])
            if pa > 0:
                steps.append((pb / pa - 1) * 100)
    if gaps:
        print(f"加仓间隔 中位 {statistics.median(gaps):.0f}s  "
              f"价格步长 中位 {statistics.median(steps):+.2f}%  "
              f"（|步长| 中位 {statistics.median([abs(s) for s in steps]):.2f}%）")

    hours = Counter(datetime.fromtimestamp(f["time"] / 1000).hour for f in fills)
    print("\n成交时段（本地时间 小时:笔数）：")
    print("  " + "  ".join(f"{h:02d}:{n}" for h, n in sorted(hours.items())))

    # 方向偏好
    dirs = Counter(("空" if f["side"] == "SELL" else "多")
                   for f in fills if float(f.get("realizedProfit") or 0) == 0)
    print(f"\n开仓方向：{dict(dirs)}")
    return episodes


def context(episodes: list[dict], top_n: int) -> None:
    """抽若干回合，看开仓前的涨跌幅与持仓期最大逆行。"""
    picked = sorted(episodes, key=lambda e: -sum(float(x.get("quantity") or 0)
                                                 for x in e["opens"]))[:top_n]
    print(f"\n=== 抽 {len(picked)} 个回合看上下下文 ===")
    print(f"{'币种':12s} {'开仓':16s} {'方向':4s} {'开仓前1h':>9s} {'开仓前15m':>10s} "
          f"{'持仓':>6s} {'最大逆行':>8s} {'盈亏U':>8s}")
    for e in picked:
        sym = e["symbol"]
        start = e["start"]
        rows = http_json(f"{FAPI}/fapi/v1/klines?symbol={sym}&interval=1m"
                         f"&startTime={start - 4*3600*1000}&endTime={e['end'] + 60000}"
                         f"&limit=1000", timeout=20)
        if not isinstance(rows, list) or not rows:
            continue
        bars = [{"t": int(k[0]), "h": float(k[2]), "l": float(k[3]),
                 "c": float(k[4])} for k in rows]
        pre = [b for b in bars if b["t"] < start]
        if len(pre) < 60:
            continue
        entry_px = float(e["opens"][0]["price"])
        h1 = (entry_px / pre[-60]["c"] - 1) * 100 if len(pre) >= 60 else 0
        h15 = (entry_px / pre[-15]["c"] - 1) * 100 if len(pre) >= 15 else 0
        side = e["opens"][0]["side"]
        during = [b for b in bars if start <= b["t"] <= e["end"]]
        adv = 0.0
        if during:
            if side == "SELL":
                worst = max(b["h"] for b in during)
                adv = (worst / entry_px - 1) * 100
            else:
                worst = min(b["l"] for b in during)
                adv = (entry_px / worst - 1) * 100
        pnl = sum(float(x.get("realizedProfit") or 0) for x in e["closes"])
        held = (e["end"] - start) / 60000
        print(f"{sym:12s} {datetime.fromtimestamp(start/1000):%m-%d %H:%M:%S} "
              f"{'空' if side == 'SELL' else '多':4s} {h1:+8.2f}% {h15:+9.2f}% "
              f"{held:5.0f}m {adv:7.2f}% {pnl:+8.3f}")


def main() -> None:
    ap = argparse.ArgumentParser(description="带单员行为研究")
    ap.add_argument("--lead", required=True, help="带单员的 leadPortfolioId")
    ap.add_argument("--context", type=int, default=0, help="抽多少个回合看 K 线上下文")
    ap.add_argument("--refresh", action="store_true", help="忽略缓存，重新拉全量")
    args = ap.parse_args()
    L.load_session()
    fills = fetch_all(args.lead, refresh=args.refresh)
    if not fills:
        print("没有拉到成交数据")
        return
    episodes = build_episodes(fills)
    report(fills, episodes)
    if args.context:
        context(episodes, args.context)


if __name__ == "__main__":
    main()
