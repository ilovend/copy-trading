#!/usr/bin/env python3
"""实盘 / 干跑 / 影子 对比报表：从策略台账生成 CSV + Excel。

数据源：bot/data/strategy-YYYYMMDD.jsonl
- open / add / close_partial / close 按 symbol 还原成一笔笔「回合」
- close 的 pnl_total 已包含该回合的 partial，不会重复计
- 手续费按名义 0.1%（往返 taker）估算；交易所整仓平仓的 pnl_total 已含费，不重复扣

用法：
    python bot/report_compare.py --days 7
    python bot/report_compare.py --days 30 --exchange
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "bot" / "data"
FEE_RATE = 0.001          # 往返 0.1%，按名义估算

MODE_LABEL = {"live": "实盘", "live_dry": "干跑", "shadow": "影子", "?": "未知"}
REASON_LABEL = {"basket_tp": "止盈", "basket_stop": "止损", "basket_timeout": "超时",
                "stop_loss": "止损", "take_profit": "止盈", "timeout": "超时",
                "trail": "追踪", "tp1": "止盈1", "tp2": "止盈2",
                "cutoff": "收盘", "exchange_tp": "交易所止盈",
                "exchange_sl": "交易所止损", "external": "外部平仓"}


def load_episodes(days: int) -> list[dict]:
    since = datetime.now() - timedelta(days=days)
    files = sorted(DATA.glob("strategy-*.jsonl"))
    rows: list[dict] = []
    for f in files:
        try:
            day = datetime.strptime(f.stem.split("-")[-1], "%Y%m%d")
        except ValueError:
            continue
        if day < since.replace(hour=0, minute=0, second=0, microsecond=0):
            continue
        for line in f.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except Exception:  # noqa: BLE001
                pass
    rows.sort(key=lambda r: int(r.get("ts") or 0))

    episodes: list[dict] = []
    open_by_sym: dict[str, dict] = {}

    def finish(ep: dict, ts: int | None, reason: str = "open",
               total: float | None = None) -> None:
        ep["close_ts"] = ts
        ep["reason"] = reason
        ep["gross"] = float(total if total is not None else ep["partial"])
        ep["notional"] = sum(e["qty"] * e["price"] for e in ep["entries"])
        ep["adds"] = max(0, len(ep["entries"]) - 1)
        ep["hold_min"] = round(((ts or ep["open_ts"]) - ep["open_ts"]) / 60000, 1)
        ep["win"] = ep["gross"] > 0
        if reason == "open":
            ep["status"] = "未平"
        elif reason == "reopen":
            ep["status"] = "中断"
        elif abs(ep["gross"]) < 1e-12 and ep["partial"] == 0 and (
                reason.startswith("exchange") or "交易所" in str(ep.get("notes") or "")):
            ep["status"] = "未知"        # 交易所平了但没取回成交明细
        else:
            ep["status"] = "已平"
        episodes.append(ep)

    for r in rows:
        kind = r.get("kind")
        sym = r.get("symbol") or "?"
        ts = int(r.get("ts") or 0)
        if kind == "open":
            if sym in open_by_sym:
                finish(open_by_sym.pop(sym), ts, "reopen")
            open_by_sym[sym] = {
                "symbol": sym, "mode": r.get("mode") or "?",
                "open_ts": ts, "entries": [], "partial": 0.0,
                "partial_count": 0, "fee_included": False, "notes": "",
            }
            qty = float(r.get("qty") or 0)
            px = float(r.get("entry") or 0)
            if qty > 0 and px > 0:
                open_by_sym[sym]["entries"].append({"qty": qty, "price": px})
        elif kind in ("add", "close_partial", "close"):
            ep = open_by_sym.get(sym)
            if ep is None:
                continue
            if kind == "add":
                qty = float(r.get("qty") or 0)
                px = float(r.get("price") or 0)
                if qty > 0 and px > 0:
                    ep["entries"].append({"qty": qty, "price": px})
            elif kind == "close_partial":
                ep["partial"] += float(r.get("pnl") or 0)
                ep["partial_count"] += 1
            else:
                open_by_sym.pop(sym, None)
                notes = str(r.get("notes") or "")
                ep["notes"] = notes
                reason = str(r.get("reason") or "external")
                total = r.get("pnl_total")
                # 交易所整仓平仓的 pnl_total 来自 userTrades，已扣手续费
                if reason.startswith("exchange") or "交易所" in notes:
                    ep["fee_included"] = True
                finish(ep, ts, reason,
                       float(total) if total is not None else None)

    for ep in open_by_sym.values():
        finish(ep, None, "open", ep["partial"])
    episodes.sort(key=lambda e: e["open_ts"])
    return episodes


def exchange_daily(days: int) -> dict[str, float]:
    """实盘每日净额：REALIZED_PNL + COMMISSION + FUNDING_FEE。"""
    try:
        sys.path.insert(0, str(ROOT / "bot"))
        from exec_client import ExecClient
        ex = ExecClient()
    except BaseException:  # noqa: BLE001
        return {}
    out: dict[str, float] = {}
    for i in range(days + 1):
        day = (datetime.now() - timedelta(days=i)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        start = int(day.timestamp() * 1000)
        end = start + 86_400_000
        try:
            rows = ex.income(start, 1000)
        except Exception:  # noqa: BLE001
            continue
        net = sum(float(r.get("income") or 0) for r in rows
                  if r.get("incomeType") in ("REALIZED_PNL", "COMMISSION", "FUNDING_FEE")
                  and start <= int(r.get("time") or 0) < end)
        out[day.strftime("%Y-%m-%d")] = round(net, 4)
    return out


def summarize(eps: list[dict], exchange: dict[str, float]) -> list[dict]:
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for e in eps:
        day = datetime.fromtimestamp(e["open_ts"] / 1000).strftime("%Y-%m-%d")
        groups[(day, e["mode"])].append(e)
    out = []
    for (day, mode), items in sorted(groups.items()):
        gross = sum(e["gross"] for e in items)
        fee = sum(0.0 if e["fee_included"] else e["notional"] * FEE_RATE
                  for e in items)
        wins = sum(1 for e in items if e["win"])
        reasons = Counter(REASON_LABEL.get(e["reason"], e["reason"]) for e in items)
        out.append({
            "日期": day, "模式": MODE_LABEL.get(mode, mode), "笔数": len(items),
            "胜率": round(wins / len(items) * 100, 1),
            "毛利U": round(gross, 4), "手续费U": round(fee, 4),
            "账本净利U": round(gross - fee, 4),
            "交易所净额U": exchange.get(day) if mode == "live" else None,
            "净利U": (exchange.get(day) if (mode == "live" and day in exchange)
                      else round(gross - fee, 4)),
            "平均每笔U": round(((exchange.get(day) if (mode == "live" and day in exchange)
                                else (gross - fee)) / len(items)), 4),
            "平均持仓分钟": round(sum(e["hold_min"] for e in items) / len(items), 1),
            "加仓次数": sum(e["adds"] for e in items),
            "止盈": reasons.get("止盈", 0) + reasons.get("止盈1", 0) + reasons.get("止盈2", 0),
            "止损": reasons.get("止损", 0) + reasons.get("交易所止损", 0),
            "超时": reasons.get("超时", 0),
            "其他": len(items) - reasons.get("止盈", 0) - reasons.get("止盈1", 0)
                    - reasons.get("止盈2", 0) - reasons.get("止损", 0)
                    - reasons.get("交易所止损", 0) - reasons.get("超时", 0),
            "备注": "",
        })
    return out


def compare_rows(summary: list[dict]) -> list[dict]:
    by_day: dict[str, dict[str, dict]] = defaultdict(dict)
    for r in summary:
        by_day[r["日期"]][r["模式"]] = r
    out = []
    for day in sorted(by_day):
        live = by_day[day].get("实盘")
        dry = by_day[day].get("干跑")
        if not live and not dry:
            continue
        out.append({
            "日期": day,
            "实盘笔数": live["笔数"] if live else 0,
            "实盘净利U": live["净利U"] if live else 0.0,
            "实盘胜率": live["胜率"] if live else 0.0,
            "干跑笔数": dry["笔数"] if dry else 0,
            "干跑净利U": dry["净利U"] if dry else 0.0,
            "干跑胜率": dry["胜率"] if dry else 0.0,
            "净利差(实盘-干跑)": round((live["净利U"] if live else 0.0)
                                     - (dry["净利U"] if dry else 0.0), 4),
        })
    return out


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def _ensure_openpyxl() -> bool:
    """优先当前解释器；没有就找 DSH 运行时里的 openpyxl。"""
    try:
        import openpyxl  # noqa: F401
        return True
    except ImportError:
        pass
    bases = [Path.home() / ".dsh" / "dsh-runtimes"]
    for base in bases:
        for site in base.glob("*/dependencies/python/Lib/site-packages"):
            if not (site / "openpyxl").exists():
                continue
            sys.path.append(str(site))
            try:
                import openpyxl  # noqa: F401
                return True
            except ImportError:
                sys.path.pop()
    return False


def write_xlsx(path: Path, summary: list[dict], detail: list[dict],
               compare: list[dict]) -> None:
    if not _ensure_openpyxl():
        print("未安装 openpyxl，跳过 Excel（CSV 已生成）")
        return
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    head_fill = PatternFill("solid", fgColor="1F4E78")
    head_font = Font(color="FFFFFF", bold=True)

    def style_header(ws, row: int = 1) -> None:
        for cell in ws[row]:
            cell.fill = head_fill
            cell.font = head_font
            cell.alignment = Alignment(horizontal="center", vertical="center")

    def autofit(ws) -> None:
        for col in ws.columns:
            width = max(len(str(c.value or "")) for c in col)
            ws.column_dimensions[get_column_letter(col[0].column)].width = min(22, max(9, width + 3))

    ws = wb.active
    ws.title = "汇总"
    cols = list(summary[0].keys()) if summary else ["日期"]
    ws.append(cols)
    for r in summary:
        ws.append([r[c] for c in cols])
    style_header(ws)
    autofit(ws)
    ws.freeze_panes = "A2"
    for row in ws.iter_rows(min_row=2, min_col=5, max_col=7):
        for c in row:
            c.number_format = "+0.0000;-0.0000"

    if compare:
        ws.append([])
        start = ws.max_row + 1
        ws.cell(row=start, column=1, value="实盘 vs 干跑").font = Font(bold=True, size=12)
        cols2 = list(compare[0].keys())
        ws.append(cols2)
        style_header(ws, ws.max_row)
        for r in compare:
            ws.append([r[c] for c in cols2])
        autofit(ws)

    ws2 = wb.create_sheet("明细")
    dcols = ["日期", "模式", "币种", "开仓时间", "持仓分钟", "笔数", "名义U",
             "毛利U", "估算费U", "净利U", "状态", "出场", "已含费"]
    ws2.append(dcols)
    for e in detail:
        ws2.append([
            datetime.fromtimestamp(e["open_ts"] / 1000).strftime("%Y-%m-%d"),
            MODE_LABEL.get(e["mode"], e["mode"]), e["symbol"],
            datetime.fromtimestamp(e["open_ts"] / 1000).strftime("%H:%M:%S"),
            e["hold_min"], len(e["entries"]), round(e["notional"], 4),
            round(e["gross"], 4),
            0.0 if e["fee_included"] else round(e["notional"] * FEE_RATE, 4),
            round(e["gross"] - (0.0 if e["fee_included"] else e["notional"] * FEE_RATE), 4),
            e.get("status", "已平"),
            REASON_LABEL.get(e["reason"], e["reason"]), "是" if e["fee_included"] else "",
        ])
    style_header(ws2)
    autofit(ws2)
    ws2.freeze_panes = "A2"
    for row in ws2.iter_rows(min_row=2, min_col=8, max_col=10):
        for c in row:
            c.number_format = "+0.0000;-0.0000"

    ws3 = wb.create_sheet("说明")
    notes = [
        ["项目", "说明"],
        ["数据源", "bot/data/strategy-YYYYMMDD.jsonl（引擎台账）"],
        ["毛利", "按开仓/加仓记录与平仓 pnl_total 还原，未扣本地估算手续费"],
        ["手续费", "按名义 0.1% 往返估算；交易所整仓平仓的 pnl_total 已含费，不重复扣"],
        ["实盘", "真实成交；每日盈亏也可用 /fapi/v1/income 交叉核对"],
        ["干跑", "live_dry：只打印订单，不发真单"],
        ["影子", "shadow：只记录信号，不模拟成交"],
        ["一笔", "从 open 到 close 的完整回合；close 的 pnl_total 已含 partial"],
    ]
    for r in notes:
        ws3.append(r)
    style_header(ws3)
    autofit(ws3)

    wb.save(path)


def main() -> None:
    ap = argparse.ArgumentParser(description="实盘/干跑对比报表")
    ap.add_argument("--days", type=int, default=7, help="回看天数")
    ap.add_argument("--out", default=str(ROOT / "reports"), help="输出目录")
    ap.add_argument("--no-exchange", action="store_true", help="不查交易所当日净额")
    ap.add_argument("--modes", default="live,live_dry",
                    help="统计模式：live,live_dry 或 all（含 shadow）")
    args = ap.parse_args()

    eps = load_episodes(args.days)
    modes = None if args.modes.strip().lower() == "all" else {
        x.strip() for x in args.modes.split(",") if x.strip()}
    if modes:
        eps = [e for e in eps if e["mode"] in modes]
    closed = [e for e in eps if e.get("status") == "已平"]
    if not closed:
        print("台账里没有已平仓的回合（未平/中断/未知不参与统计）")
        return
    exchange = {} if args.no_exchange else exchange_daily(args.days)
    summary = summarize(closed, exchange)
    # 当天有实盘但没有干跑成交时，补一行 0，方便看对比
    for day in sorted({r["日期"] for r in summary}):
        if any(r["日期"] == day and r["模式"] == "实盘" for r in summary) and \
                not any(r["日期"] == day and r["模式"] == "干跑" for r in summary):
            summary.append({"日期": day, "模式": "干跑", "笔数": 0, "胜率": 0.0,
                            "毛利U": 0.0, "手续费U": 0.0, "账本净利U": 0.0,
                            "交易所净额U": None, "净利U": 0.0, "平均每笔U": 0.0,
                            "平均持仓分钟": 0.0, "加仓次数": 0, "止盈": 0,
                            "止损": 0, "超时": 0, "其他": 0, "备注": "暂无已平仓"})
    summary.sort(key=lambda r: (r["日期"], r["模式"]))
    compare = compare_rows(summary)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d")
    s_csv = out / f"strategy-compare-summary-{stamp}.csv"
    d_csv = out / f"strategy-compare-detail-{stamp}.csv"
    xlsx = out / f"strategy-compare-{stamp}.xlsx"
    write_csv(s_csv, summary)
    write_csv(d_csv, [{
        "日期": datetime.fromtimestamp(e["open_ts"] / 1000).strftime("%Y-%m-%d"),
        "模式": MODE_LABEL.get(e["mode"], e["mode"]), "币种": e["symbol"],
        "开仓时间": datetime.fromtimestamp(e["open_ts"] / 1000).strftime("%H:%M:%S"),
        "持仓分钟": e["hold_min"], "笔数": len(e["entries"]),
        "名义U": round(e["notional"], 4), "毛利U": round(e["gross"], 4),
        "估算费U": 0.0 if e["fee_included"] else round(e["notional"] * FEE_RATE, 4),
        "净利U": round(e["gross"] - (0.0 if e["fee_included"] else e["notional"] * FEE_RATE), 4),
        "状态": e.get("status", "已平"),
        "出场": REASON_LABEL.get(e["reason"], e["reason"]),
        "已含费": "是" if e["fee_included"] else "",
    } for e in eps])
    write_xlsx(xlsx, summary, eps, compare)
    print(f"已平仓 {len(closed)} 笔 · 未平 {len(eps) - len(closed)} 笔 · "
          f"汇总 {len(summary)} 行 · 对比 {len(compare)} 行")
    print(f"CSV  {s_csv}")
    print(f"CSV  {d_csv}")
    print(f"XLSX {xlsx}")


if __name__ == "__main__":
    main()
