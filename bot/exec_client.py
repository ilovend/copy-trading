#!/usr/bin/env python3
"""币安合约下单客户端（最小实现）。

职责：
    · HMAC 签名请求
    · 查账户 / 查持仓 / 设置杠杆
    · 下单、撤单

密钥来源优先级：环境变量 EXEC_API_KEY / EXEC_API_SECRET > gui/copy_config.json

注意：本模块只做「执行」，不做任何策略判断。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "gui" / "copy_config.json"
FAPI = "https://fapi.binance.com"


def load_keys() -> tuple[str, str]:
    key = os.environ.get("EXEC_API_KEY", "").strip()
    secret = os.environ.get("EXEC_API_SECRET", "").strip()
    if not key or not secret:
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            key = key or str(cfg.get("exec_api_key") or "").strip()
            secret = secret or str(cfg.get("exec_api_secret") or "").strip()
        except Exception:  # noqa: BLE001
            pass
    return key, secret


def _request(method: str, path: str, params: dict | None = None,
             key: str = "", secret: str = "", signed: bool = True,
             timeout: int = 10) -> dict:
    params = dict(params or {})
    headers = {"X-MBX-APIKEY": key, "User-Agent": "copy-exec/0.1"}
    if signed:
        params["timestamp"] = int(time.time() * 1000)
        params["recvWindow"] = 5000
        query = urllib.parse.urlencode(params, doseq=True)
        sig = hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        query = f"{query}&signature={sig}"
    else:
        query = urllib.parse.urlencode(params, doseq=True)
    url = f"{FAPI}{path}" + (f"?{query}" if query else "")
    req = urllib.request.Request(url, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            body = json.loads(raw)
        except Exception:  # noqa: BLE001
            body = raw
        raise RuntimeError(f"HTTP {e.code}: {body}") from None


class ExecClient:
    def __init__(self, key: str | None = None, secret: str | None = None) -> None:
        k, s = load_keys()
        self.key = key or k
        self.secret = secret or s

    # ------------------------------------------------------------ read
    def account(self) -> dict:
        return _request("GET", "/fapi/v2/account", key=self.key, secret=self.secret)

    def positions(self) -> list[dict]:
        rows = _request("GET", "/fapi/v2/positionRisk", key=self.key, secret=self.secret)
        return [r for r in rows if float(r.get("positionAmt") or 0) != 0]

    def position(self, symbol: str) -> float:
        rows = _request("GET", "/fapi/v2/positionRisk", {"symbol": symbol},
                        key=self.key, secret=self.secret)
        return float((rows[0] if isinstance(rows, list) and rows else rows).get("positionAmt") or 0)

    def open_orders(self, symbol: str) -> list[dict]:
        return _request("GET", "/fapi/v1/openOrders", {"symbol": symbol},
                        key=self.key, secret=self.secret)

    # ----------------------------------------------------------- write
    def set_leverage(self, symbol: str, leverage: int) -> dict:
        return _request("POST", "/fapi/v1/leverage",
                        {"symbol": symbol, "leverage": int(leverage)},
                        key=self.key, secret=self.secret)

    def set_margin_type(self, symbol: str, mode: str) -> dict:
        try:
            return _request("POST", "/fapi/v1/marginType",
                            {"symbol": symbol, "marginType": mode},
                            key=self.key, secret=self.secret)
        except RuntimeError as e:
            if "-4046" in str(e):  # 已经是该模式
                return {"msg": "no need to change margin type"}
            raise

    def order(self, symbol: str, side: str, qty: float, *,
              order_type: str = "MARKET", price: float | None = None,
              time_in_force: str | None = None, reduce_only: bool = False,
              close_position: bool = False, stop_price: float | None = None) -> dict:
        params = {"symbol": symbol, "side": side, "type": order_type,
                  "quantity": qty, "newOrderRespType": "RESULT"}
        if price is not None:
            params["price"] = price
        if time_in_force:
            params["timeInForce"] = time_in_force
        if reduce_only:
            params["reduceOnly"] = "true"
        if close_position:
            params["quantity"] = None
            params["closePosition"] = "true"
        if stop_price is not None:
            params["stopPrice"] = stop_price
        params = {k: v for k, v in params.items() if v is not None}
        return _request("POST", "/fapi/v1/order", params, key=self.key, secret=self.secret)

    def cancel(self, symbol: str, order_id: int) -> dict:
        return _request("DELETE", "/fapi/v1/order",
                        {"symbol": symbol, "orderId": order_id},
                        key=self.key, secret=self.secret)

    # ------------------------------------------------------------ test
    def test(self) -> dict:
        if not self.key or not self.secret:
            raise RuntimeError("没有配置 API Key / Secret")
        acc = self.account()
        positions = self.positions()
        return {
            "equity": float(acc.get("totalMarginBalance") or 0),
            "wallet": float(acc.get("totalWalletBalance") or 0),
            "available": float(acc.get("availableBalance") or 0),
            "positions": len(positions),
            "symbols": [p["symbol"] for p in positions][:5],
        }


if __name__ == "__main__":
    print(ExecClient().test())
