# 币安跟单工具 v0.1.0

把币安「跟单交易」的成交事件实时接到本地：**监控 / 复制 / 推送 / 自动跟单**，带一个桌面控制台。

> 纯本地运行，凭证只存在你自己的机器上。

---

## 功能

| 模块 | 说明 | 状态 |
|---|---|---|
| **仓位监控** | WebSocket 实时接收你所有跟单组合的成交（约 100ms），推 Telegram | ✅ |
| **仓位复制** | 按净值比 / 固定倍数 / 固定保证金算出「执行账户该下多少」，含风控 | 影子模式 ✅，实盘待接入 |
| **仓位推送** | Telegram 已接；QQ / 微信 / 钉钉 / Webhook 预留 | ✅ |
| **自动跟单** | 监控收藏带单员名额，出现空位自动加入 | ✅ |

---

## 快速开始

```bat
python -m pip install websockets
copy .env.example .env
python gui\启动.bat
```

> 当前版本 v0.1.0，更新内容见 [CHANGELOG.md](CHANGELOG.md)。

首次使用三步：

1. **仓位推送** 页填 Bot Token / Chat ID → 测试发送 → 保存
2. **仓位监控** 页点「更新登录态」，按提示从浏览器复制 cURL
3. 列表里点「监控 / 复制」勾选 → 点「启动」

---

## 关键设计

**信号源是「你自己的跟单账户」。**
本工具监听的是官方跟单镜像到你账户的成交，不是带单员的原始仓位。
好处是不需要带单员授权；代价是它只能当探针，要自定义比例必须另开执行账户。

**WebSocket 走 `/private/stream`。**
币安已改版：旧路径 `/ws/<listenKey>` 能握手但不下发数据；
正确做法是连 `/private/stream` 后发 `SUBSCRIBE [listenKey]`，事件还包了一层 `{stream, data}`。

**影子模式先跑。**
复制模块默认只计算不下单，记录「目标数量 / 盘口估算成交价 / 滑点 bps」，
跑够样本再决定是否实盘。

---

## 目录

```
bot/
  listen_copy.py    WebSocket 监听 + 推 Telegram
  copy_run.py    复制运行器（影子/干跑/实盘）
  copy_engine.py    比例换算 / 风控 / 盘口滑点
  auto_join.py      名额监控 + 自动加入
  update_session.py 浏览器 cURL → 登录态
  copy_report.py  复制日报
gui/
  app.py            桌面控制台（四页）
docs/
  配置与使用说明.md
  仓位复制页设计.md
```

---

## 安全

- `.env`、`session.json`、`gui/copy_config.json` 全部在 `.gitignore` 里
- 登录态等同账号权限，只在本地使用，不要放服务器
- 实盘前给 API Key 关掉提现、绑定 IP 白名单

---

## 免责声明

仅供个人学习与自用。跟单交易风险极高，任何损失由使用者自行承担。
本项目使用币安网页端私有接口读取数据，接口变更或平台限制可能导致功能失效。
