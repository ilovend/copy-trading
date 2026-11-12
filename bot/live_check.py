#!/usr/bin/env python3
"""实盘预检：不下真单，只验证密钥、权益、持仓模式、盘口与下单参数。

用法：
    python bot/live_check.py --preset fade_live
    python bot/live_check.py --preset fade_live --symbols 5

检查项（任何 FAIL 都会阻止上线）：
    1. API Key / Secret 是否配置
    2. 账户权益是否够覆盖最大仓位（单笔 × (1+加仓) × 同时持仓）
    3. 持仓模式（单向/双向）与当前持仓
    4. 候选币的盘口价差、最小名义、步长
    5. 用 /fapi/v1/order/test 验证一单的签名与参数（不会真的下单）
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bot"))

import strategy_config as SC  # noqa: E402
import strategy_run as RS  # noqa: E402
from copy_engine import http_json  # noqa: E402
from exec_client import ExecClient, load_keys  # noqa: E402

FAPI = "https://fapi.binance.com"


def main() -> None:
    ap = argparse.ArgumentParser(description="策略实盘预检")
    ap.add_argument("--preset", default="fade_live")
    ap.add_argument("--symbols", type=int, default=3, help="抽几个候选币做下单预检")
    args = ap.parse_args()

    cfg = SC.apply_preset(SC.load(), args.preset)
    trade = cfg["trade"]
    basket = cfg["basket"]
    fade = cfg["fade"]
    notional = float(basket.get("notional", trade.get("notional", 5)))
    max_adds = int(basket.get("max_adds", 0)) if basket.get("enabled") else 0
    max_pos = int(trade.get("max_positions", 1))
    need = notional * (1 + max_adds) * max_pos
    blockers: list[str] = []
    warnings: list[str] = []

    print(f"=== 预检 · 预设 {args.preset} ===")
    print(f"模式 {cfg.get('mode')} · 周期 {cfg.get('interval')} · "
          f"单笔 {notional:g}U × (1+{max_adds}) 次 · 同时 {max_pos} 个币 "
          f"→ 最大占用 {need:g}U")
    if fade.get("enabled"):
        print(f"信号：1h ≥{fade.get('entry_pct')}% 且 15m ≥{fade.get('entry_15m_pct')}% 做空")
    print(f"出场：止盈 {basket.get('basket_tp_pct')}% · 硬止损 {basket.get('hard_stop_pct')}% "
          f"· 超时 {basket.get('max_hold_s')}s · 入场 {trade.get('entry_mode')} "
          f"{trade.get('entry_timeout_s')}s · 止盈 {trade.get('tp_mode')}")
    print(f"风控：当日亏损上限 {trade.get('max_daily_loss')}U · 收盘强平 {trade.get('cutoff')}\n")

    # 1) 密钥
    key, secret = load_keys()
    if not key or not secret:
        blockers.append("没有配置 EXEC_API_KEY / EXEC_API_SECRET（.env 或 GUI 复制页）")
        print("FAIL  密钥未配置")
        _summary(blockers, warnings)
        return
    print(f"PASS  密钥已配置（{key[:6]}…{key[-4:]}）")

    ex = ExecClient(key, secret)
    # 2) 账户
    try:
        acc = ex.account()
        equity = float(acc.get("totalMarginBalance") or 0)
        avail = float(acc.get("availableBalance") or 0)
        print(f"PASS  账户权益 {equity:.2f}U  可用 {avail:.2f}U")
        if equity <= 0:
            blockers.append("合约账户权益为 0，先从现货划转")
        elif equity < need * 1.2:
            blockers.append(f"权益 {equity:.2f}U 不够覆盖最大占用 {need:g}U 的 1.2 倍")
    except Exception as e:  # noqa: BLE001
        blockers.append(f"账户查询失败：{str(e)[:160]}")
        _summary(blockers, warnings)
        return

    # 3) 持仓模式与现有持仓
    try:
        hedge = ex.position_mode()
        pos = ex.positions()
        print(f"PASS  持仓模式 {'双向(hedge)' if hedge else '单向(one-way)'} · "
              f"现有持仓 {len(pos)} 个")
        if pos:
            warnings.append(f"账户已有 {len(pos)} 个持仓，策略会跳过同币开仓")
    except Exception as e:  # noqa: BLE001
        warnings.append(f"持仓模式查询失败：{str(e)[:120]}")

    # 4) 候选币与下单参数
    try:
        syms = RS.scan_universe(cfg)[: args.symbols]
    except Exception as e:  # noqa: BLE001
        blockers.append(f"候选池扫描失败：{str(e)[:160]}")
        _summary(blockers, warnings)
        return
    if not syms:
        blockers.append("候选池为空（涨幅/成交额门槛可能太高）")
        _summary(blockers, warnings)
        return
    print(f"\n候选币（{len(syms)}）：{', '.join(syms)}")

    info = http_json(f"{FAPI}/fapi/v1/exchangeInfo", timeout=20)
    filters = {}
    for s in info.get("symbols", []):
        f = {"step": 0.001, "tick": 0.0001, "min_notional": 5.0}
        for x in s.get("filters", []):
            t = x.get("filterType")
            if t == "LOT_SIZE":
                f["step"] = float(x["stepSize"])
            elif t == "MIN_NOTIONAL":
                f["min_notional"] = float(x.get("notional") or 5)
            elif t == "PRICE_FILTER":
                f["tick"] = float(x["tickSize"])
        filters[s["symbol"]] = f

    for sym in syms:
        try:
            d = http_json(f"{FAPI}/fapi/v1/depth?symbol={sym}&limit=5", timeout=10)
            bid, ask = float(d["bids"][0][0]), float(d["asks"][0][0])
            spread = (ask - bid) / bid * 100
            f = filters.get(sym, {})
            step = f.get("step", 0.001)
            mn = f.get("min_notional", 5) * 1.01
            qty = notional / ask
            qty = round(int(qty / step + 1e-9) * step, 10)
            if qty * ask < mn:            # 和引擎一致：最小名义优先，向上补一步
                qty = round(int((mn / ask + step) / step + 1e-9) * step, 10)
            ok_notional = qty * ask >= mn
            ex.test_order(sym, "SELL", qty, order_type="LIMIT", price=ask,
                          time_in_force="GTC",
                          position_side=None if not hedge else "SHORT")
            flag = "PASS" if ok_notional else "FAIL"
            print(f"{flag}  {sym:12s} 买/卖 {bid:.6g}/{ask:.6g} 价差 {spread:.2f}% "
                  f"名义 {qty * ask:.2f}U · order/test 通过")
            if not ok_notional:
                blockers.append(f"{sym} 计算名义 {qty * ask:.2f}U 低于最小 {mn:.2f}U")
            if spread > 0.3:
                warnings.append(f"{sym} 价差 {spread:.2f}%，滑点风险偏高")
        except Exception as e:  # noqa: BLE001
            blockers.append(f"{sym} 下单预检失败：{str(e)[:150]}")
            print(f"FAIL  {sym:12s} {str(e)[:150]}")

    _summary(blockers, warnings)


def _summary(blockers: list[str], warnings: list[str]) -> None:
    print()
    if warnings:
        print("WARN：")
        for w in warnings:
            print(f"  · {w}")
    if blockers:
        print("\n❌ 有阻塞项，先别上实盘：")
        for b in blockers:
            print(f"  · {b}")
        sys.exit(1)
    print("\n✅ 预检通过。启动命令：")
    print("   python bot\\strategy_run.py --preset fade_live")
    print("   （GUI：仓位复制页先停止复制，避免同币撞单；再到「自研策略」载入首跑预设）")


if __name__ == "__main__":
    main()
