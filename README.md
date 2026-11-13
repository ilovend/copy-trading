# 币安跟单 + 策略框架

把币安「跟单交易」的成交事件实时接到本地，按你自己的比例在**执行账户**里复制：
**监控 / 复制 / 推送 / 自动跟单**，带一个桌面控制台。

> 纯本地运行，密钥只存在你自己的机器上。
>
> **作者**：ilovend · **License**：[MIT](LICENSE) · Copyright (c) 2026 ilovend
>
> ⚠️ **风险提示**：本项目仅用于技术研究与个人自动化，不构成投资建议。
> 加密货币合约交易具有高杠杆、高波动、可能全部亏损的风险；实盘前请自行审计代码、
> 使用小资金验证，并自行承担一切交易与合规后果。

---

## 功能

| 模块 | 说明 | 状态 |
|---|---|---|
| **仓位监控** | WebSocket 实时接收全部跟单组合的成交（实测约 100ms），推 Telegram | ✅ |
| **仓位复制** | 按比例 / 每笔固定金额计算执行账户该下多少，含风控与滑点保护 | ✅ 影子 / 干跑 / 实盘 |
| **仓位推送** | Telegram 已接；QQ / 微信 / 钉钉 / Webhook 预留 | ✅ |
| **自动跟单** | 监控收藏带单员名额，出现空位自动加入 | ✅ |
| **自研策略** | 插件式策略框架；公开示例 + `private/` 私有策略 | ✅ 影子 / 干跑 / 实盘 |

复制的三种运行模式：

| 模式 | 行为 |
|---|---|
| 影子 | 只计算、只记录，不碰交易所 |
| 干跑 | 打印完整订单参数，**不发送** |
| 实盘 | 真实下单（IOC 限价 + 双向持仓自适应） |

---

## 快速开始

```bat
python -m pip install -r requirements.txt
copy .env.example .env
python gui\启动.bat
```

首次使用三步：

1. **仓位监控** 页点「更新登录态」，按弹窗提示从浏览器复制 cURL
2. **仓位推送** 页填 Bot Token / Chat ID → 测试发送 → 保存
3. 列表里点「监控 / 复制」勾选 → 点「启动」

实盘还需要：点顶栏 **「账户配置」** 填 API Key / Secret（只存 `.env`），
点「测试连接」确认权益与持仓模式。复制与自研策略共用这一组 Key。

> 更新内容见 [CHANGELOG.md](CHANGELOG.md)。
> **币安接口变更（WS 入口、条件单等）见 [docs/API-CHANGES.md](docs/API-CHANGES.md)**，升级出问题先对照它。

---

## 关键设计

**信号源是「你自己的跟单账户」。**
监听的是官方跟单镜像到你账户的成交，不是带单员的原始仓位。
不需要带单员授权；代价是它只能当探针，要自定义比例必须另开执行账户。

**WebSocket 走 `/private/stream`。**
币安已改版：旧路径 `/ws/<listenKey>` 能握手但不下发数据；
正确做法是连 `/private/stream` 后发 `SUBSCRIBE [listenKey]`，事件还包了一层 `{stream, data}`。

**参数默认只问三件事。**
执行资金、跟单方式、单笔上限；总敞口、单币上限、滑点、杠杆按安全值自动推导。
需要细调时展开「高级设置」。

**主链路与旁路分离。**
算量和下单所需的四个调用（净投入、盘口、持仓、下单）同步执行；
Telegram 推送走有界异步队列，发不出去也不影响开平仓。

---

## 目录

```
bot/
  listen_copy.py    WebSocket 监听 + 成交推送
  copy_engine.py    比例换算 / 风控 / 盘口滑点 / 下单
  copy_run.py       复制运行器（影子 / 干跑 / 实盘）
  exec_client.py    币安合约签名客户端（查仓 / 杠杆 / 下单）
  auto_join.py      名额监控 + 自动加入
  update_session.py 浏览器 cURL → 登录态
  copy_report.py    复制日报
  strategy_run.py   自研策略：扫涨幅榜 / 订阅 K 线 / 下单
  strategy_engine.py 策略风控、下单、持仓管理
  strategy_volume_pinbar.py 放量急拉 + 上影反转信号（纯函数）
  strategy_config.py 策略参数默认值与读写（含类型/范围校验）
  strategy_registry.py 策略注册表（标题/字段/预设/信号方式）
  net.py            公开 HTTP（复用连接 + 线程锁）
  notify.py         .env 与 Telegram 推送
  bapi.py           币安网页私有接口（登录态 + bapi 请求）
  trade_lock.py     实盘互斥锁（复制/策略只能一个 live）
  symbol_filter.py  合约过滤：白名单 / 黑名单 / 自动拉黑
  version.py        版本号
gui/
  app.py            桌面控制台（五页）
  theme.py          深浅色主题、字体与统一配色
docs/
  配置与使用说明.md
  仓位复制页设计.md
  API-CHANGES.md    币安接口变更记录与复验方法
```

---

## 策略插件：公开框架 + 私有策略

公开仓库只带一个**示例策略** `bot/strategies/example.py`（均线交叉，仅演示接口）。
实盘策略放在本地 `private/strategies/`，不进 Git、不进 Release。

加载顺序：

1. `private/strategies/*.py`（存在则优先）
2. `bot/strategies/*.py`（公开示例）

### 写一个策略插件

新建 `bot/strategies/my_strategy.py`（或 `private/strategies/my_strategy.py`）：

```python
SPEC = {
    "id": "my_strategy",
    "name": "我的策略",
    "tab": "我的策略",
    "desc": "一句话说明",
    "kind": "plugin",
    "fields": (
        ("策略参数", (
            ("快线", "my_strategy.fast"),
            ("慢线", "my_strategy.slow"),
        )),
    ),
    "presets": ("my_preset",),
}

def signal(bars, cfg):
    """返回 {"side": "LONG"/"SHORT", "chg_1h": ..., "chg_15m": ...} 或 None。"""
    return None
```

可选实现 `simulate_basket(bars, i, basket, bar_s, side)` 供「回放」使用；
不实现时回放会退回通用的固定止盈/止损模拟。

预设写在 `strategy_config.py` 的 `PRESETS`（公开示例）或
`private/presets.py`（私有），并让 `SPEC["presets"]` 登记对应 id。

### 私有目录

```
private/
  strategies/      私有策略插件
  presets.py       私有预设（实盘参数）
  reports/         私有回测/对比报表
```

`private/` 已被 `.gitignore` 忽略。公开版没有它时，程序自动回退到示例策略，
GUI 和测试仍可正常启动。

## 安全

- API Key / Telegram token 只存在 `.env`，不进仓库
- `session.json`（网页登录态）等同账号权限，只在本地使用
- API Key 关掉提现权限、绑定 IP 白名单
- 换代理节点会导致 IP 白名单失效（报 `-2015`）

---

## 已知限制

- 没有独立止损、日亏熔断、连续失败熔断（自研策略模块有自己的止损与日亏熔断）
- 模拟台账不持久化，进程重启后清零（实盘查真实持仓，不受影响）
- 信号串行处理，带单员一次开多个币时可能因排队被丢弃（超过 8 秒判过期）
- 子账户杠杆上限 5x，实盘建议用主账户

---

## 免责声明

仅供个人学习与自用。跟单交易风险极高，任何损失由使用者自行承担。
本项目使用币安网页端私有接口读取数据，接口变更或平台限制可能导致功能失效。
