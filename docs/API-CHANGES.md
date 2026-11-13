# 币安接口变更记录

> 目的：币安改接口时往往**旧地址不报错、只是不返回数据**，排障极费时间。
> 这里记录已验证的变更、失败表现和复验方法，升级出问题时先对照本表。
>
> 最近核验：2026-11-13（v1.10.3）
>
> 快速自检：`python bot\ws_probe.py`（一次性测所有候选 WS 入口，看谁还活着）。

## 一、快速对照表

| # | 功能 | 旧接口（已不可用/停推） | 新接口 | 判定 |
|---|---|---|---|---|
| 1 | 合约行情 WS | `wss://fstream.binance.com/ws/<stream>`<br>`wss://fstream.binance.com/stream?streams=...` | `wss://fstream.binance.com/market/stream?streams=...` | 旧地址**握手成功但 0 条推送** |
| 2 | 合约条件单（止盈/止损） | `POST /fapi/v1/order`<br>`type=STOP_MARKET / TAKE_PROFIT_MARKET` | `POST /fapi/v1/algoOrder`<br>`algoType=CONDITIONAL` | 旧接口返回 `-4120` |
| 3 | 撤条件单 | `DELETE /fapi/v1/order` | `DELETE /fapi/v1/algoOrder`（`algoId`） | 对应 #2 |
| 4 | 跟单公开成交 | `POST /friendly/future/copy-trade/lead-portfolio/trade-history` | 路径未变，**分页参数变了** | `pageNumber>1` 必须带 `timeRange` |
| 5 | 用户数据流 | `wss://fstream.binance.com/ws/<listenKey>` | `wss://fstream.binance.com/private/stream` + `SUBSCRIBE` | 路径已换（本项目已用新路径） |

---

## 二、合约行情 WebSocket（2026-11 确认）

### 现象

- 连接正常、TLS 握手正常、日志显示「WS 已连接」；
- 之后**一条消息都没有**，`ws_age` 一直涨；
- 策略表现为「挂一晚上不开单」。

### 排除过程（很重要，避免误判成代理问题）

| 测试 | 结果 |
|---|---|
| 老 `/ws/btcusdt@kline_1m` | 0 条 |
| 老 `/stream?streams=...` | 0 条 |
| 现货 `wss://stream.binance.com:9443/ws/...` | **正常**（8s 5 条） |
| 合约测试网 `wss://fstream.binancefuture.com/ws/...` | **正常**（10s 13 条） |
| DoH 取真实 IP 绕过 fake-ip 直连老地址 | 仍 0 条 |
| **新** `/market/stream?streams=...` | **正常**（8s 16 条） |
| 新入口组合流（2 K 线 + `!markPrice@arr@1s`） | **正常**（10s 45 条） |

结论：不是代理、不是客户端、不是防火墙，是**生产环境合约行情旧入口停推**。
`/public/stream` 实测 0 条（可能需要先发订阅消息），用 `/market/stream` 即可。

### 本项目的改法

`bot/strategy_run.py`：

```python
WS = "wss://fstream.binance.com/market/stream"
streams = [f"{s.lower()}@kline_{interval}" for s in universe]
url = f"{WS}?streams={'/'.join(streams)}"
```

保留 REST 轮询兜底（`feed=auto`），WS 不可用自动接管。

### 复验命令

```bash
python - <<'PY'
import asyncio, time, websockets
async def main():
    url = "wss://fstream.binance.com/market/stream?streams=btcusdt@kline_1m"
    n = 0
    async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
        t0 = time.time()
        while time.time() - t0 < 8:
            try:
                await asyncio.wait_for(ws.recv(), timeout=2); n += 1
            except asyncio.TimeoutError: pass
    print("收到", n, "条")
asyncio.run(main())
PY
```

正常应 ≥10 条；0 条说明入口又变了，去币安文档查最新 WS 路径。

---

## 三、条件单（止盈 / 止损，2026-11 确认）

### 现象

在 `/fapi/v1/order` 上发 `STOP_MARKET` / `TAKE_PROFIT_MARKET`：

```json
{"code": -4120, "msg": "Order type not supported for this endpoint.
 Please use the Algo Order API endpoints instead."}
```

`STOP` / `TAKE_PROFIT`（限价触发）也一样被拒。

### 新接口

```
POST /fapi/v1/algoOrder
```

| 参数 | 值 |
|---|---|
| `algoType` | `CONDITIONAL` |
| `symbol` / `side` | 如 `CBRSUSDT` / `BUY`（平空用 BUY） |
| `positionSide` | 双向模式必填：`SHORT` |
| `type` | `STOP_MARKET` 或 `TAKE_PROFIT_MARKET` |
| `triggerPrice` | 触发价 |
| `closePosition` | `"true"`（全平，不能同时传 `quantity` / `reduceOnly`） |
| `workingType` | `MARK_PRICE`（推荐，避免插针） |

响应示例：

```json
{"algoId": 3000002361038907, "algoStatus": "NEW",
 "orderType": "STOP_MARKET", "triggerPrice": "120.26000",
 "closePosition": true}
```

### 撤单

```
DELETE /fapi/v1/algoOrder?symbol=CBRSUSDT&algoId=3000002361038907
```

### 坑

- `closePosition=true` 的订单**必须在有持仓时才能挂**，空仓会返回
  `-4509 Time in Force (TIF) GTE can only be used with open positions`；
- **同方向只允许一张 `closePosition` 条件单**，再挂会返回
  `-4130 An open stop or take profit order with GTE and closePosition in the direction is existing`；
  所以止损/止盈不能都用全平：本项目**止损用 `closePosition=true`，止盈用 `quantity`**；
- **撤单是异步的**：撤完立刻挂新单仍可能 `-4130`，要轮询
  `GET /fapi/v1/openAlgoOrders` 直到为空（本项目等 2 秒）再挂；
- 触发价若已在市价错误一侧，会返回 `-2021 Order would immediately trigger`；
- 双向模式不能传 `reduceOnly`（`-1106`）；
- 条件单**不占 IP 权重**，但计入下单频率（10s / 1min 各 1）；
- 执行客户端复用同一条 TLS 连接，多线程并发会报 `Idle` /
  `CannotSendRequest` / `WinError 10038`，`exec_client._request` 已加锁串行。

### 本项目的实现

- `bot/exec_client.py`：`stop_close()`、`cancel_algo()`
- `bot/strategy_engine.py`：`_place_protect()` / `_cancel_protect()`
  开仓/加仓后自动挂 STOP_MARKET + TAKE_PROFIT_MARKET，平仓撤单，加仓按新均价重挂，
  重启接管时重挂；配置开关 `basket.protect`

### 复验命令（需要有持仓）

```python
from exec_client import ExecClient
ex = ExecClient()
r = ex.stop_close("CBRSUSDT", "BUY", 120.26, kind="STOP_MARKET", position_side="SHORT")
print(r["algoId"], r["algoStatus"])
print(ex.cancel_algo("CBRSUSDT", r["algoId"]))
```

---

## 四、跟单公开接口（分页坑）

`POST /bapi/futures/v1/friendly/future/copy-trade/lead-portfolio/trade-history`

- 第 1 页：`{"portfolioId": ..., "pageNumber": 1, "pageSize": 100}` 可以；
- **第 2 页起必须带 `timeRange`**（如 `"90D"`），否则返回空列表；
- `pageSize` 实际上限 **200**（传 500 只回 200）；
- 频率过高返回 `11012005 当前系统正忙`，需要退避重试。

本项目实现见 `bot/lead_research.py`（含退避与缓存）。

---

## 五、用户数据流（跟单）

```
POST /fapi/v1/listenKey        （API Key 签名，换 listenKey）
GET  /fapi/v1/listenKey        （续期，建议每 30 分钟）
wss://fstream.binance.com/private/stream   （新路径）
发送 {"method":"SUBSCRIBE","params":["<listenKey>"],"id":1}
```

`/ws/<listenKey>` 是旧路径，已停推。

---

## 六、未变、但容易踩的接口

| 接口 | 说明 |
|---|---|
| `GET /fapi/v1/klines` | 正常，`limit` 上限 1500 |
| `GET /fapi/v1/exchangeInfo` | 正常，注意 `LOT_SIZE.stepSize`、`PRICE_FILTER.tickSize`、`MIN_NOTIONAL` |
| `GET /fapi/v2/account` | 正常 |
| `GET /fapi/v2/positionRisk` | 正常，**刚成交时可能短暂查不到仓位**（本项目对账做了二次确认） |
| `GET /fapi/v1/positionSide/dual` | 正常 |
| `GET /fapi/v1/ticker/price` | 正常，不带 symbol 返回全市场 |
| `GET /fapi/v1/userTrades` | 正常，用于核对实际成交与手续费 |

---

## 七、排查顺序（下次再遇到「连上但没数据」）

**第 0 步（最快，已经两次直接解决问题）**：打开 <https://www.binance.com> 合约页面
→ F12 → Network → WS → 点开连接看 **Request URL**，把真实地址抄下来直接测。

> 本项目已经踩过两次同一类坑：
> 1. 跟单用户数据流：`/ws/<listenKey>` → `/private/stream`（握手正常、不推数据）
> 2. 合约行情：`/ws/<stream>`、`/stream?streams=` → `/market/stream`
>
> 两次都是「连接成功」骗了人，所以**永远要数消息条数**。

**第 1 步**：用自带探针对照，一条命令看清哪个入口活着：

```bat
python bot\ws_probe.py
python bot\ws_probe.py --stream akeusdt@kline_1m
python bot\ws_probe.py --url "wss://fstream.binance.com/market/stream?streams=btcusdt@kline_1m"
```

输出 `✅ N 条` 是可用，`❌ 0 条` 是「握手成功但不推」，`连接失败` 才是网络问题。

**第 2 步**：现货 WS + 合约测试网对照，区分「客户端/网络」与「入口迁移」；
**第 3 步**：DoH 拿真实 IP 直连，排除 fake-ip / 代理 DNS；
**第 4 步**：用 `/fapi/v1/order/test` 或 `/fapi/v1/algoOrder` 的错误码定位接口是否迁移
（`-4120` = 接口换了，`-4509` = 参数/持仓前提不满足）；
**第 5 步**：查本文件，再查币安官方「Change Log」。
