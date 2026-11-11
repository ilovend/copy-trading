# 跟单成交 → Telegram 实时推送（快速参考）

完整配置说明见 **[../docs/配置与使用说明.md](../docs/配置与使用说明.md)**。

自动发现你所有跟单组合，每个组合一条 WebSocket，成交即推 Telegram。

---

## 三步跑起来

```bat
:: 1. 依赖
python -m pip install websockets

:: 2. 配置
copy .env.example .env      :: 填 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID

:: 3. 会话（2-3 天一次）
python bot\update_session.py --clipboard

:: 4. 启动
bot\start.bat
```

---

## `.env` 速查

| 参数 | 必填 | 说明 |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | ✅ | @BotFather 创建的机器人 token |
| `TELEGRAM_CHAT_ID` | ✅ | 你的 id，@userinfobot 可查 |
| `TELEGRAM_PROXY` | ⬜ | 访问 Telegram 的本地代理，留空直连 |
| `COPY_PORTFOLIO_ID` | ⬜ | 兜底组合 ID，仅在自动发现失败时使用 |
| `WATCH_NICKNAMES` | ⬜ | 只监听名字包含关键词的人，逗号分隔 |
| `WATCH_LEADS` | ⬜ | 按带单员 ID 筛选 |
| `WATCH_PORTFOLIOS` | ⬜ | 按组合 ID 筛选 |
| `BINANCE_COOKIE` | ⬜ | 登录态 cookie，替代 session.json（需与下一项同时填） |
| `BINANCE_CSRFTOKEN` | ⬜ | 与 cookie 配对，单独填 cookie 会 401 |
| `BINANCE_UA` | ⬜ | 自定义 UA，一般不用填 |

---

## 启动成功的标志

```
discovered 4 portfolios:
  玄冥二老        copy=5307462232747617537 lead=4908633203782592768
  ...
connected [玄冥二老(617537)]
connected [星辰社区-双子星(670145)]
connected [星辰社区-海(412353)]
connected [星辰社区-意钦(581121)]
```

手机收到「🚀 跟单监听已启动」。

---

## 消息格式

```
🟢 开仓 玄冥二老 · ETHUSDT 多
价格 2,032.19  数量 0.32
名义 650.30 U
延迟 96 ms  01:02:31
```

```
🔴 平仓 星辰社区-海 · ETHUSDT 多
价格 2,059.63  数量 0.32
盈亏 +8.78 U
延迟 88 ms  01:02:33
```

`rp`（realizedProfit）为 0 → 开/加仓；非 0 → 平/减仓。
`延迟` = 成交时间戳到本机收到事件的毫秒差。

---

## 数据

| 文件 | 内容 |
|---|---|
| `data/events-YYYYMMDD.jsonl` | 每笔成交（已解析 + 延迟 + 带单员） |
| `data/raw-YYYYMMDD.jsonl` | 全部原始 WS 报文 |
| `data/bot.log` | 进程日志（`start.bat` 启动时） |

---

## 维护

- **cookie 过期**（2–3 天）：`python bot\update_session.py --clipboard`，程序自动重载
- **新增/取消跟单**：重启一次（组合在启动时发现）
- **Telegram 慢**：在 `.env` 配 `TELEGRAM_PROXY=http://127.0.0.1:7890`
