#!/usr/bin/env python3
"""自动跟单（名额监控 + 自动加入）。

逻辑：
    每 N 秒拉一次「收藏的带单员」列表，发现 currentCopyCount < maxCopyCount
    且该带单员在监控名单里，就按配置调用 copy-portfolio/create 加入跟单。

配置：gui/autojoin_config.json
    {
      "poll_interval": 1.0,
      "live": false,                       // false = 只提示不下单
      "defaults": { ...跟单参数... },
      "targets": {"<leadPortfolioId>": {"nickname": "...", "investAmount": 50}}
    }

用法：
    python bot/auto_join.py            # 按配置跑
    python bot/auto_join.py --dry-run  # 强制只提示
"""
from __future__ import annotations

import argparse
import http.client
import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bot"))

import listen_copy as L  # noqa: E402

CONFIG_PATH = ROOT / "gui" / "autojoin_config.json"
BASE_URL = "https://www.binance.com"


class Api:
    """复用同一条 TLS 连接：实测能把每次轮询从 ~180ms 降到 ~90ms。"""

    def __init__(self) -> None:
        self.conn: http.client.HTTPSConnection | None = None
        self.headers = {**L.HEADERS, "connection": "keep-alive"}

    def _new(self) -> http.client.HTTPSConnection:
        self.conn = http.client.HTTPSConnection("www.binance.com", timeout=10)
        return self.conn

    def post(self, path: str, payload: dict) -> dict:
        body = json.dumps(payload)
        last: Exception | None = None
        for _ in range(2):
            try:
                conn = self.conn or self._new()
                conn.request("POST", path, body=body, headers=self.headers)
                resp = conn.getresponse()
                raw = resp.read()
                if resp.getheader("connection", "").lower() == "close":
                    conn.close()
                    self.conn = None
                return json.loads(raw.decode())
            except Exception as e:  # noqa: BLE001
                last = e
                if self.conn:
                    self.conn.close()
                    self.conn = None
        raise RuntimeError(f"request failed: {last}")

DEFAULT_PARAMS = {
    "investAmount": 50,
    "investAsset": "USDT",
    "copyModel": "FIXED_RATIO",
    "costPerOrder": 0,
    "marginMode": "FOLLOW_LEAD",
    "leverageMode": "FOLLOW_LEAD",
    "leverageValue": None,
    "totalStopLossRate": None,
    "takeProfitRate": None,
    "stopLostRate": None,
    "maxPositionPerSymbolRate": None,
}


def log(msg: str) -> None:
    print(f"{datetime.now():%H:%M:%S} {msg}", flush=True)


def load_config() -> dict:
    cfg = {"poll_interval": 1.0, "live": False, "defaults": dict(DEFAULT_PARAMS), "targets": {}}
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception as e:  # noqa: BLE001
            log(f"config load failed: {e}")
    return cfg


def fetch_favorites(api: Api) -> list[dict]:
    body = {"pageNumber": 1, "pageSize": 50, "timeRange": "90D", "dataType": "ROI",
            "favoriteOnly": True, "hideFull": False, "nickname": "",
            "order": "DESC", "apiKeyOnly": False}
    j = api.post("/bapi/futures/v1/friendly/future/copy-trade/home-page/query-list", body)
    return (j.get("data") or {}).get("list") or []


def join(api: Api, lead_id: str, params: dict) -> tuple[bool, str]:
    body = {
        "leverageMode": params.get("leverageMode", "FOLLOW_LEAD"),
        "marginMode": params.get("marginMode", "FOLLOW_LEAD"),
        "investAmount": params.get("investAmount", 50),
        "copyModel": params.get("copyModel", "FIXED_RATIO"),
        "costPerOrder": params.get("costPerOrder", 0),
        "investAsset": params.get("investAsset", "USDT"),
        "leadPortfolioId": lead_id,
        "totalStopLossRate": params.get("totalStopLossRate"),
        "leverageValue": params.get("leverageValue"),
        "takeProfitRate": params.get("takeProfitRate"),
        "stopLostRate": params.get("stopLostRate"),
        "maxPositionPerSymbolRate": params.get("maxPositionPerSymbolRate"),
    }
    try:
        r = api.post("/bapi/futures/v1/private/future/copy-trade/copy-portfolio/create", body)
    except Exception as e:  # noqa: BLE001
        return False, f"请求异常: {e}"
    if r.get("success"):
        return True, "加入成功"
    return False, f"{r.get('code')} {r.get('message')}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只提示不下单")
    args = ap.parse_args()

    cfg = load_config()
    live = bool(cfg.get("live")) and not args.dry_run
    interval = float(cfg.get("poll_interval", 1.0) or 1.0)
    targets = cfg.get("targets") or {}
    defaults = dict(DEFAULT_PARAMS)
    defaults.update(cfg.get("defaults") or {})

    if not targets:
        log("监控名单为空：先在 GUI 的「自动跟单」页选中带单员并保存")
        return

    L.load_session()
    api = Api()
    log(f"自动跟单启动 · {'实盘下单' if live else '只提示（dry-run）'} · "
        f"监控 {len(targets)} 人 · 间隔 {interval:.0f}s")
    names = "、".join(t.get("nickname") or k for k, t in targets.items())
    L.tg_send(f"🟢 自动跟单已启动（{'实盘' if live else '只提示'}）\n"
              f"监控 {len(targets)} 人：{names}\n"
              f"每 {interval:.0f} 秒检查一次名额，出现空位会立刻通知。")

    joined: set[str] = set()
    announced: set[str] = set()

    while True:
        try:
            rows = fetch_favorites(api)
        except Exception as e:  # noqa: BLE001
            log(f"拉取收藏失败: {e}")
            time.sleep(interval)
            continue

        by_id = {r["leadPortfolioId"]: r for r in rows}
        for lead_id, tcfg in targets.items():
            if lead_id in joined:
                continue
            row = by_id.get(lead_id)
            if not row:
                continue
            cur, mx = row.get("currentCopyCount"), row.get("maxCopyCount")
            nickname = row.get("nickname") or tcfg.get("nickname") or lead_id
            if cur is None or mx is None or cur >= mx:
                continue

            if lead_id not in announced:
                log(f"发现名额！{nickname} {cur}/{mx}")
                L.tg_send(f"🔔 名额出现：{nickname}\n{cur}/{mx}\n"
                          f"https://www.binance.com/zh-CN/copy-trading/copy-setting?portfolioId={lead_id}")
                announced.add(lead_id)

            params = dict(defaults)
            params.update(tcfg)
            params.setdefault("investAmount", defaults.get("investAmount", 50))

            if not live:
                log(f"[dry-run] 本应加入 {nickname}，投入 {params['investAmount']} U")
                joined.add(lead_id)
                continue

            ok, msg = join(api, lead_id, params)
            if ok:
                log(f"✅ 已加入 {nickname}，投入 {params['investAmount']} U")
                L.tg_send(f"✅ 自动跟单成功\n{nickname}\n投入 {params['investAmount']} U")
                joined.add(lead_id)
            else:
                log(f"❌ 加入失败 {nickname}: {msg}")
                L.tg_send(f"❌ 自动跟单失败\n{nickname}\n{msg}\n请手动处理")

        if len(joined) >= len(targets):
            log("监控名单已全部处理完，退出")
            return
        time.sleep(interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("bye")
