"""自研策略的参数默认值与读写。

GUI 和运行进程共用这一份 DEFAULTS，避免两处默认值漂移（复制模块踩过这个坑）。
配置文件：gui/strategy_config.json（不进仓库）
"""
from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path

import strategy_registry as REG

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "gui" / "strategy_config.json"

INTERVALS = ["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h",
             "6h", "8h", "12h", "1d", "3d", "1w", "1M"]

DEFAULTS: dict = {
    "mode": "shadow",                 # shadow 只记录 / live_dry 打印不发送 / live 实盘
    "strategy": "example",           # 当前策略（见 strategy_registry）
    "direction": "short",             # short 只做空 / long 只做多 / both 多空都做
    "interval": "1m",                 # K 线周期（执行/管理都用它）
    "signal_interval": "1m",          # 信号判断周期：只在它的收盘时看信号
    "universe": {
        "top_n": 15,                  # 涨幅榜取前 N 个
        "min_change_pct": 3.0,        # 24h 涨幅下限
        "min_quote_volume": 50e6,     # 24h 成交额下限（USDT），过滤没人交易的
        "refresh_min": 5,             # 榜单多久重扫一次
        "sort_by": "change",          # change=按涨幅榜 / volume=按成交额（剥头皮用）
    },
    "signal": {
        "vol_mult": 4.0,              # 本根量 / 前 60 根量中位数
        "tr_mult": 2.5,               # 本根真实波幅 / 前 60 根 TR 中位数
        "pump_pct": 1.0,              # 过去 pump_lookback 根内最大涨幅 %
        "pump_lookback": 10,
        "body_max_ratio": 0.35,       # 实体 / 振幅 上限（1/3）
        "upper_min_ratio": 0.45,      # 上影 / 振幅 下限
        "lower_max_ratio": 0.30,      # 下影 / 振幅 上限
        "min_range_atr": 1.2,         # 本根振幅至少是 TR 中位数的几倍
        "cooldown_bars": 10,          # 同币种冷却根数
        "htf_enabled": True,          # 长周期位置过滤：单根形态必须站在 HTF 高位
        "htf_minutes": 15,            # HTF 周期（由 1m 聚合，无需额外订阅）
        "htf_lookback": 12,           # HTF 回看根数，用来算区间高低点
        "htf_ema": 12,                # HTF 均线周期
        "htf_high_zone": 0.75,        # 信号高点必须落在 HTF 区间的这个分位以上
        "htf_ext_atr": 0.8,           # 收盘价相对 HTF EMA 至少偏离几个 ATR
    },
    # 垂直拉升拉黑：当天不再交易该币，次日自动解除
    "runaway": {
        "enabled": True,
        "entry_1h_pct": 20.0,         # 1h 涨幅达到就判定为垂直拉升
        "entry_15m_pct": 10.0,        # 或 15m 涨幅达到
        "max_bans": 0,                # 每天最多拉黑几个币（0 = 不限，见一个拉一个）
    },
    "trade": {
        "notional": 10.0,             # 每笔名义（U）
        "post_stop_guard": True,      # 止损后价格没跌回去，不再追空同一个币
        "max_positions": 2,           # 最多同时持有几个币的仓位
        "add_window_s": 120,          # 首笔后多少秒内允许加仓
        "max_adds": 2,                # 最多加仓几次（总笔数 = 1 + max_adds）
        "add_min_gain_pct": 0.0,      # 加仓价相对上一笔至少高多少%（做空，越高越好）
        "tp1_pct": 0.6,               # 第一档止盈 %
        "tp1_frac": 0.5,              # 第一档平掉当前仓位的比例
        "tp2_pct": 1.2,               # 第二档止盈 %
        "tp2_frac": 0.5,              # 第二档平掉当时剩余仓位的比例
        "trail_activate_pct": 0.8,    # 盈利达到这个 % 后启动追踪
        "trail_gap_pct": 0.3,         # 追踪回撤（百分点），触发即平剩余
        "sl_pct": 0.5,                # 止损 %
        "max_hold_s": 180,            # 最长持仓秒数（从首笔算），到点无条件平
        "max_daily_loss": 5.0,        # 当日已实现亏损上限（U），超了停止开仓
        "max_slippage_pct": 0.3,      # 盘口相对信号价的滑点上限
        "max_spread_pct": 0.3,        # 买卖价差超过这个 % 就不开仓
        "leverage": 10,
        "manage_leverage": False,     # 默认不碰账户全局杠杆
        "ioc_buffer_pct": 0.05,       # IOC 挂对手价的缓冲
        "entry_mode": "taker",        # taker=追价 IOC 必成 / maker=挂买一价等成交（省费率）
        "entry_timeout_s": 5,         # maker 挂单多久不成交就撤
        "tp_mode": "taker",           # taker=到价 IOC 平 / maker=挂限价等成交
        "cutoff": "23:55",            # 到这个时间强平并停止开仓（不隔夜）
    },
    "push": True,                     # 开/平仓推 Telegram
    "heartbeat_min": 60,              # 存活心跳（分钟），0 = 关
    "feed": "auto",                   # 行情来源：ws / rest / auto（auto = WS + REST 兜底）
    "rest_poll_s": 5,                 # REST 兜底轮询间隔（秒）
    "example": {"fast": 20, "slow": 60},
    "basket": {
        "enabled": False,
        "protect": True,              # 交易所侧条件单：开仓后自动挂止盈/止损
        "sl_working_type": "MARK_PRICE",     # 止损按标记价（防插针扫损）
        "tp_working_type": "CONTRACT_PRICE",  # 止盈按最后成交价（插针也能吃到）
        "notional": 5.0,              # 每笔加仓名义
        "add_step_pct": 1.26,         # 相对均价逆行多少加一笔
        "max_adds": 3,                # 最多加几次（总笔数 1+max_adds）
        "basket_tp_pct": 0.5,         # 均价盈利多少全平
        "trail_activate_pct": 0.0,    # 浮盈到多少启动追踪（0=关闭）
        "trail_gap_pct": 0.5,         # 峰值回撤多少 pp 平仓
        "trail_partial_pct": 0.0,     # 到多少先平一部分（0=不分批）
        "trail_partial_frac": 0.5,    # 分批平掉的比例
        "trail_atr_mult": 0.0,        # >0 时回撤 = 倍数×ATR%（0=固定 trail_gap_pct）
        "trail_atr_min": 0.15,        # ATR 回撤下限 pp
        "trail_atr_max": 0.6,         # ATR 回撤上限 pp
        "hard_stop_pct": 6.0,         # 均价逆行多少强平（保命）
        "max_hold_s": 3600,           # 时间止损
    },
    # 两段式：长周期出信号进「预备」，短周期急剧放量才触发（与上面的单根形态二选一）
    "setup": {
        "enabled": False,
        "htf_minutes": 30,            # 预备周期
        "lookback": 12,               # 区间回看根数
        "ema": 12,
        "high_zone": 0.8,             # 收盘位于区间高位才算预备
        "ext_atr": 1.0,               # 相对 HTF 均线偏离的 ATR 倍数
        "arm_bars": 2,                # 预备有效期（HTF 根数）
        "trig_vol_mult": 3.0,         # 1m 触发需要的放量倍数
        "trig_max_drop_pct": 0.5,     # 现价跌离预备高点超过这个 % 就不再追
        "trig_bearish": True,         # 触发根要求收阴
    },
}

# 只保留回放为正期望的复刻策略；剥头皮/反转回放为负，已撤下
PRESETS: dict[str, dict] = {
    "example": {
        "strategy": "example",
        "mode": "shadow",
        "interval": "1m",
        "signal_interval": "1m",
        "example": {"fast": 20, "slow": 60},
    },
}

PRESET_LABELS: dict[str, str] = {"example": "示例：均线交叉"}


def _load_private_presets() -> None:
    """运行时合并 private/presets.py；公开仓库没有该目录则跳过。"""
    import importlib.util
    if os.environ.get("STRATEGY_DISABLE_PRIVATE") == "1":
        return
    p = ROOT / "private" / "presets.py"
    if not p.exists():
        return
    try:
        spec = importlib.util.spec_from_file_location("private_presets", p)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        presets = getattr(mod, "PRESETS", None)
        labels = getattr(mod, "PRESET_LABELS", None)
        if isinstance(presets, dict):
            PRESETS.update(presets)
        if isinstance(labels, dict):
            PRESET_LABELS.update(labels)
    except Exception as e:  # noqa: BLE001
        print(f"[preset] private/presets.py 加载失败: {e}", file=sys.stderr)


_load_private_presets()


def apply_preset(cfg: dict, name: str) -> dict:
    """把预设叠加到当前配置上（只覆盖预设里出现的键）。"""
    preset = PRESETS.get(name)
    if not preset:
        return cfg
    return _merge(cfg, preset)


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _warn(msg: str) -> None:
    print(f"[config] {msg}", file=sys.stderr)


def _sanitize(cfg: dict) -> dict:
    """按 DEFAULTS 校验类型，修正明显越界的值，并告警。"""
    def walk(node: dict, dnode: dict, path: str) -> None:
        for k, dv in dnode.items():
            if k not in node:
                node[k] = copy.deepcopy(dv)
                continue
            v = node[k]
            if isinstance(dv, dict):
                if not isinstance(v, dict):
                    _warn(f"{path}{k} 应为对象，已重置")
                    node[k] = copy.deepcopy(dv)
                    continue
                walk(v, dv, f"{path}{k}.")
            elif isinstance(dv, bool):
                if isinstance(v, str):
                    node[k] = v.strip().lower() in ("1", "true", "yes", "on")
                elif not isinstance(v, bool):
                    node[k] = bool(v)
            elif isinstance(dv, int):
                try:
                    node[k] = int(float(v))
                except (TypeError, ValueError):
                    _warn(f"{path}{k}={v!r} 不是数字，已用 {dv}")
                    node[k] = dv
            elif isinstance(dv, float):
                try:
                    node[k] = float(v)
                except (TypeError, ValueError):
                    _warn(f"{path}{k}={v!r} 不是数字，已用 {dv}")
                    node[k] = dv
            elif isinstance(dv, str) and not isinstance(v, str):
                node[k] = str(v)
    walk(cfg, DEFAULTS, "")

    enums = {"mode": {"shadow", "live_dry", "live"},
             "strategy": set(REG.ids()),
             "direction": {"short", "long", "both"},
             "interval": set(INTERVALS),
             "signal_interval": set(INTERVALS)}
    for key, allowed in enums.items():
        if cfg.get(key) not in allowed:
            _warn(f"{key}={cfg.get(key)!r} 不支持，已用 {DEFAULTS[key]}")
            cfg[key] = DEFAULTS[key]
    u = cfg.get("universe") or {}
    if u.get("sort_by") not in ("volume", "change"):
        _warn(f"universe.sort_by={u.get('sort_by')!r} 不支持，已用 change")
        u["sort_by"] = "change"
    t = cfg.get("trade") or {}
    for key in ("entry_mode", "tp_mode"):
        if t.get(key) not in ("maker", "taker"):
            _warn(f"trade.{key}={t.get(key)!r} 不支持，已用 taker")
            t[key] = "taker"
    cutoff = str(t.get("cutoff", "")).strip().lower()
    if cutoff not in ("", "off", "none", "0") and ":" not in cutoff:
        _warn(f"trade.cutoff={cutoff!r} 不是 HH:MM，已用 23:55")
        t["cutoff"] = "23:55"

    ranges = [("trade", "notional", 0.1, None), ("trade", "max_positions", 1, None),
              ("trade", "max_adds", 0, None), ("trade", "sl_pct", 0.01, None),
              ("trade", "max_daily_loss", 0, None),
              ("trade", "max_hold_s", 1, None),
              ("basket", "notional", 0.1, None), ("basket", "max_adds", 0, None),
              ("basket", "add_step_pct", 0.01, None),
              ("basket", "basket_tp_pct", 0.01, None),
              ("basket", "hard_stop_pct", 0.1, None),
              ("fade", "entry_pct", 0.0, None), ("fade", "entry_15m_pct", 0.0, None),
              ("fade", "lookback_min", 5, None),
              ("runaway", "entry_1h_pct", 0.0, None),
              ("runaway", "entry_15m_pct", 0.0, None),
              ("runaway", "max_bans", 0, None)]
    for section, key, lo, hi in ranges:
        sec_defaults = DEFAULTS.get(section)
        if not isinstance(sec_defaults, dict) or key not in sec_defaults:
            continue          # 私有策略字段：公开版 DEFAULTS 不登记，跳过校验
        node = cfg.get(section) or {}
        v = node.get(key)
        if not isinstance(v, (int, float)) or (lo is not None and v < lo) or \
                (hi is not None and v > hi):
            default = (DEFAULTS.get(section) or {}).get(key)
            _warn(f"{section}.{key}={v!r} 越界，已用 {default}")
            node[key] = default
    return cfg


def load() -> dict:
    if CONFIG_PATH.exists():
        try:
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
            return _sanitize(_merge(DEFAULTS, raw))
        except Exception:  # noqa: BLE001
            pass
    return _sanitize(copy.deepcopy(DEFAULTS))


def save(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")