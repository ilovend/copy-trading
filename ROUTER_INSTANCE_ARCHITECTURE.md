# 路由器多实例复制 + 自研策略：架构方案（供 Gemini 审查）

> 目标：把当前分散的 LuCI 页面、临时 cockpit、8787/8788 旧策略控制台，收敛成一套可长期维护的多实例系统。
> 本文只描述方案，不包含 `.env`、`session.json` 或 API 密钥。

---

## 1. 背景与现状

### 1.1 已有资产

- 路由器：OpenWrt / QWRT，Python 3.11。
- 旧策略 WebUI：
  - `http://192.168.10.1:8787/` → `/root/ct-router/webui`
  - `http://192.168.10.1:8788/` → `/root/ct-router-me/webui`
  - 两个都是“策略控制台”，Basic Auth，包含账户/API/session、持仓、策略参数、启停、日志。
- 复制引擎：
  - `bot/copy_run.py`
  - `bot/copy_engine.py`
  - `bot/listen_copy.py`
  - 旧 root 里已有复制代码，但没有接到 WebUI。
- 临时 cockpit：
  - `http://192.168.10.1:8091/`
  - `/root/ct-copy/instances/mom`
  - 只有复制配置，没有自研策略页；实例名硬编码为 `mom`，不应继续作为默认名。

### 1.2 现状问题

1. 8787/8788 只有策略控制，没有复制控制。
2. 8091 是临时复制 cockpit，和旧 WebUI 功能重复。
3. 固定端口 + 固定实例名（`mom`/`妈妈`）不符合“自定义端口、任意实例名”的目标。
4. 配置分散在 `copy_config.json`、策略配置、`.env`、`session.json`，容易互相覆盖。
5. 复制和自研策略如果同时 live，会争用同一个账户；目前没有统一账户锁展示。
6. 旧 8787/8788 的 UI 和策略逻辑是有价值的，不能丢。

---

## 2. 目标

```text
LuCI = 总控台/管家
每个自定义端口 = 一个实例的完整驾驶舱
一个实例 = 一个账户 = 复制引擎 + 自研策略引擎
```

- 保留自研策略全部逻辑。
- 保留 8787 的策略控制台 UI/交互，作为驾驶舱的页面基础。
- 复制和自研策略在同一实例内共存，但同一账户 live 互斥。
- 实例名、端口自定义，不再硬编码 `mom`、`妈妈`。
- LuCI 不编辑策略/复制参数，只负责生命周期。
- 全程浏览器配置，不需要 SSH/SCP 传 `.env`/session。
- 旧 8787/8788 固定服务在迁移验证后归档，不再作为固定入口。

---

## 3. 总体架构

```text
┌─────────────────────────────────────────────┐
│ LuCI 总控台                                  │
│ /admin/services/copy_trading                 │
│  - 新建实例（名称 + 自定义端口）              │
│  - 启停 Web / 复制引擎 / 策略引擎             │
│  - 一键全停所有引擎                           │
│  - Session/API 状态、日志、trash 恢复         │
└──────────────────┬──────────────────────────┘
                   │ 读取 instances.json / 调用服务
                   ▼
┌─────────────────────────────────────────────┐
│ 实例 a1（自定义端口 9010）                    │
│ ┌─────────────────────────────────────────┐ │
│ │ 驾驶舱 WebUI（复用 8787 策略控制台 UI）   │ │
│ │ 概览 | 账户 | 复制 | 自研策略 | 风控 | 通知 | 日志 │
│ └─────────────────────────────────────────┘ │
│ ┌──────────────┐  ┌──────────────────────┐  │
│ │ 复制引擎      │  │ 自研策略引擎          │  │
│ │ copy_run.py   │  │ strategy_run.py      │  │
│ └──────────────┘  └──────────────────────┘  │
│ 共享：instance.json / .env / session.json    │
└─────────────────────────────────────────────┘
```

### 3.1 进程模型

每个实例三个独立 procd 服务：

```text
copy-web-<id>          驾驶舱 WebUI（创建后即可用）
copy-copy-<id>         复制引擎
copy-strategy-<id>     自研策略引擎
```

- 新建实例只启动 `copy-web-<id>`，两个引擎默认停止。
- Web 重启不影响任何引擎。
- 复制/策略引擎分别启停、重启，不互杀。
- 每个引擎独立日志、独立 respawn，不通过 shell `&` 派生。
- 所有引擎共享账户锁 `TRADE_LOCK_PORT`（见 6.2）。

> 说明：不使用“一个 init 脚本双 `procd_open_instance`”，因为 procd 无法干净地单独重启其中一个实例；拆成三个服务更符合“Web 不杀、引擎独立”的要求。

### 3.2 实例目录

建议统一为：

```text
/opt/ct-app/                      只读代码（复制/策略/WebUI 共享）
/root/ct-instances/<id>/          实例数据
├── instance.json                 非敏感统一配置
├── .env                          API/通知密钥，0600
├── session.json                  登录态，0600
├── logs/
│   ├── web.log
│   ├── copy.log
│   └── strategy.log
└── data/                         成交/事件/状态
```

- 代码共享，避免每个实例复制一份 `bot/` 导致版本漂移。
- 通过 `INSTANCE_ROOT=/root/ct-instances/<id>` 让复制/策略/WebUI 读取实例数据。
- 如果短期不想改 `ROOT` 推导逻辑，可先保留“每实例复制一份代码”，但列为技术债。

---

## 4. 实例驾驶舱 UI

复用 8787 的页面结构和策略控制逻辑，扩展为：

```text
概览 | 账户 | 复制 | 自研策略 | 风控 | 通知 | 日志
```

### 4.1 概览

- 实例名 / 端口 / token 状态
- Web 状态
- 复制引擎状态
- 策略引擎状态
- Session 状态（正常 / 可能过期 / 缺失）
- API Key 状态
- 账户余额、可用保证金
- 当前持仓（全部 + 策略/复制标记）
- 今日成交/盈亏

### 4.2 账户

- `EXEC_API_KEY` / `EXEC_API_SECRET`
- Binance `session.json` 粘贴
- 账户别名
- 测试 API / 测试 Session
- 保存权限 `0600`
- 不在页面回显完整密钥

### 4.3 复制

复用 PC 端复制配置字段：

- 模式：`shadow / live_dry / live`
- 比例模式：`net_equity / fixed_notional / margin`
- 执行权益
- 单笔上限 / 总上限
- 比例 k
- 固定名义
- 每笔保证金
- 滑点
- 跟单组合列表
- 每个组合启用/禁用
- 最小名义处理

### 4.4 自研策略

复用 8787 策略控制台：

- 策略选择/参数
- 方向
- 信号参数
- 入场方式
- 加仓
- 追踪止盈
- 黑名单
- 策略启停/重启
- 策略日志

### 4.5 风控 / 互斥

- 当前账户锁（按 API Key 派生）
- 复制/策略 live 状态
- 同一账户 live 互斥提示
- 最大总名义
- 最大持仓数
- dry / live 状态一览
- “只允许一个 live 引擎”的硬约束

### 4.6 通知

- Telegram / 企业微信
- 通知级别
- 测试发送

### 4.7 日志

- `web.log`
- `copy.log`
- `strategy.log`
- 切换、刷新、下载
- 轮转：单文件 2MB，保留 3 份

---

## 5. LuCI 总控台

### 5.1 顶部

- 新建实例：实例名 + 自定义端口（默认下一个空闲端口）
- 一键全停所有引擎（只停复制/策略引擎，不停 Web，不平仓）

### 5.2 实例表

列：

```text
实例名 | 端口 | Web | 复制引擎 | 策略引擎 | 模式 | Session | 配置页 | 操作
```

操作：

- 打开配置页（带 token，新标签）
- 启动/停止/重启复制引擎
- 启动/停止/重启策略引擎
- 看 copy/strategy/web 日志
- 删除（引擎运行中禁止；移入 trash）

### 5.3 trash

- 列出已删除实例归档
- 恢复（重建目录 + 三个服务）
- 不直接物理删除

### 5.4 实例清单

`/root/ct-instances/instances.json`：

```json
{
  "schema_version": 2,
  "instances": [
    {"id": "a1", "web_port": 9010, "created": 0}
  ]
}
```

不使用固定 `mom` 模板；新建模板来自任意现有实例或 `seed` 模板。

---

## 6. 配置模型

### 6.1 统一实例配置

`instance.json`（非敏感）：

```json
{
  "schema_version": 2,
  "id": "a1",
  "web_port": 9010,
  "account": {
    "label": "账户A",
    "api_state": "configured"
  },
  "engines": {
    "active_live": "none",
    "copy": {
      "enabled": false,
      "mode": "live_dry",
      "ratio_mode": "net_equity",
      "exec_equity": 100.0,
      "max_order_notional": 10.0,
      "max_total_notional": 40.0,
      "k": 1.0,
      "targets": {},
      "monitored": []
    },
    "strategy": {
      "enabled": false,
      "config": {}
    }
  },
  "risk": {
    "max_positions": 3,
    "trade_lock": "auto"
  },
  "notify": {
    "telegram": true,
    "wechat": true
  }
}
```

- `.env` 存敏感值：`EXEC_API_KEY`、`EXEC_API_SECRET`、通知密钥、`WEB_TOKEN`。
- `session.json` 单独存登录态。
- 旧 `copy_config.json` 和策略配置通过迁移器导入。
- 页面只读写统一配置，不再出现两个页面改同一字段。

### 6.2 账户锁派生

```text
TRADE_LOCK_PORT = 47000 + crc32(EXEC_API_KEY[:12]) % 2000
```

- 同账户多实例 → 同一个锁 → live 自动互斥。
- 不同账户 → 不同锁 → 自动隔离。
- 不硬编码 `47620`。

### 6.3 live 互斥规则

```text
copy=live + strategy=live  → 拒绝启动第二个
copy=live + strategy=dry   → 允许
copy=dry  + strategy=live  → 允许
copy=dry  + strategy=dry   → 允许
```

风控页明确显示当前锁持有者。

---

## 7. 后端接口（驾驶舱）

复用 8787 现有接口，增加复制/风控：

```text
GET  /api/instance/summary
GET  /api/credentials
POST /api/credentials/apikey
POST /api/credentials/session

GET  /api/copy/config
POST /api/copy/config
GET  /api/copy/status
POST /api/copy/control        start/stop/restart

GET  /api/strategy/config
POST /api/strategy/config
GET  /api/strategy/status
POST /api/strategy/control    start/stop/restart

GET  /api/risk
GET  /api/positions
GET  /api/logs?type=copy|strategy|web
POST /api/notify/test
```

- 驾驶舱接口由实例 WebUI 提供，不经过 LuCI。
- LuCI 只调用 helper 做服务控制/清单/日志，不直接改配置。
- Token 通过 `?token=...` 写入 HttpOnly Cookie；未授权 403。
- 所有配置写入原子化，`.env`/session 权限 0600。

---

## 8. 迁移步骤

### Phase 0：冻结旧入口

- 8787/8788 旧固定服务停止、禁用；
- 源码目录不删，移入归档或保留为 UI 参考；
- 保留旧策略逻辑，不覆盖。
- 8091 临时 cockpit 在迁移验证后归档。

### Phase 1：共享后端 + 配置迁移

- 建立 `/opt/ct-app` 共享代码；
- 添加 `INSTANCE_ROOT` 支持；
- 把旧 `copy_config.json`/策略配置迁移到 `instance.json`；
- 保留兼容读取层，避免旧脚本立即失效。

### Phase 2：单实例驾驶舱

- 复用 8787 `index.html` + `app.py` 页面结构；
- 增加复制/风控/通知 tab；
- 接入 `copy-engine-<id>` / `strategy-engine-<id>` 服务；
- token auth。

### Phase 3：LuCI 总控台

- 实例清单；
- 创建（自定义端口、自动分配）；
- 启停三个服务；
- 一键全停引擎；
- trash/restore；
- 日志查看。

### Phase 4：迁移与清理

- 新建 `a1` 实例，迁移 8787 的账户/session/策略；
- 验证复制 + 策略 dry/live 切换；
- 归档 8787/8788/8091 固定入口；
- 最终只保留 LuCI + 任意端口实例。

---

## 9. 验收测试

1. LuCI 新建 `a1` / 9010；
2. 只启动 Web，两个引擎停止；
3. 无 token 访问 403；带 token 跳转 200；
4. 账户页保存 API/session，权限 0600；
5. 自研策略配置保存、启动 dry、日志正常；
6. 复制配置保存、启动 dry、日志正常；
7. 复制 live + 策略 live 被拒绝；
8. 复制 live + 策略 dry 允许；
9. 重启复制引擎，Web 和策略引擎不受影响；
10. 停止策略引擎，Web 和复制引擎不受影响；
11. 一键全停引擎，Web 全部保持；
12. 删除运行中引擎被拒绝；
13. 删除后进 trash，可恢复；
14. 日志轮转 2MB × 3；
15. 页面/日志/接口不泄露完整 API key、session、token。

---

## 10. 需要 Gemini 重点审查的问题

1. 共享代码 + `INSTANCE_ROOT` 是否比每实例复制代码更稳？
2. 三个 procd 服务（web/copy/strategy）是否合理，是否需要统一 supervisor？
3. 账户锁按 API Key 哈希是否足够，如何处理 API Key 轮换/多账户？
4. 复制和策略 live 互斥放在 UI 层还是后端强制？
5. `instance.json` schema 是否覆盖 PC 端所有配置，迁移兼容层怎么做？
6. 复用 8787 WebUI 时，如何最小改动接入复制/风控页？
7. Token + Cookie 的鉴权方案是否足够，是否需要 CSRF？
8. 旧 8787/8788 源码归档时机和回滚方案。
9. 如何处理 session 过期检测（仅 mtime 还是主动校验）？
10. 日志轮转、磁盘配额、异常 respawn 策略。

---

## 11. 明确不做的事

- 不删除自研策略逻辑。
- 不把 `mom`/`妈妈` 作为默认实例名或模板。
- 不保留 8787/8788 固定端口作为最终入口。
- 不在 LuCI 编辑策略/复制参数。
- 不允许同账户两个 live 引擎同时下单。

---

## 12. 一句话总结

```text
LuCI 管实例生命周期；
每个自定义端口是一个完整驾驶舱；
一个实例一个账户，复制与自研策略共存；
同一账户 live 互斥；
8787 的 UI/策略逻辑复用，固定端口和硬编码实例名退役。
```
