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
import secrets
import os
import threading
import time
import urllib.error
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "gui" / "copy_config.json"
FAPI = "https://fapi.binance.com"


def _load_env_file() -> None:
    """.env 里的变量如果没进环境，就补进来（不改已有值）。"""
    path = ROOT / ".env"
    if not path.exists():
        return
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except Exception:  # noqa: BLE001
        pass


def load_keys() -> tuple[str, str]:
    _load_env_file()
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
_CONN_LOCK = threading.Lock()


def _conn() -> http.client.HTTPSConnection:
    global _CONN
    if _CONN is None:
        _CONN = http.client.HTTPSConnection("fapi.binance.com", timeout=10)
    return _CONN


def _request(method: str, path: str, params: dict | None = None,
             key: str = "", secret: str = "", signed: bool = True,
             timeout: int = 10, retry: bool = True) -> dict:
    """复用同一条 TLS 连接：实测每次 1–3 秒降到 90ms 左右。

    连接是全局共享的，信号/持仓/对账/条件单多个线程都会用，
    并发时会出 `Idle`、`CannotSendRequest`、WinError 10038，所以要加锁串行。
    """
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
    with _CONN_LOCK:
        last: Exception | None = None
        for _ in range(3 if retry else 1):
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
                        -4120: "该订单类型已迁移到 /fapi/v1/algoOrder",
                        -4130: "同方向已有条件单，先撤再挂",
                        -4164: "名义低于最小值，需要向上补一个步长",
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
                time.sleep(0.15)
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
    def set_leverage(self, symbol: str, leverage: int) -> dict:
        try:
            return _request("POST", "/fapi/v1/leverage",
                            {"symbol": symbol, "leverage": int(leverage)},
                            key=self.key, secret=self.secret)
        except RuntimeError as e:
            if "-4421" in str(e):
                raise RuntimeError(
                    f"当前 API Key 属于子账户，杠杆最高 5x（请求 {leverage}x）。"
                    "实盘请改用主账户的 API Key，或把杠杆手动设为 5 或更低。"
                ) from None
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

    def query_order_by_client(self, symbol: str, client_id: str) -> dict:
        return _request("GET", "/fapi/v1/order",
                        {"symbol": symbol, "origClientOrderId": client_id},
                        key=self.key, secret=self.secret)

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
        cid = f"cx{int(time.time() * 1000)}{secrets.randbelow(9000) + 1000}"
        params["newClientOrderId"] = cid
        try:
            return _request("POST", "/fapi/v1/order", params,
                            key=self.key, secret=self.secret, retry=False)
        except RuntimeError:
            raise                       # 交易所明确拒绝，不重试
        except Exception:               # 超时/断连：订单可能已受理，回查
            try:
                o = self.query_order_by_client(symbol, cid)
                if o.get("orderId"):
                    print(f"[exec] 下单超时但已受理 {symbol} {cid}", flush=True)
                    return o
            except Exception:  # noqa: BLE001
                pass
            raise

    def cancel(self, symbol: str, order_id: int) -> dict:
        return _request("DELETE", "/fapi/v1/order",
                        {"symbol": symbol, "orderId": order_id},
                        key=self.key, secret=self.secret)

    def query_order(self, symbol: str, order_id: int) -> dict:
        """查单状态：NEW / PARTIALLY_FILLED / FILLED / CANCELED / EXPIRED。"""
        return _request("GET", "/fapi/v1/order",
                        {"symbol": symbol, "orderId": order_id},
                        key=self.key, secret=self.secret)

    def user_trades(self, symbol: str, start_ms: int, limit: int = 200) -> list[dict]:
        """成交明细（含 realizedPnl 与手续费），用于回填真实盈亏。"""
        return _request("GET", "/fapi/v1/userTrades",
                        {"symbol": symbol, "startTime": start_ms, "limit": limit},
                        key=self.key, secret=self.secret)

    def cancel_all(self, symbol: str) -> dict:
        """撤掉该币全部挂单（策略停止/收盘清理用）。"""
        return _request("DELETE", "/fapi/v1/allOpenOrders",
                        {"symbol": symbol}, key=self.key, secret=self.secret)

    def stop_close(self, symbol: str, side: str, stop_price: float, *,
                   kind: str = "STOP_MARKET", position_side: str | None = None,
                   working_type: str = "MARK_PRICE",
                   qty: float | None = None) -> dict:
        """挂条件单（止损用全平，止盈用指定数量）。

        币安 2025-12 起把条件单迁到 Algo 接口：POST /fapi/v1/algoOrder。
        注意：**同方向只允许一张 closePosition 条件单**（否则 -4130），
        所以止损用 closePosition=true，止盈改用 quantity 指定数量，两者可并存。
        """
        params = {"algoType": "CONDITIONAL", "symbol": symbol, "side": side,
                  "type": kind, "triggerPrice": stop_price,
                  "workingType": working_type}
        if qty is not None:
            params["quantity"] = qty
            if not position_side:        # 单向持仓：只减仓，不反向开仓
                params["reduceOnly"] = "true"
        else:
            params["closePosition"] = "true"
        if position_side:
            params["positionSide"] = position_side
        return _request("POST", "/fapi/v1/algoOrder", params,
                        key=self.key, secret=self.secret)

    def cancel_algo(self, symbol: str, algo_id) -> dict:
        """撤条件单。"""
        return _request("DELETE", "/fapi/v1/algoOrder",
                        {"symbol": symbol, "algoId": algo_id},
                        key=self.key, secret=self.secret)

    def open_algo_orders(self, symbol: str = "") -> list[dict]:
        """当前未触发的条件单；不传 symbol 返回全部（用于清理残留）。"""
        params = {"symbol": symbol} if symbol else {}
        return _request("GET", "/fapi/v1/openAlgoOrders", params,
                        key=self.key, secret=self.secret)

    def cancel_all_algo(self, symbol: str) -> int:
        """撤掉该币所有条件单，返回撤单数量。"""
        n = 0
        try:
            rows = self.open_algo_orders(symbol)
        except Exception:  # noqa: BLE001
            return 0
        for r in rows:
            aid = r.get("algoId")
            if not aid:
                continue
            try:
                self.cancel_algo(symbol, aid)
                n += 1
            except Exception:  # noqa: BLE001
                pass
        return n

    def test_order(self, symbol: str, side: str, qty: float, *,
                   order_type: str = "LIMIT", price: float | None = None,
                   time_in_force: str | None = None,
                   position_side: str | None = None) -> dict:
        """币安下单预检接口：校验签名与参数，不会真的下单。"""
        params = {"symbol": symbol, "side": side, "type": order_type,
                  "quantity": qty, "newOrderRespType": "RESULT"}
        if position_side:
            params["positionSide"] = position_side
        if price is not None:
            params["price"] = price
        if time_in_force:
            params["timeInForce"] = time_in_force
        return _request("POST", "/fapi/v1/order/test", params,
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
