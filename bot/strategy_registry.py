#!/usr/bin/env python3
"""策略注册表：策略 = 一条注册 + 配置。

GUI 的标题、说明、参数字段、预设下拉，运行器选哪条信号路径，全部从这里读。
以后新增策略：写一个信号模块 + 在本文件 register 一条 StrategySpec，
不用改 GUI 或运行器骨架。

字段格式：(显示名, 配置路径)，配置路径用点号访问 DEFAULTS。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class StrategySpec:
    id: str
    name: str                       # 完整名称（页面标题）
    tab: str                        # 标签页短名
    desc: str                       # 一句话说明
    kind: str                       # 信号执行方式：fade / two_stage / pinbar
    fields: tuple = ()              # 该策略自己的参数字段
    presets: tuple = ()             # 关联的预设名


REGISTRY: dict[str, StrategySpec] = {}

# 所有策略共用的参数分组
COMMON_FIELDS = (
    ("扫描", (("候选池数量", "universe.top_n"),
              ("24h 涨幅下限 %", "universe.min_change_pct"),
              ("24h 成交额下限 (U)", "universe.min_quote_volume"),
              ("榜单刷新 (分钟)", "universe.refresh_min"),
              ("榜单排序", "universe.sort_by"))),
    ("交易与风控", (("方向 short/long/both", "direction"),
                    ("单笔名义 (U)", "trade.notional"),
                    ("最多同时持仓", "trade.max_positions"),
                    ("止损 %", "trade.sl_pct"),
                    ("最长持仓 (秒)", "trade.max_hold_s"),
                    ("当日亏损上限 (U)", "trade.max_daily_loss"),
                    ("滑点上限 %", "trade.max_slippage_pct"),
                    ("杠杆", "trade.leverage"),
                    ("IOC 缓冲 %", "trade.ioc_buffer_pct"),
                    ("强平时间 HH:MM", "trade.cutoff"),
                    ("入场方式", "trade.entry_mode"),
                    ("入场等待 (秒)", "trade.entry_timeout_s"),
                    ("止盈方式", "trade.tp_mode"))),
    ("加仓与分批止盈", (("加仓窗口 (秒)", "trade.add_window_s"),
                        ("最多加仓次数", "trade.max_adds"),
                        ("加仓最小涨幅 %", "trade.add_min_gain_pct"),
                        ("止盈1 触发 %", "trade.tp1_pct"),
                        ("止盈1 平仓比例", "trade.tp1_frac"),
                        ("止盈2 触发 %", "trade.tp2_pct"),
                        ("止盈2 平仓比例", "trade.tp2_frac"),
                        ("追踪启动 %", "trade.trail_activate_pct"),
                        ("追踪回撤 pp", "trade.trail_gap_pct"))),
)

FADE_FIELDS = (
    ("策略参数：复刻 · 暴涨做空 + 均价加仓", (
        ("1h 涨幅阈值 %", "fade.entry_pct"),
        ("15m 涨幅阈值 %", "fade.entry_15m_pct"),
        ("回看分钟", "fade.lookback_min"),
        ("做多放量倍数", "fade.long_vol_mult"),
        ("每笔名义 (U)", "basket.notional"),
        ("加仓步长 %", "basket.add_step_pct"),
        ("最多加仓次数", "basket.max_adds"),
        ("均价止盈 %", "basket.basket_tp_pct"),
        ("硬止损 %", "basket.hard_stop_pct"),
        ("最长持仓 (秒)", "basket.max_hold_s"),
        ("垂直拉升 1h %", "runaway.entry_1h_pct"),
        ("垂直拉升 15m %", "runaway.entry_15m_pct"),
        ("每日拉黑名额（0=不限）", "runaway.max_bans"))),
)

PINBAR_FIELDS = (
    ("策略参数：单根形态（放量急拉 + 上影反转）", (
        ("放量倍数 x", "signal.vol_mult"),
        ("波动放大 x", "signal.tr_mult"),
        ("拉升幅度 %", "signal.pump_pct"),
        ("拉升回看根数", "signal.pump_lookback"),
        ("实体/振幅 上限", "signal.body_max_ratio"),
        ("上影/振幅 下限", "signal.upper_min_ratio"),
        ("下影/振幅 上限", "signal.lower_max_ratio"),
        ("振幅/TR 下限", "signal.min_range_atr"),
        ("冷却根数", "signal.cooldown_bars"),
        ("HTF 周期(分钟)", "signal.htf_minutes"),
        ("HTF 回看根数", "signal.htf_lookback"),
        ("HTF 均线周期", "signal.htf_ema"),
        ("HTF 高位阈值", "signal.htf_high_zone"),
        ("HTF 偏离 ATR", "signal.htf_ext_atr"))),
)

TWO_STAGE_FIELDS = (
    ("策略参数：两段式（长周期预备 → 短周期放量）", (
        ("预备周期 (分钟)", "setup.htf_minutes"),
        ("区间回看根数", "setup.lookback"),
        ("预备高位阈值", "setup.high_zone"),
        ("预备偏离 ATR", "setup.ext_atr"),
        ("预备有效根数", "setup.arm_bars"),
        ("触发放量倍数", "setup.trig_vol_mult"),
        ("允许跌离 %", "setup.trig_max_drop_pct"))),
)


def register(spec: StrategySpec) -> None:
    if spec.id in REGISTRY:
        raise ValueError(f"策略 {spec.id} 已注册")
    REGISTRY[spec.id] = spec


def get(sid: str) -> StrategySpec | None:
    return REGISTRY.get(sid)


def all() -> list[StrategySpec]:
    return list(REGISTRY.values())


def ids() -> list[str]:
    return list(REGISTRY)


def infer(cfg: dict) -> str:
    """老配置没有 strategy 字段时，从开关推断。"""
    sid = str(cfg.get("strategy") or "").strip()
    if sid in REGISTRY:
        return sid
    if (cfg.get("fade") or {}).get("enabled"):
        return "fade"
    if (cfg.get("setup") or {}).get("enabled"):
        return "two_stage"
    return "pinbar"


register(StrategySpec(
    id="fade", name="复刻带单员：暴涨做空", tab="复刻做空",
    desc=("1h 急涨 + 15m 仍在冲 → 做空；逆行加仓、回均价止盈，"
          "交易所条件单兜底，止损后不追、垂直拉升当天拉黑。"),
    kind="fade", fields=FADE_FIELDS,
    presets=("fade", "fade_live", "aggr_100", "aggr_500")))

register(StrategySpec(
    id="pinbar", name="单根形态：放量急拉 + 上影反转", tab="单根形态",
    desc=("放量急拉之后，1m 收盘出现「阴线 + 长上影」的 pinbar 就做空；"
          "适合小利快跑，手续费敏感。"),
    kind="pinbar", fields=PINBAR_FIELDS, presets=("swing",)))

register(StrategySpec(
    id="two_stage", name="两段式：长周期预备 → 短周期放量", tab="两段式",
    desc=("先在 30m 上找高位/偏离均线的「预备」，再等 1m 放量触发，"
          "过滤掉假突破。"),
    kind="two_stage", fields=TWO_STAGE_FIELDS, presets=("scalp",)))
