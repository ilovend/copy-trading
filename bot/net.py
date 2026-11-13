"""公开 HTTP 客户端：复用 TLS 连接 + 线程安全。

从 copy_engine 抽出来，策略/复制/工具都直接依赖本模块，
不再需要「为了发一个 GET 去 import 复制模块」。
"""
from __future__ import annotations

import http.client
import json
import threading
import time

FAPI_HOST = "fapi.binance.com"

_PUB_CONN: http.client.HTTPSConnection | None = None
_PUB_LOCK = threading.Lock()


def http_json(url: str, timeout: int = 10):
    """GET 一个 JSON，复用连接；检查 HTTP 状态码，限频时退避重试。"""
    global _PUB_CONN
    path = url.replace(f"https://{FAPI_HOST}", "")
    with _PUB_LOCK:
        last: Exception | None = None
        for attempt in range(3):
            try:
                if _PUB_CONN is None:
                    _PUB_CONN = http.client.HTTPSConnection(FAPI_HOST, timeout=timeout)
                _PUB_CONN.request("GET", path,
                                  headers={"User-Agent": "copy-engine/0.4",
                                           "Connection": "keep-alive"})
                resp = _PUB_CONN.getresponse()
                raw = resp.read()
                status = resp.status
                if resp.getheader("connection", "").lower() == "close":
                    _PUB_CONN.close()
                    _PUB_CONN = None
                try:
                    body = json.loads(raw.decode())
                except Exception:  # noqa: BLE001
                    body = None
                if status >= 400:
                    code = body.get("code") if isinstance(body, dict) else None
                    msg = (body.get("msg") if isinstance(body, dict)
                           else raw[:200].decode("utf-8", "replace"))
                    err = RuntimeError(f"HTTP {status} code={code}: {msg}")
                    if status in (418, 429, 500, 502, 503, 504) and attempt < 2:
                        last = err
                        time.sleep(1.0 * (attempt + 1))
                        continue
                    raise err
                if body is None:
                    raise RuntimeError(f"非 JSON 响应: {raw[:120]!r}")
                return body
            except Exception as e:  # noqa: BLE001
                last = e
                try:
                    if _PUB_CONN:
                        _PUB_CONN.close()
                except Exception:  # noqa: BLE001
                    pass
                _PUB_CONN = None
                time.sleep(0.1)
        raise RuntimeError(f"request failed: {last}")
