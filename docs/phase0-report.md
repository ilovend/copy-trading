# Phase 0 实测报告 — 币安带单数据接口

> 日期：2026-11-11 00:25 (UTC+8)
> 环境：Windows / Python 3.15 / 直连 `www.binance.com`
> 脚本：`probe/` 下全部可复现
> 结论级别：**匿名公开数据已定论；账户私有数据待新鲜会话验证**

---

## 一、核心结论

1. **公开的"带单员成交流"有约 121 秒的固定延迟。**
   四次独立测量：120.4s / 121.2s / 121.2s / 122.6s。
   这是币安的反抢跑设计，不是网络问题，**用它做实时复制没有可能**。

2. **公开的"当前持仓"快照约 5 秒刷新**，但只对 `positionShow=true` 的带单员开放，
   且接口有软限流（4 req/s → 100% 空包）。抽样 30 个带单员，**47% 隐藏持仓**。

3. **你的账户 API Key 看不到跟单账户。**
   合约钱包余额 `0`、1884 个仓位全为 0，但一小时前有一笔 `-1508 USDT` 的 TRANSFER。
   说明跟单资金在**独立的跟单钱包**，标准合约 API（`/fapi/*`）完全看不到。

4. **你贴的 Cookie 是旧会话**（挑战令牌 11-10 02:23 就过期了），
   服务端把我当游客，私有接口返回空。

5. 结论：**外部实时复制一个"隐藏持仓"的带单员，目前看不到可行路径**；
   唯一还有希望的是"用你自己的跟单成交当信号"，但它需要新鲜会话验证。

---

## 二、已确认的端点（全部实测）

| # | 方法 | 路径（前缀 `/bapi/futures/v1`） | 用途 | 鉴权 |
|---|---|---|---|---|
| 1 | POST | `/friendly/future/copy-trade/home-page/query-list` | 带单员列表/名额 | 无 |
| 2 | GET | `/friendly/future/copy-trade/lead-portfolio/detail?portfolioId=` | 元数据（含 `positionShow`、`hasCopy`、`copyPortfolioId`） | 无 |
| 3 | GET | `/friendly/future/copy-trade/lead-data/positions?portfolioId=` | 当前持仓快照 | 无（仅公开持仓者） |
| 4 | POST | `/friendly/future/copy-trade/lead-portfolio/position-history` | 已平仓持仓 | 无 |
| 5 | POST | `/friendly/future/copy-trade/lead-portfolio/order-history` | 订单（含下单/更新时间） | 无 |
| 6 | POST | `/friendly/future/copy-trade/lead-portfolio/trade-history` | 逐笔成交 | 无 |
| 7 | POST | `/private/future/copy-trade/copy-portfolio/trade-history` | **我的**跟单成交 | Cookie |
| 8 | POST | `/private/future/copy-trade/copy-portfolio/position-history` | **我的**跟单持仓历史 | Cookie |

> 方法不能错：GET/POST 用反了会返回 `400 {"code":"000002"}`；路径不存在返回 404。

---

## 三、两条公开数据通道的实测性能

### 3.1 成交流 `/lead-portfolio/trade-history` —— 延迟 121 秒

字段：

```json
{"time":1794312033000,"symbol":"XAGUSDT","side":"SELL","positionSide":"SHORT",
 "price":52.42,"qty":9.156,"quantity":479.95752,
 "realizedProfit":0.0,"fee":0.0,"activeBuy":true}
```

开平仓判定：`realizedProfit == 0` → 开/加仓；`!= 0` → 平/减仓。

**延迟实测（4 次，跨 3 个带单员）**：

| 成交时间 | 接口可见时间 | 延迟 |
|---|---|---|
| 00:15:29 | 00:17:31 | 120.4 s |
| 00:15:39 | 00:17:39 | 121.2 s |
| 00:21:11 | 00:22:12 | 121.2 s |
| — | — | 122.6 s |

延迟高度稳定在 121s ± 1s → **服务端定时批处理/延迟发布**，不是网络抖动。
轮询再快也没用。

### 3.2 持仓快照 `/lead-data/positions`

- 返回整账户全币种数组：759–804 行，**260–275 KB**（未压缩），非零仓位通常 2–6 条。
- 延迟 p50 = 231 ms，p95 = 928 ms。
- 快照约 **5 秒**刷新一次（200ms 轮询时返回体逐字节不变，连 `markPrice` 都是冻结的）。
- **软限流**：

  | 频率 | `data:null` 比例 |
  |---|---|
  | 1 req/s | 50% |
  | 4 req/s | 100% |

  返回 `code:"000000"` + `data:null`，**不是 429**，业务层必须自己判空重试。

---

## 四、`positionShow` 决定你能看到什么

| 数据 | 公开持仓 | 隐藏持仓 |
|---|---|---|
| 当前持仓 | ✅ | ❌ `null` |
| 已平仓持仓 | ✅ | ✅ |
| 订单 | ✅ | ✅ |
| 成交 | ✅（延迟 121s） | ✅（延迟 121s） |

抽样 30 个 ROI 前 30 的带单员：**14 个隐藏持仓（47%）**。

你给的三个 ID：

| 昵称 | leadPortfolioId | positionShow | 名额 |
|---|---|---|---|
| 熬鹰资本 | 5075281354358777856 | false | 1000/1000 |
| DAmorné | 5219911688676500225 | false | 399/400 |

---

## 五、账户 API Key 探测结果

| 检查项 | 结果 |
|---|---|
| 权限 | 读 ✅ / 合约 ✅ / 提现 ❌ / IP 白名单 ✅ |
| 合约钱包余额 | `0.00000000` |
| 仓位（`/fapi/v2/positionRisk`） | 1884 条，**非零 0 条** |
| 最近资金流水 | `TRANSFER -1508.38 USDT`（约 1 小时前） |
| `/sapi/v1/copyTrading/futures/userStatus` | `isLeadTrader: false` |
| `/sapi/v1/copyTrading/futures/leadSymbol` | 返回全量 TradFi 币种白名单（无实用价值） |

**判定：跟单账户与合约账户是两套账。** 资金转进跟单钱包后，
`/fapi/v2/account`、`/fapi/v2/positionRisk`、USER_DATA 流都看不到，
所以"用 API 读自己跟单成交"这条路的前提是：存在一个能读跟单钱包的接口。
标准 API 没有，只能看私有 bapi 有没有。

---

## 六、会话（Cookie）状态

你贴的 Cookie 里 `bnc-challenge-token` 的 `exp` = 2026-11-10 02:23，
**已过期约 22 小时**；`csrftoken` / `bnc-uuid` 与你仓库里那份完全一致（旧会话）。

实测后果：
- 页面 SSR 返回"登录 | Binance"；
- 私有接口 `/copy-portfolio/trade-history` 返回 `total: 0`（游客态）；
- `/copy-portfolio/position-history` 返回 `200001054 参数不合法`。

**需要一个刚抓的新会话才能继续。**

---

## 七、对方案的影响（修订版）

| 路线 | 结论 |
|---|---|
| A 官方跟单 + 比例叠加 | 仍可行，但叠加部分要靠自己的跟单账户状态，需私有接口 |
| B 全自建轮询公开数据 | **基本判死**：成交流延迟 121s；持仓快照仅公开持仓者可用且限流 |
| C 跟单账户当探针 | **唯一有希望**，但必须先证明能读到跟单钱包的成交，且延迟可接受 |

也就是说：**如果私有接口读自己跟单成交的延迟也在分钟级，这个项目就没有"低延迟"可言，
只能做"官方跟单 + 事后补仓"这种弱复制。**

---

## 八、下一步（需要你配合）

1. **重新抓一次新鲜 headers**：
   F12 → Network → 刷新 `copy-management` 页面 → 点任意 `copy-portfolio` 请求 →
   右键 *Copy as cURL* → 整段贴给我。
2. **告诉我你实际跟的是谁**（主页链接即可，含 `portfolioId`）。
3. **删掉并重建 API Key**（它已经出现在对话记录里）。

拿到后我会立即验证：
- 你的跟单组合列表 / 持仓 / 成交；
- 私有接口的真实刷新延迟（是不是也 121s）；
- 能否用 `hasCopy` / `copyPortfolioId` 定位你正在跟的带单员。

---

## 附：复现命令

```bash
python probe\probe_dump.py                  # 公开端点 schema
python probe\probe_positions.py             # 扫公开持仓的带单员
python probe\probe_measure.py               # 体积/限频
python probe\probe_clean_lag.py             # 121s 延迟复测
python probe\api_check.py                   # 账户只读探测（需 env）
python probe\cookie_probe.py                # 私有接口探测（需 session.json）
```
