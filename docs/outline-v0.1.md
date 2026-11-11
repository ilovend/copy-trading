# 币安带单「自建按比例复制」系统 — 方案大纲 v0.1

> 目标：不依赖币安官方跟单，按自定义比例/自定义风控复制某个带单员的开平仓，且延迟尽量低。
> 状态：调研稿，未开始编码。日期 2026-11-10。

---

## 0. 结论先行（TL;DR）

1. **能做，但"自建 = 更低延迟"是伪命题。**
   官方跟单是币安撮合引擎内部行为，跟单成交与带单员成交几乎同时（毫秒级）。任何外部复制都要经过
   「带单员成交 → 数据落到可读接口 → 你拉到 → 你决策 → 你下单 → 你成交」，
   物理下限就是秒级，实测普遍 1–30 秒。**自建买到的是"比例自由 + 风控自由"，不是"更快"。**

2. **币安没有"查询带单员持仓"的官方 API。**
   官方 Copy Trading API 只有两个端点，且都是**带单员自己**用的：
   - `GET /sapi/v1/copyTrading/futures/userStatus`（我是否为带单员）
   - `GET /sapi/v1/copyTrading/futures/leadSymbol`（带单币种白名单）
   没有 follower→lead 的持仓查询，也没有 WebSocket 推送。所有"实时看到带单员开仓"的方案，
   底层都是网页私接口 `bapi/futures/v1/...`（Cookie + csrftoken），非公开契约。

3. **真正值得做的架构是「双账户」：官方跟单账户当探针，普通合约账户当执行。**
   如果验证通过（见 §9 Phase 0），这是唯一能同时拿到"低延迟"和"自定义比例"的路子；
   若验证失败，退化为纯轮询方案，接受 1–5 秒延迟。

4. **GitHub 上没有开箱即用的"币安带单员按比例复制"成熟库**，只有可拼装的轮子（§5）。
   最贴近的参考实现是 `terryso/nof1-tracker`（轮询外部信号 → 差分 → 币安合约执行，MIT）。

5. **⚠️ 当前仓库有已提交的明文凭证**（Cookie、csrftoken、Telegram Bot Token、fvideo token），
   且 remote 指向公开 GitHub。这是比跟单延迟更紧急的问题，见 §8.3。

---

## 1. 事实核查：能力边界

| 能力 | 官方 API | 网页私接口 bapi | 说明 |
|---|---|---|---|
| 查带单员当前持仓 | ❌ | ✅（Cookie） | 无公开文档，前端改版即失效 |
| 查带单员历史成交流 | ❌ | ✅（Cookie） | 有分页，适合回补/对账，不适合实时 |
| 订阅带单员成交推送 | ❌ | ❌ | 币安未开放任何 WS |
| 查自己跟单组合持仓 | ❌（待验证） | ✅ | **Phase 0 核心验证项** |
| 自己下单/平仓 | ✅ `/fapi/v1/order` | — | 标准合约 API，合规 |
| 自己的成交推送 | ✅ USER_DATA WS | — | 毫秒级，但只推自己账户 |
| 跟单名额/是否满员 | ❌ | ✅（已在现有脚本用到） | |

**已知的、来自现有脚本的可用端点（实测过）：**
- `POST /bapi/futures/v1/friendly/future/copy-trade/home-page/query-list` — 收藏带单员列表 + 名额
- `POST /bapi/futures/v1/private/future/copy-trade/copy-portfolio/create` — 创建跟单

**待实测确认的候选端点（不要当事实，Phase 0 用浏览器抓包确认）：**
- 带单员详情 / 当前持仓 / 持仓历史：`/bapi/futures/v1/friendly/future/copy-trade/lead-portfolio/*`、`.../lead-data/*`
- 我的跟单组合持仓 / 成交：`/bapi/futures/v1/private/future/copy-trade/copy-portfolio/*`

> 注意：这类接口的参数名和路径随前端版本变化，且带 WAF（`aws-waf-token`）。
> 现有脚本 2–3 天就要换一次 Cookie，这是运维成本的主要来源。

---

## 2. 延迟链路拆解（决定方案上限）

```
带单员点击开仓
   │  ① 撮合（币安内部，~ms）
   ▼
带单员成交写入「可对外读」的数据（持仓缓存 / 历史表）
   │  ② 落库 + 缓存刷新（未知，估计 0.5–5s，可能是最大黑箱）
   ▼
你轮询到这次变化（③ 轮询间隔 + RTT）
   │
   ▼
差分/风控/算量（④ 本地，~1–5ms）
   │
   ▼
下单到币安（⑤ RTT + 撮合，东京机房 30–80ms；国内直连 200–500ms）
   │
   ▼
你的成交
```

现实数字（第三方项目 `sweetcornna/copy-trader` 的信号源调研表，供参考）：

| 信号源 | 实时性 | 鉴权 |
|---|---|---|
| Hyperliquid 链上 | <200ms | 无 |
| OKX 带单（公开接口） | ~1s | 无 |
| Bitget 带单 | ~1s | 无 |
| **币安带单（bapi 私接口）** | **5–30s** | Cookie + 住宅 IP |
| 币安隐藏持仓 | 不可控 | 用户 Cookie |

**关键判断：延迟的 90% 以上在②③，不在你的机房和代码。**
所以优化顺序是「换信号源」>「缩短轮询」>>>「买东京服务器」。

---

## 3. 三条技术路线

### 路线 A —— 官方跟单打底 + 自定义比例叠加（Overlay）
官方跟单用最小金额跑着（毫秒级、自动跟随平仓，当作"底仓/保险"），
同时用普通合约账户按目标比例**补差量**：目标名义 − 官方已复制名义 = 需要补的手数。

- ✅ 延迟最低：基线由官方保证，你只补差额
- ✅ 平仓风险小：官方那份一定跟得上
- ❌ 双份手续费；比例只能"加"不能"减"；两次成交价不同
- ❌ 补仓/减仓决策仍依赖读到自己跟单账户的状态 → 仍需 Phase 0 验证
- 适用：只想放大敞口、能接受费用的人

### 路线 B —— 全自建：轮询带单员 + 自有风控 + 自己下单
- ✅ 完全自由：比例、杠杆、止损、黑名单、反向跟单、多交易所
- ✅ 合规（执行侧全用官方 API）
- ❌ 延迟 1–30s，且信号源是私接口，随时失效
- ❌ 带单员「隐藏持仓」你根本看不到；带单员真实保证金/杠杆也不可见
- 适用：中长线（持仓小时级以上）、或想做成产品/多带单员

### 路线 C —— 信号账户探针（关键，需验证）
用极小资金官方跟单该带单员，**把自己账户的成交当成信号源**，
再用另一个账户按自定义比例执行。

- ✅ 若 USER_DATA WS 能看到跟单成交 → 延迟 ≈ 官方撮合 + 你的 RTT，**亚秒级**，远超轮询
- ✅ 官方 API + WS，完全合规，不怕前端改版
- ❌ 前提未验证：币安"跟单账户"与普通合约账户是否隔离、API Key 能否读到
- ❌ 最小跟单金额占用资金；带单员满员时开不了探针
- 适用：**如果 V1 验证通过，这就是首选架构**

> A 和 C 都卡在同一个验证点上：**我能不能用 API 读到自己跟单账户的持仓/成交，多快？**
> 这一项验证结果决定整个项目的技术选型。

---

## 4. 推荐架构（以 B 为骨架，C 为加速器）

```
                    ┌──────────────────────────────────────────┐
   Probe 账户 ──WS──▶│  Signal Adapter                          │
   (路线C)           │   ├─ probe_ws   (USER_DATA, 亚秒)        │
                    │   ├─ bapi_poll  (带单员持仓, 1–5s)        │
                    │   └─ history    (历史回补/对账)           │
                    └───────────────┬──────────────────────────┘
                                    ▼
                    ┌──────────────────────────────────────────┐
                    │  Normalizer  统一事件模型                 │
                    │  OPEN/INCREASE/REDUCE/CLOSE/REVERSE      │
                    └───────────────┬──────────────────────────┘
                                    ▼
                    ┌──────────────────────────────────────────┐
                    │  Diff Engine  仓位指纹 + 去重 + 状态机     │
                    └───────────────┬──────────────────────────┘
                                    ▼
                    ┌──────────────────────────────────────────┐
                    │  Policy/Risk  比例、白名单、滑点、陈旧信号 │
                    │  Sizer        手数取整、最小名义、保证金   │
                    └───────────────┬──────────────────────────┘
                                    ▼
                    ┌──────────────────────────────────────────┐
                    │  Executor  下单（WS API 优先）/ 撤单/重试  │
                    └───────────────┬──────────────────────────┘
                                    ▼
                    ┌──────────────────────────────────────────┐
                    │  Reconciler  30–60s 对账，修正孤儿仓       │
                    │  Store/Alert  SQLite + TG 通知 + 指标     │
                    └──────────────────────────────────────────┘
```

**核心设计点：**

| 点 | 做法 |
|---|---|
| 仓位指纹 | `symbol + positionSide + entryOid/updateTime + qty`，用于去重与识别"这次是新事件还是轮询重复" |
| 状态机 | FLAT → OPENING → OPEN → REDUCING → FLAT；反手必须"先 reduceOnly 平完再开" |
| 幂等 | 每个信号生成 `intent_id`，执行前查本地库，避免重试导致双开 |
| 陈旧信号 | 信号年龄 > `max_signal_age`（默认 3s）直接丢弃，宁可不跟也不追高 |
| 滑点保护 | 限价 = 带单员开仓价 ± tolerance（0.2–1%）；超时未成交 → 撤单或转市价（可配） |
| 手数 | 按 `stepSize` / `minNotional` / `maxQty` 取整；不足最小名义则跳过并告警 |
| 平仓 | 一律 `reduceOnly=true`，数量取 min(目标减仓量, 当前持仓)，防反向开仓 |
| 对账 | 定期比对"我的实际持仓"与"目标持仓"，偏差超阈值 → 修正或只告警 |
| 熔断 | 连续 N 次下单失败 / 信号源 30s 无心跳 / 回撤超阈值 → 停止跟单并平仓（可配） |

**比例（Sizer）三种模式：**

```python
# 1) 净值比：最贴近"按比例跟单"，但需要带单员净值（只能用 AUM/累计收益近似）
qty_mine = qty_lead * (equity_mine / equity_lead)

# 2) 固定倍数：最可控，m 可 >1 或 <1
qty_mine = qty_lead * m

# 3) 固定保证金/笔：qty 由「每笔保证金 × 杠杆 ÷ 价格」倒算，最抗带单员仓位变化
qty_mine = (margin_per_trade * leverage) / entry_price
```

第三种种最实用（也是 `nof1-tracker --fixed-amount-per-coin` 的做法）：不关心带单员多大仓，只关心自己每笔下多少钱。

---

## 5. 可复用的轮子（GitHub 调研结果）

| 项目 | 语言 | 用途 | 可复用度 | 备注 |
|---|---|---|---|---|
| [binance/binance-connector-python](https://github.com/binance/binance-connector-python) / `binance-sdk-copy-trading` | Python | 官方 SDK，含 Copy Trading 模块 | ★★★★★ | 但 copyTrading 只有 2 个带单员端点，**不能查别人持仓** |
| [binance/binance-connector-js](https://github.com/binance/binance-connector-js) | TS | 同上 | ★★★★ | `@binance/copy-trading` |
| [terryso/nof1-tracker](https://github.com/terryso/nof1-tracker) | TS/Node | 轮询外部信号 → 差分识别开/平/换仓 → 币安合约执行；含 risk-only、价格容差、持仓管理、盈利统计 | ★★★★★ | **架构最贴近，直接抄设计**；MIT |
| [apollotoday/Binance-copy-trading-bot](https://github.com/apollotoday/Binance-copy-trading-bot) | Python | 抓币安排行榜带单员持仓 → Bybit 执行；MongoDB + TG；Cookie 2–3 天续期 | ★★★★ | 抓取层和 Cookie 生命周期可参考 |
| [sweetcornna/copy-trader](https://github.com/sweetcornna/copy-trader) | Python+Vue | 全栈跟单产品蓝图（6 信号源 / 4 交易所 / 20+ 风控参数 / 研究文档） | ★★★ | 看 `research/signal_sources_research.md` 的延迟对比表；代码未验证成熟度 |
| [ccxt/ccxt](https://github.com/ccxt/ccxt) | 多语言 | 统一交易所接口 | ★★★★ | 多交易所执行时用；单币安可直接用官方 SDK |
| [hummingbot](https://github.com/hummingbot/hummingbot) | Python | 做市/执行框架 | ★★ | 太重，跟单不是它的场景 |
| [freqtrade](https://github.com/freqtrade/freqtrade) | Python | 策略回测/实盘 | ★★ | 可借回测框架验证"跟单策略"本身有没有 alpha |
| [TopTrenDev/Copy-Trading-Bot](https://github.com/TopTrenDev/Copy-Trading-Bot) | Rust | 跟单机器人（偏 Hyperliquid/EVM） | ★★ | 低延迟参考 |

**结论：没有"币安带单员按比例复制"的现成方案，必须自己拼。**
骨架抄 nof1-tracker，抓取层抄 apollotoday，产品化参考 sweetcornna，执行层用官方 SDK。

---

## 6. 数据模型（最小集）

```sql
signal(id, source, portfolio_id, symbol, side, action, lead_qty, lead_price,
       event_time, recv_time, raw_json)            -- 原始信号，保留 7 天

position_target(symbol, side, qty, entry_price, leverage, updated_at)  -- 带单员目标态

position_mine(id, symbol, side, qty, entry_price, opened_at, source_intent)  -- 我的实际态

order_intent(id, signal_id, symbol, side, type, qty, price, reduce_only,
             status, created_at, sent_at, filled_at, err)

fill(id, intent_id, order_id, price, qty, fee, ts)

config(key, value)      -- 比例模式、白名单、滑点、熔断阈值
run_log(...)            -- 每个信号的完整决策链，事后复盘用
```

---

## 7. 风控参数清单（建议默认值）

| 类别 | 参数 | 默认 |
|---|---|---|
| 比例 | 模式（净值比/固定倍数/固定保证金） | 固定保证金 |
| 比例 | 每笔保证金 / 倍数 m | 50 USDT / 1.0 |
| 筛选 | 币种白名单/黑名单 | 空 |
| 筛选 | 只跟多 / 只跟空 / 双向 | 双向 |
| 筛选 | 单币最大名义占比 | 20% |
| 执行 | 滑点容差 | 0.5% |
| 执行 | 信号最大年龄 | 3s |
| 执行 | 限价未成交处理（撤/转市价） | 撤单 |
| 执行 | 杠杆（跟随/固定） | 跟随，上限 10x |
| 执行 | 保证金模式 | 逐仓 |
| 平仓 | 跟平比例（可部分平） | 100% |
| 保护 | 单日最大亏损 / 最大回撤 | 5% / 10% |
| 保护 | 连续失败熔断 | 5 次 |
| 保护 | 信号源心跳超时 | 30s |
| 其他 | 反向跟单 | 关 |

---

## 8. 风险与合规

### 8.1 平台规则
- **执行侧**（`/fapi/v1/order`）用官方 API Key：合规。
- **信号侧**（`bapi/...` + 登录 Cookie）属于对网页接口的自动化访问，**不在公开 API 条款内**。
  历史上有人因此被限流、要求二次验证，极端情况限制账户。这是本项目最大的非技术风险。
- 官方 Copy Trading API 明确只服务带单员自身，绕开它去读别人的仓位没有官方背书。

### 8.2 交易风险
- **隐藏持仓**：带单员可对非跟单用户隐藏持仓，此时外部完全看不到，只有路线 C 可行。
- **信号滞后**：3 秒延迟在快速行情里等于滑点几个点，可能把策略的 edge 吃光。
- **看不到真实杠杆/保证金**：只能从公开的"仓位数量 × 开仓价"推算名义，推算可能与实际有偏差。
- **带单员改仓/撤单**：公开持仓是快照，短时间的挂单撤销、部分成交你未必看得到。
- **最小跟单金额、名额限制**：探针账户也可能因为满员而开不出来。

### 8.3 ⚠️ 当前仓库的安全问题（最高优先级）
`Binance-Join-CopyTrader-Automation/Binance_Join_CopyTrader_Automation.py` 里硬编码了：
- 完整登录 Cookie（含 `aws-waf-token`、`BNC_FV_KEY`）
- `csrftoken`、`fvideo-id/token`
- Telegram Bot Token + chat_id

而 `git remote` 指向 `github.com/asamgx/Binance-Join-CopyTrader-Automation`。
**处理顺序：① 立刻在币安 App 退出所有会话 / 改密；② 重新生成 Telegram Bot Token；
③ `git rm --cached` + 清历史（filter-repo）后再推；④ 全部迁到 `.env` + `.gitignore`。**

---

## 9. 下一步：Phase 0 可行性验证（1 天，不写业务代码）

只写一个探针脚本 + 抓包，回答 4 个问题：

| # | 验证项 | 方法 | 判定 |
|---|---|---|---|
| V1 | 官方 API 能否看到**自己跟单账户**的持仓？ | 跟单后调 `/fapi/v2/positionRisk`，对比 App 显示 | 能 → 路线 C 可行 |
| V2 | USER_DATA WS 是否推送跟单成交？ | 订阅 listenKey，让带单员开一单观察 | 能 → **亚秒级方案成立** |
| V3 | `bapi` 读带单员持仓的真实延迟？ | 每 200ms 打一次，记录 `recv_time - 持仓 updateTime`，统计 P50/P95 | 得基线数字 |
| V4 | 私接口限频/封禁边界？ | 逐步加压 + 观察 429/WAF 挑战 | 得安全轮询频率 |

**输出物：** `docs/phase0-report.md`（含延迟分布图 + 可行性结论 + 推荐架构）。
**然后**再决定投不投 M1。

如果 V1/V2 都为否，唯一可行的是路线 B，且要接受"延迟 1–30s、Cookie 长期维护"这两件事。
如果 V1/V2 为真，项目价值立刻上一个台阶——**这才是值得投入的方向**。

---

## 10. 里程碑（待 Phase 0 后细化）

| 里程碑 | 内容 | 预估 | 验收 |
|---|---|---|---|
| M0 | Phase 0 探针 + 报告 | 1 天 | 4 个验证项有明确结论 |
| M1 | 信号层：bapi 抓取 + 归一化 + 落库 | 2–3 天 | 能稳定拿到带单员持仓变化，延迟可量化 |
| M2 | 执行层：Sizer + Executor + 精度处理 | 3–4 天 | Testnet 上能按比例开平，含限价/滑点保护 |
| M3 | 风控 + 对账 + 熔断 | 2–3 天 | 断网/重启/重复信号下不重复下单、不裸奔 |
| M4 | 观测：日志、TG 告警、指标面板 | 1–2 天 | 每个信号的决策链可复盘 |
| M5 | 部署（东京 VPS）+ 小资金实盘 | 1 天 | 连续 3 天稳定，偏差 < 阈值 |
| M6 | 多带单员 / 多账户 / 反向跟单 | 按需 | — |

技术栈建议：**Python 3.11 + asyncio + httpx(HTTP/2) + websockets + pydantic v2 + SQLite**（单机足够，
不必上 PostgreSQL/Redis，除非要多账户并发）。

---

## 11. 需要你拍板的 5 件事

1. **替换还是叠加？** 停掉官方跟单全自建（路线 B），还是保留官方跟单只做放大（路线 A）？
2. **持仓周期多长？** 分钟级 scalping → 自建基本没意义（延迟吃掉一切）；小时级以上 → 可行。
3. **资金规模和可接受回撤？** 决定探针账户最小金额和熔断阈值。
4. **是否接受 Cookie 私接口的账号风险？** 不接受的话只剩路线 C（若 V1/V2 通过）或第三方平台。
5. **单带单员还是多带单员/多账户？** 决定要不要上 Redis/Postgres 和进程隔离。

---

## 附：一句话总结

> 想做"低延迟 + 自定义比例"，唯一有希望的路是**用官方跟单当探针、用官方 API 做执行**；
> 纯轮询方案能做，但延迟由币安的数据刷新决定（5–30s），你再优化代码也追不回来。
> 所以先花 1 天验证 V1/V2，再决定要不要写后面几周的代码。
