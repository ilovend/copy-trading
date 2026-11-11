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
import http.client
import json
import os
import time
import urllib.error
import urllib.parse
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


_CONN: http.client.HTTPSConnection | None = None


def _conn() -> http.client.HTTPSConnection:
    global _CONN
    if _CONN is None:
        _CONN = http.client.HTTPSConnection("fapi.binance.com", timeout=10)
    return _CONN


def _request(method: str, path: str, params: dict | None = None,
             key: str = "", secret: str = "", signed: bool = True,
             timeout: int = 10) -> dict:
    """复用同一条 TLS 连接：实测每次 1–3 秒降到 90ms 左右。"""
    global _CONN
    params = dict(params or {})
    headers = {"X-MBX-APIKEY": key, "User-Agent": "copy-exec/0.2",
               "Connection": "keep-alive", "Content-Type": "application/x-www-form-urlencoded"}
    if signed:
        params["timestamp"] = int(time.time() * 1000)
        params["recvWindow"] = 5000
        query = urllib.parse.urlencode(params, doseq=True)
        sig = hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        query = f"{query}&signature={sig}"
    else:
        query = urllib.parse.urlencode(params, doseq=True)
    target = path + (f"?{query}" if query else "")
    last: Exception | None = None
    for _ in range(2):
        try:
            conn = _conn()
            conn.request(method, target, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            if resp.getheader("connection", "").lower() == "close":
                conn.close()
                _CONN = None
            body = json.loads(raw.decode())
            if resp.status >= 400:
                code = body.get("code") if isinstance(body, dict) else None
                hint = {
                    -2015: "IP 不在白名单 / 权限不足 / Key 已过期（检查 API 管理页）",
                    -2014: "API Key 格式错误或不存在",
                    -1022: "签名错误（Secret 不对）",
                    -1021: "时间戳超出窗口（本机时间偏差过大）",
                    -4061: "账户是单向持仓模式，不能带 positionSide",
                    -4062: "账户是双向持仓模式，必须带 positionSide",
                }.get(code)
                msg = f"HTTP {resp.status}: {body}"
                if hint:
                    msg += f" → {hint}"
                raise RuntimeError(msg)
            return body
        except RuntimeError:
            raise
        except Exception as e:  # noqa: BLE001
            last = e
            try:
                if _CONN:
                    _CONN.close()
            except Exception:  # noqa: BLE001
                pass
            _CONN = None
    raise RuntimeError(f"request failed: {last}")


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

    def position(self, symbol: str, ps: str | None = None) -> float:
        rows = _request("GET", "/fapi/v2/positionRisk", {"symbol": symbol},
                        key=self.key, secret=self.secret)
        if isinstance(rows, dict):
            rows = [rows]
        total = 0.0
        for r in rows:
            if ps and r.get("positionSide") not in (ps, "BOTH"):
                continue
            total += float(r.get("positionAmt") or 0)
        return total

    def position_mode(self) -> bool:
        """True = 双向持仓（Hedge Mode），False = 单向。"""
        r = _request("GET", "/fapi/v1/positionSide/dual", key=self.key, secret=self.secret)
        return bool(r.get("dualSidePosition"))

    def set_position_mode(self, dual: bool) -> dict:
        """切换持仓模式。有持仓时币安会拒绝，这是保护，不要绕过。"""
        return _request("POST", "/fapi/v1/positionSide/dual",
                        {"dualSidePosition": "true" if dual else "false"},
                        key=self.key, secret=self.secret)

    def open_orders(self, symbol: str) -> list[dict]:
        return _request("GET", "/fapi/v1/openOrders", {"symbol": symbol},
                        key=self.key, secret=self.secret)

    # ----------------------------------------------------------- write
    SUBACCOUNT_MAX_LEVERAGE = 5

    def set_leverage(self, symbol: str, leverage: int) -> dict:
        try:
            return _request("POST", "/fapi/v1/leverage",
                            {"symbol": symbol, "leverage": int(leverage)},
                            key=self.key, secret=self.secret)
        except RuntimeError as e:
            # -4421：子账户杠杆不能超过 5x，自动降级
            if "-4421" in str(e) and int(leverage) > self.SUBACCOUNT_MAX_LEVERAGE:
                r = _request("POST", "/fapi/v1/leverage",
                             {"symbol": symbol,
                              "leverage": self.SUBACCOUNT_MAX_LEVERAGE},
                             key=self.key, secret=self.secret)
                r["clamped"] = True
                return r
            raise

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
              close_position: bool = False, stop_price: float | None = None,
              position_side: str | None = None) -> dict:
        params = {"symbol": symbol, "side": side, "type": order_type,
                  "quantity": qty, "newOrderRespType": "RESULT"}
        if position_side:
            params["positionSide"] = position_side
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
        hedge = self.position_mode()
        return {
            "hedge": hedge,
            "equity": float(acc.get("totalMarginBalance") or 0),
            "wallet": float(acc.get("totalWalletBalance") or 0),
            "available": float(acc.get("availableBalance") or 0),
            "positions": len(positions),
            "symbols": [p["symbol"] for p in positions][:5],
        }


if __name__ == "__main__":
    print(ExecClient().test())
