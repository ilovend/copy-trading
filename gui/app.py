#!/usr/bin/env python3
"""币安跟单监听 · 桌面控制台

三个标签页：
    仓位监控  —— 选择带单员、启停监听、看实时日志（已可用）
    仓位复制  —— 执行账户、比例、风控（预留，等交易 API 接入）
    仓位推送  —— Telegram 配置（已可用）；QQ / 微信 / Webhook（预留）

启动：python gui/app.py  或双击 gui/启动.bat
"""
from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import threading
import tkinter as tk
import urllib.parse
import urllib.request
from pathlib import Path
from tkinter import messagebox, ttk

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bot"))

import listen_copy as L  # noqa: E402

STATE_PATH = ROOT / "gui" / "state.json"
LOG_PATH = ROOT / "gui" / "gui.log"
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
PY = sys.executable


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.jobs: dict[str, subprocess.Popen] = {}
        self.q: queue.Queue = queue.Queue()
        self.rows: list[dict] = []
        self.monitored: set[str] = set()
        self.copied: set[str] = set()
        self._build()
        self._load_state()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(120, self._drain)
        root.after(600, self._poll)
        self.refresh()

    # ------------------------------------------------------------------ UI
    def _build(self) -> None:
        self.root.title("币安跟单监听 · 控制台")
        self.root.geometry("1180x800")
        self.root.minsize(980, 660)

        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Treeview", rowheight=27)
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("TButton", padding=(10, 6))
        style.configure("TNotebook.Tab", padding=(18, 8))

        head = ttk.Frame(self.root, padding=(14, 12, 14, 4))
        head.pack(fill="x")
        ttk.Label(head, text="币安跟单控制台",
                  font=("Microsoft YaHei UI", 15, "bold")).pack(side="left")
        ttk.Label(head, text="    监控 / 复制 / 推送", foreground="#777").pack(side="left")

        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill="both", expand=True, padx=12, pady=(4, 12))

        self.tab_monitor = ttk.Frame(self.nb, padding=(10, 10, 10, 8))
        self.tab_copy = ttk.Frame(self.nb, padding=(14, 14, 14, 10))
        self.tab_push = ttk.Frame(self.nb, padding=(14, 14, 14, 10))
        self.tab_join = ttk.Frame(self.nb, padding=(10, 10, 10, 8))
        self.nb.add(self.tab_monitor, text="仓位监控")
        self.nb.add(self.tab_copy, text="仓位复制")
        self.nb.add(self.tab_push, text="仓位推送")
        self.nb.add(self.tab_join, text="自动跟单")

        self._build_monitor(self.tab_monitor)
        self._build_copy(self.tab_copy)
        self._build_push(self.tab_push)
        self._build_autojoin(self.tab_join)
        self._refresh_env_labels()

    # ------------------------------------------------------------- 监控 tab
    def _build_monitor(self, parent: ttk.Frame) -> None:
        bar = ttk.Frame(parent)
        bar.pack(fill="x")
        ttk.Button(bar, text="刷新列表", command=self.refresh).pack(side="left")
        ttk.Button(bar, text="启动", command=self.start_session).pack(side="left", padx=6)
        ttk.Button(bar, text="停止", command=self.stop_session).pack(side="left")
        self.var_hint = tk.StringVar(value="点列表里的「监控 / 复制」切换")
        ttk.Label(bar, textvariable=self.var_hint, foreground="#777").pack(side="left", padx=12)
        ttk.Button(bar, text="更新登录态", command=self.update_session).pack(side="right")
        ttk.Button(bar, text="打开数据目录", command=self.open_data).pack(side="right", padx=6)

        paned = ttk.Panedwindow(parent, orient="horizontal")
        paned.pack(fill="both", expand=True, pady=(10, 8))

        left = ttk.Frame(paned)
        paned.add(left, weight=3)
        cols = ("name", "lead", "copy", "mb", "mon", "cp")
        self.tree = ttk.Treeview(left, columns=cols, show="headings", selectmode="extended")
        for c, t, w in (("name", "带单员", 140), ("lead", "带单员ID", 175),
                        ("copy", "我的组合ID", 175), ("mb", "保证金余额", 100),
                        ("mon", "监控", 60), ("cp", "复制", 60)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w" if c == "name" else "center")
        vsb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.tree.bind("<Button-1>", self._on_tree_click)

        right = ttk.Frame(paned, padding=(14, 0, 0, 0))
        paned.add(right, weight=2)

        box = ttk.LabelFrame(right, text="运行状态", padding=10)
        box.pack(fill="x")
        self.var_status = tk.StringVar(value="未运行")
        self.var_shadow = tk.StringVar(value="未运行")
        self.var_join = tk.StringVar(value="未运行")
        self.var_tg = tk.StringVar()
        self.var_proxy = tk.StringVar()
        self.var_session = tk.StringVar()
        for label, var in (("监控", self.var_status), ("影子复制", self.var_shadow),
                           ("自动跟单", self.var_join), ("推送", self.var_tg),
                           ("代理", self.var_proxy), ("登录态", self.var_session)):
            row = ttk.Frame(box)
            row.pack(fill="x", pady=2)
            ttk.Label(row, text=label, width=9, foreground="#666").pack(side="left")
            ttk.Label(row, textvariable=var).pack(side="left")

        tips = ttk.LabelFrame(right, text="说明", padding=10)
        tips.pack(fill="both", expand=True, pady=(10, 0))
        ttk.Label(tips, justify="left", foreground="#555", wraplength=360, text=(
            "· 这里监听的是「你自己的跟单组合」的实时成交。\n"
            "· WebSocket 走 /private/stream，实测延迟约 100ms。\n"
            "· Ctrl / Shift 可多选带单员。\n"
            "· 登录态 2–3 天过期一次，点「更新登录态」即可。\n"
            "· 推送渠道在「仓位推送」页配置。"
        )).pack(anchor="w")

        logframe = ttk.LabelFrame(parent, text="实时日志", padding=(8, 6))
        logframe.pack(fill="both", expand=True)
        self.log = tk.Text(logframe, height=9, wrap="none", bg="#111418", fg="#c9d1d9",
                           insertbackground="#c9d1d9", font=("Consolas", 10), relief="flat")
        lsb = ttk.Scrollbar(logframe, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=lsb.set)
        self.log.pack(side="left", fill="both", expand=True)
        lsb.pack(side="right", fill="y")
        for tag, color in (("info", "#9aa4b2"), ("ok", "#4ade80"), ("warn", "#fbbf24"),
                           ("err", "#f87171"), ("fill", "#38bdf8"), ("close", "#fb7185")):
            self.log.tag_config(tag, foreground=color)

    # ------------------------------------------------------------- 复制 tab
    def _build_copy(self, parent: ttk.Frame) -> None:
        cfg = self._load_copy_cfg()
        self.copy_vars: dict[str, tk.StringVar] = {}

        ttk.Label(parent, text="仓位复制（影子模式）",
                  font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w")
        ttk.Label(parent, foreground="#777", justify="left", wraplength=760, text=(
            "当前是影子模式：完整计算「本应下的单」并测量滑点，但不会真的下单。"
            "改完参数点「保存配置」，再点「启动影子复制」。"
        )).pack(anchor="w", pady=(2, 10))

        mode_box = ttk.LabelFrame(parent, text="运行模式", padding=10)
        mode_box.pack(fill="x", pady=(0, 10))
        self._mode_labels = {
            "影子：只计算、只记录，不碰交易所": "shadow",
            "干跑：打印订单参数，但不发送": "live_dry",
            "实盘：真实下单（请先小资金验证）": "live",
        }
        self.var_exec_mode = tk.StringVar()
        cur_mode = cfg.get("mode", "shadow")
        self.cb_exec_mode = ttk.Combobox(mode_box, values=list(self._mode_labels),
                                         state="readonly", width=42)
        self.cb_exec_mode.set(next((k for k, v in self._mode_labels.items() if v == cur_mode),
                                   list(self._mode_labels)[0]))
        self.cb_exec_mode.grid(row=0, column=0, sticky="w")
        self.cb_exec_mode.bind("<<ComboboxSelected>>", self._on_exec_mode_change)
        self.var_exec_warn = tk.StringVar(value="")
        ttk.Label(mode_box, textvariable=self.var_exec_warn, foreground="#c0392b",
                  justify="left").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self._refresh_exec_warn()

        acct = ttk.LabelFrame(parent, text="执行账户", padding=12)
        acct.pack(fill="x")
        self._cfg_entry(acct, 0, "API Key", "exec_api_key", cfg, "接入实盘时填（当前不用）", show="*")
        self._cfg_entry(acct, 1, "API Secret", "exec_api_secret", cfg, "接入实盘时填（当前不用）", show="*")
        self._cfg_entry(acct, 2, "执行账户权益 (U)", "exec_equity", cfg, "影子模式下手填，比如 50")
        self._cfg_entry(acct, 3, "杠杆", "leverage", cfg, "固定保证金模式用它算名义")
        self._cfg_entry(acct, 4, "单笔上限 (U)", "max_order_notional", cfg, "单笔名义不超过这个")
        self._cfg_entry(acct, 5, "总敞口上限 (U)", "max_total_notional", cfg, "所有持仓名义合计")

        ratio = ttk.LabelFrame(parent, text="比例与风控", padding=12)
        ratio.pack(fill="x", pady=(12, 0))
        ttk.Label(ratio, text="比例模式", width=18, foreground="#666").grid(row=0, column=0, sticky="w", pady=3)
        self.var_ratio_mode = tk.StringVar()
        self._ratio_labels = {
            "净值比（执行权益 / 镜像权益）": "net_equity",
            "固定倍数（镜像数量 × k）": "fixed_multiple",
            "固定保证金（每笔固定保证金 × 杠杆）": "fixed_margin",
        }
        cb = ttk.Combobox(ratio, values=list(self._ratio_labels), state="readonly", width=34)
        cur = cfg.get("ratio_mode", "net_equity")
        cb.set(next((k for k, v in self._ratio_labels.items() if v == cur),
                    "净值比（执行权益 / 镜像权益）"))
        cb.grid(row=0, column=1, sticky="w", pady=3)
        self.cb_ratio = cb
        cb.bind("<<ComboboxSelected>>", lambda _e: self._apply_ratio_mode())

        self._cfg_entry(ratio, 1, "系数 k", "k", cfg, "净值比/固定倍数用")
        self._cfg_entry(ratio, 2, "每笔保证金 (U)", "margin_per_trade", cfg, "固定保证金模式用")
        self._cfg_entry(ratio, 3, "单币仓位上限 %", "max_symbol_pct", cfg, "占执行权益的比例")
        self._cfg_entry(ratio, 4, "滑点上限 BTC/ETH %", "slippage_btc_eth_pct", cfg, "官方默认 0.5")
        self._cfg_entry(ratio, 5, "滑点上限 其他币 %", "slippage_default_pct", cfg, "官方默认 1.5")
        self._cfg_entry(ratio, 6, "保护止损 %", "protective_stop_pct", cfg, "实盘时挂交易所止损，0=关闭")

        self.var_bump = tk.BooleanVar(value=bool(cfg.get("min_notional_bump", True)))
        ttk.Checkbutton(ratio, text="按比例算出来不足 5U 时，按 5U 最小名义下单",
                        variable=self.var_bump).grid(row=7, column=0, columnspan=3,
                                                     sticky="w", pady=(8, 0))

        btns = ttk.Frame(parent)
        btns.pack(fill="x", pady=(14, 0))
        ttk.Button(btns, text="保存配置", command=self.save_copy_cfg).pack(side="left")
        ttk.Button(btns, text="测试连接", command=self.test_exec).pack(side="left", padx=8)
        ttk.Button(btns, text="查看今日日报", command=self.show_shadow_report).pack(side="left")

        self.var_mode_hint = tk.StringVar(value="")
        ttk.Label(parent, textvariable=self.var_mode_hint,
                  foreground="#666", justify="left").pack(anchor="w", pady=(10, 0))

        ttk.Label(parent, foreground="#999", justify="left", wraplength=760, text=(
            "影子模式会把每笔意图写进 bot/data/intents-YYYYMMDD.jsonl，"
            "内容包括目标数量、盘口估算成交价、滑点 bps。跑一天再用「查看今日日报」评估。"
        )).pack(anchor="w", pady=(10, 0))

        self._apply_ratio_mode()

    def _apply_ratio_mode(self) -> None:
        mode = self._ratio_labels.get(self.cb_ratio.get(), "net_equity")
        use_k = mode in ("net_equity", "fixed_multiple")
        use_margin = mode == "fixed_margin"

        def enable(key: str, on: bool) -> None:
            w = getattr(self, "copy_entries", {}).get(key)
            if w is not None:
                w.configure(state="normal" if on else "disabled")

        enable("k", use_k)
        enable("margin_per_trade", use_margin)
        enable("leverage", use_margin)

        if mode == "net_equity":
            self.var_mode_hint.set(
                "净值比：手数 = 镜像数量 × (执行权益 ÷ 该组合保证金余额) × k。"
                "只用到「系数 k」，「每笔保证金」和「杠杆」不参与计算。"
                "比例算出来不足 5U 时，会按 5U 最小名义下单（可关）。")
        elif mode == "fixed_multiple":
            self.var_mode_hint.set(
                "固定倍数：手数 = 镜像数量 × k。只用到「系数 k」，跟两边资金多少无关。")
        else:
            self.var_mode_hint.set(
                "固定保证金：名义 = 每笔保证金 × 杠杆，手数 = 名义 ÷ 价格。"
                "用不到「系数 k」。")

    def _cfg_entry(self, parent, row: int, label: str, key: str, cfg: dict,
                   hint: str = "", show: str = "") -> None:
        ttk.Label(parent, text=label, width=18, foreground="#666").grid(
            row=row, column=0, sticky="w", pady=3)
        var = tk.StringVar(value=str(cfg.get(key, "")))
        e = ttk.Entry(parent, textvariable=var, width=30, show=show)
        e.grid(row=row, column=1, sticky="w", pady=3)
        if not hasattr(self, "copy_entries"):
            self.copy_entries = {}
        self.copy_entries[key] = e
        if show:
            ttk.Label(parent, text=hint, foreground="#bbb").grid(row=row, column=2, sticky="w", padx=(10, 0))
        elif hint:
            ttk.Label(parent, text=hint, foreground="#aaa").grid(row=row, column=2, sticky="w", padx=(10, 0))
        self.copy_vars[key] = var

    @staticmethod
    def _load_copy_cfg() -> dict:
        defaults = {
            "exec_equity": 50.0, "leverage": 20, "max_order_notional": 10.0,
            "max_total_notional": 50.0, "ratio_mode": "net_equity", "k": 1.0,
            "margin_per_trade": 5.0, "max_symbol_pct": 20.0,
            "max_slippage_pct": 0.5, "slippage_btc_eth_pct": 0.5,
            "slippage_default_pct": 1.5, "protective_stop_pct": 2.0,
            "exec_api_key": "", "exec_api_secret": "",
        }
        p = ROOT / "gui" / "copy_config.json"
        if p.exists():
            try:
                defaults.update(json.loads(p.read_text(encoding="utf-8")))
            except Exception:  # noqa: BLE001
                pass
        return defaults

    def save_copy_cfg(self) -> None:
        cfg = self._load_copy_cfg()
        for key, var in self.copy_vars.items():
            raw = var.get().strip()
            if key in ("exec_api_key", "exec_api_secret"):
                cfg[key] = raw
                continue
            try:
                cfg[key] = float(raw) if "." in raw else int(raw)
            except ValueError:
                messagebox.showwarning("格式错误", f"{key} 需要一个数字，当前是 {raw!r}")
                return
        cfg["ratio_mode"] = self._ratio_labels.get(self.cb_ratio.get(), "net_equity")
        cfg["min_notional_bump"] = bool(self.var_bump.get())
        cfg["mode"] = self._mode_labels.get(self.cb_exec_mode.get(), "shadow")
        path = ROOT / "gui" / "copy_config.json"
        path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        self._append(f"复制配置已保存：{cfg['ratio_mode']} k={cfg['k']} "
                     f"权益={cfg['exec_equity']}U 单笔≤{cfg['max_order_notional']}U", "ok")

    # ------------------------------------------------------------ session
    def _on_tree_click(self, event) -> None:
        col = self.tree.identify_column(event.x)
        row = self.tree.identify_row(event.y)
        if not row or col not in ("#5", "#6"):
            return
        if col == "#5":
            if row in self.monitored:
                self.monitored.discard(row)
                self.copied.discard(row)
            else:
                self.monitored.add(row)
        else:
            if row in self.copied:
                self.copied.discard(row)
            else:
                self.copied.add(row)
                self.monitored.add(row)
        self._refresh_row(row)
        self._update_hint()

    def _refresh_row(self, pid: str) -> None:
        if not self.tree.exists(pid):
            return
        vals = list(self.tree.item(pid, "values"))
        vals[4] = "✓" if pid in self.monitored else "○"
        vals[5] = "✓" if pid in self.copied else "○"
        self.tree.item(pid, values=vals)

    def _update_hint(self) -> None:
        total = sum(1 for pf in self.rows if pf["copyPortfolioId"] in self.monitored)
        copied = sum(1 for pf in self.rows if pf["copyPortfolioId"] in self.copied)
        if total == 0:
            self.var_hint.set("未勾选监控")
        elif copied:
            self.var_hint.set(f"将启动：监控 + 影子复制（复制 {copied}/{total}）")
        else:
            self.var_hint.set(f"将启动：仅监控（{total} 个）")

    def start_session(self) -> None:
        ids = [pf["copyPortfolioId"] for pf in self.rows if pf["copyPortfolioId"] in self.monitored]
        if not ids:
            messagebox.showinfo("提示", "先点列表里的「监控」列，勾选要盯的带单员")
            return
        copied = [i for i in ids if i in self.copied]
        self._save_copy_targets(copied)
        if copied:
            self.start_job("shadow", ids=ids, script="shadow_copy.py")
        else:
            self.start_job("monitor", ids=ids)

    def stop_session(self) -> None:
        self.stop_job("shadow")
        self.stop_job("monitor")

    def _save_copy_targets(self, copied_ids: list[str]) -> None:
        cfg = self._load_copy_cfg()
        cfg["monitored"] = sorted(self.monitored)
        cfg["targets"] = {pid: {"enabled": pid in copied_ids} for pid in self.monitored}
        (ROOT / "gui" / "copy_config.json").write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    def _on_exec_mode_change(self, _event=None) -> None:
        target = self._mode_labels.get(self.cb_exec_mode.get(), "shadow")
        if target == "live":
            if not messagebox.askokcancel(
                    "确认实盘",
                    "即将开启真实下单。\n\n"
                    "第一周建议：单笔 ≤5U、日亏 ≤5U、只跟 1 个带单员。\n"
                    "确认要切到实盘模式吗？"):
                self.cb_exec_mode.set(list(self._mode_labels)[0])
        self._refresh_exec_warn()

    def _refresh_exec_warn(self) -> None:
        target = self._mode_labels.get(self.cb_exec_mode.get(), "shadow")
        if target == "shadow":
            self.var_exec_warn.set("")
        elif target == "live_dry":
            self.var_exec_warn.set("干跑模式：日志里会打印完整订单参数，但不会发送。")
        else:
            self.var_exec_warn.set("⚠ 实盘模式：会真实下单扣款，先用 5U 验证。")

    def test_exec(self) -> None:
        key = self.copy_vars["exec_api_key"].get().strip()
        secret = self.copy_vars["exec_api_secret"].get().strip()
        if not key or not secret:
            messagebox.showwarning("提示", "先填 API Key 和 API Secret")
            return
        self.save_copy_cfg()
        self._append("[复制] 正在测试执行账户连接…", "info")

        def work() -> None:
            try:
                from exec_client import ExecClient
                info = ExecClient(key, secret).test()
                self.q.put(("log", f"[复制] 连接成功：权益 {info['equity']:.2f}U  "
                                   f"可用 {info['available']:.2f}U  持仓 {info['positions']} 个"))
                if info["symbols"]:
                    self.q.put(("log", f"[复制] 现有持仓：{', '.join(info['symbols'])}"))
            except Exception as e:  # noqa: BLE001
                self.q.put(("log", f"[复制] 连接失败：{e}"))

        threading.Thread(target=work, daemon=True).start()

    def show_shadow_report(self) -> None:
        def work() -> None:
            p = subprocess.run([PY, str(ROOT / "bot" / "shadow_report.py")],
                               cwd=str(ROOT), capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=30)
            self.q.put(("log", "── 影子日报 ──"))
            for line in (p.stdout or "").splitlines():
                self.q.put(("log", line))

        threading.Thread(target=work, daemon=True).start()


    # ------------------------------------------------------------- 推送 tab
    def _build_push(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="推送渠道", font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w")

        tg = ttk.LabelFrame(parent, text="Telegram（已接入）", padding=12)
        tg.pack(fill="x", pady=(8, 0))
        self.var_tg_token = tk.StringVar(value=L.TG_TOKEN)
        self.var_tg_chat = tk.StringVar(value=L.TG_CHAT)
        self.var_tg_proxy = tk.StringVar(value=L.TG_PROXY)
        self.var_tg_show = tk.BooleanVar(value=False)

        ttk.Label(tg, text="Bot Token", width=12, foreground="#666").grid(row=0, column=0, sticky="w", pady=3)
        self.e_token = ttk.Entry(tg, textvariable=self.var_tg_token, width=62, show="*")
        self.e_token.grid(row=0, column=1, sticky="w", pady=3)
        ttk.Checkbutton(tg, text="显示", variable=self.var_tg_show,
                        command=lambda: self.e_token.configure(
                            show="" if self.var_tg_show.get() else "*")).grid(
            row=0, column=2, sticky="w", padx=(8, 0))

        ttk.Label(tg, text="Chat ID", width=12, foreground="#666").grid(row=1, column=0, sticky="w", pady=3)
        ttk.Entry(tg, textvariable=self.var_tg_chat, width=62).grid(row=1, column=1, sticky="w", pady=3)

        ttk.Label(tg, text="代理", width=12, foreground="#666").grid(row=2, column=0, sticky="w", pady=3)
        ttk.Entry(tg, textvariable=self.var_tg_proxy, width=62).grid(row=2, column=1, sticky="w", pady=3)
        ttk.Label(tg, text="例 http://127.0.0.1:7890，留空直连",
                  foreground="#999").grid(row=3, column=1, sticky="w")

        self.var_tg_result = tk.StringVar(value="")
        ttk.Label(tg, textvariable=self.var_tg_result,
                  foreground="#0a7").grid(row=4, column=1, sticky="w", pady=(6, 0))

        btns = ttk.Frame(tg)
        btns.grid(row=5, column=1, sticky="w", pady=(10, 0))
        ttk.Button(btns, text="测试发送", command=self.test_telegram).pack(side="left", padx=(0, 8))
        ttk.Button(btns, text="保存", command=self.save_telegram).pack(side="left")

        ttk.Label(tg, foreground="#777", justify="left", wraplength=700, text=(
            "Token：Telegram 搜 @BotFather → /newbot → 得到 123456789:AAH...\n"
            "Chat ID：Telegram 搜 @userinfobot → 发任意消息，它回复的数字\n"
            "建完机器人先给它发一句话，否则不能主动私聊你。"
        )).grid(row=6, column=0, columnspan=3, sticky="w", pady=(10, 0))

        other = ttk.LabelFrame(parent, text="其他渠道（预留）", padding=12)
        other.pack(fill="x", pady=(12, 0))
        for i, (label, hint) in enumerate((
                ("QQ 机器人", "go-cqhttp / OneBot"),
                ("企业微信 / 微信群", "Webhook"),
                ("钉钉", "Webhook"),
                ("通用 Webhook", "HTTP POST JSON"))):
            var = tk.BooleanVar(value=False)
            ttk.Checkbutton(other, text=label, variable=var, state="disabled").grid(
                row=i, column=0, sticky="w", pady=3)
            ttk.Entry(other, width=40, state="disabled").grid(row=i, column=1, sticky="w", padx=(12, 8))
            ttk.Label(other, text=hint, foreground="#999").grid(row=i, column=2, sticky="w")

        ttk.Label(parent, foreground="#999", justify="left", wraplength=720, text=(
            "以后加渠道时，接口已经按「推送适配器」设计：监听器只产生事件，"
            "由这里决定发到哪些渠道，互不影响。"
        )).pack(anchor="w", pady=(12, 0))

    # ------------------------------------------------------------ 自动跟单 tab
    def _build_autojoin(self, parent: ttk.Frame) -> None:
        cfg = self._load_aj_cfg()

        bar = ttk.Frame(parent)
        bar.pack(fill="x")
        ttk.Button(bar, text="刷新收藏", command=self.refresh_favorites).pack(side="left")
        ttk.Button(bar, text="启动监控", command=self.start_autojoin).pack(side="left", padx=6)
        ttk.Button(bar, text="停止", command=lambda: self.stop_job("autojoin")).pack(side="left")
        self.var_aj_live = tk.BooleanVar(value=bool(cfg.get("live")))
        ttk.Checkbutton(bar, text="实际下单（关闭 = 只提示）",
                        variable=self.var_aj_live).pack(side="left", padx=14)

        body = ttk.Panedwindow(parent, orient="horizontal")
        body.pack(fill="both", expand=True, pady=(10, 0))

        left = ttk.Frame(body)
        body.add(left, weight=3)
        cols = ("name", "lead", "slots")
        self.fav_tree = ttk.Treeview(left, columns=cols, show="headings", selectmode="extended")
        for c, t, w in (("name", "带单员", 220), ("lead", "带单员ID", 210), ("slots", "名额", 110)):
            self.fav_tree.heading(c, text=t)
            self.fav_tree.column(c, width=w, anchor="w" if c == "name" else "center")
        vsb = ttk.Scrollbar(left, orient="vertical", command=self.fav_tree.yview)
        self.fav_tree.configure(yscrollcommand=vsb.set)
        self.fav_tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        ttk.Label(left, text="选中要监控的人（可多选），名额出现时会自动加入",
                  foreground="#999").pack(anchor="w", pady=(6, 0))

        right = ttk.Frame(body, padding=(14, 0, 0, 0))
        body.add(right, weight=2)
        form = ttk.LabelFrame(right, text="跟单参数", padding=10)
        form.pack(fill="x")
        self.aj_vars: dict[str, tk.StringVar] = {}

        self.aj_entries: dict[str, ttk.Entry] = {}

        def row(i: int, label: str, key: str, default: str) -> None:
            ttk.Label(form, text=label, width=16, foreground="#666").grid(row=i, column=0, sticky="w", pady=3)
            var = tk.StringVar(value=str(cfg.get("defaults", {}).get(key, default) or ""))
            e = ttk.Entry(form, textvariable=var, width=22)
            e.grid(row=i, column=1, sticky="w", pady=3)
            self.aj_vars[key] = var
            self.aj_entries[key] = e

        row(0, "投资金额 (U)", "investAmount", "50")
        ttk.Label(form, text="跟单模式", width=16, foreground="#666").grid(row=1, column=0, sticky="w", pady=3)
        self.cb_copy_model = ttk.Combobox(form, values=["固定比例", "固定金额"],
                                          state="readonly", width=19)
        self.cb_copy_model.current(0 if cfg.get("defaults", {}).get("copyModel") == "FIXED_RATIO" else 1)
        self.cb_copy_model.grid(row=1, column=1, sticky="w", pady=3)
        row(2, "每笔金额 (U)", "costPerOrder", "0")
        row(3, "总止损 %", "totalStopLossRate", "")
        row(4, "止盈 %", "takeProfitRate", "")
        row(5, "止损 %", "stopLostRate", "")

        ttk.Label(form, text="保证金模式", width=16, foreground="#666").grid(row=6, column=0, sticky="w", pady=3)
        self.cb_margin = ttk.Combobox(form, values=["跟随带单员", "全仓", "逐仓"], state="readonly", width=19)
        self.cb_margin.current(0)
        self.cb_margin.grid(row=6, column=1, sticky="w", pady=3)

        ttk.Label(form, text="杠杆模式", width=16, foreground="#666").grid(row=7, column=0, sticky="w", pady=3)
        self.cb_leverage = ttk.Combobox(form, values=["跟随带单员", "固定杠杆"], state="readonly", width=19)
        self.cb_leverage.current(0)
        self.cb_leverage.grid(row=7, column=1, sticky="w", pady=3)
        row(8, "杠杆倍数", "leverageValue", "")

        ttk.Label(form, text="检查间隔 (秒)", width=16, foreground="#666").grid(row=9, column=0, sticky="w", pady=3)
        self.var_poll = tk.StringVar(value=str(cfg.get("poll_interval", 1.0)))
        ttk.Entry(form, textvariable=self.var_poll, width=22).grid(row=9, column=1, sticky="w", pady=3)
        ttk.Label(form, text="0.5–5，越小越容易抢到名额", foreground="#aaa").grid(
            row=9, column=2, sticky="w", padx=(8, 0))

        self.cb_copy_model.bind("<<ComboboxSelected>>", lambda _e: self._apply_aj_mode())
        self.cb_leverage.bind("<<ComboboxSelected>>", lambda _e: self._apply_aj_mode())
        self._apply_aj_mode()

        ttk.Button(right, text="保存配置", command=self.save_aj).pack(anchor="w", pady=(10, 0))
        ttk.Label(right, foreground="#999", justify="left", wraplength=330, text=(
            "· 只对「收藏」里的带单员生效，先点刷新收藏\n"
            "· 名额满了会一直等，不消耗资金\n"
            "· 关闭「实际下单」时只推 Telegram，不真的加入"
        )).pack(anchor="w", pady=(10, 0))

    def _apply_aj_mode(self) -> None:
        fixed_amt = self.cb_copy_model.current() == 1
        fix_lev = self.cb_leverage.current() == 1
        w = self.aj_entries.get("costPerOrder")
        if w is not None:
            w.configure(state="normal" if fixed_amt else "disabled")
        w = self.aj_entries.get("leverageValue")
        if w is not None:
            w.configure(state="normal" if fix_lev else "disabled")

    def refresh_favorites(self) -> None:
        self._append("读取收藏的带单员…", "info")

        def work() -> None:
            try:
                L.load_session()
                body = {"pageNumber": 1, "pageSize": 50, "timeRange": "90D",
                        "dataType": "ROI", "favoriteOnly": True, "hideFull": False,
                        "nickname": "", "order": "DESC", "apiKeyOnly": False}
                j = L.api("/bapi/futures/v1/friendly/future/copy-trade/home-page/query-list",
                          "POST", body)
                self.q.put(("favs", (j.get("data") or {}).get("list") or []))
            except Exception as e:  # noqa: BLE001
                self.q.put(("error", f"收藏读取失败：{e}"))

        threading.Thread(target=work, daemon=True).start()

    def _fill_favs(self, rows: list[dict]) -> None:
        self.fav_tree.delete(*self.fav_tree.get_children())
        cfg = self._load_aj_cfg()
        targets = cfg.get("targets") or {}
        for r in rows:
            lid = r.get("leadPortfolioId")
            self.fav_tree.insert("", "end", iid=lid, values=(
                r.get("nickname"), lid, f"{r.get('currentCopyCount')}/{r.get('maxCopyCount')}"))
            if lid in targets:
                self.fav_tree.selection_add(lid)
        self._append(f"收藏 {len(rows)} 人", "ok")

    @staticmethod
    def _load_aj_cfg() -> dict:
        p = ROOT / "gui" / "autojoin_config.json"
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                pass
        return {"poll_interval": 3, "live": False, "defaults": {}, "targets": {}}

    @staticmethod
    def _num(raw: str, default=None):
        raw = (raw or "").strip()
        if not raw:
            return default
        try:
            return float(raw) if "." in raw else int(raw)
        except ValueError:
            return default

    def save_aj(self) -> None:
        ids = list(self.fav_tree.selection())
        if not ids:
            messagebox.showwarning("提示", "先在左侧选中要监控的带单员")
            return
        cfg = self._load_aj_cfg()
        cfg["live"] = bool(self.var_aj_live.get())
        cfg["poll_interval"] = self._num(self.var_poll.get(), 1.0) or 1.0
        defaults = {
            "investAmount": self._num(self.aj_vars["investAmount"].get(), 50),
            "investAsset": "USDT",
            "copyModel": "FIXED_RATIO" if self.cb_copy_model.current() == 0 else "FIXED_AMT",
            "costPerOrder": self._num(self.aj_vars["costPerOrder"].get(), 0),
            "marginMode": ["FOLLOW_LEAD", "CROSS", "ISOLATED"][self.cb_margin.current()],
            "leverageMode": ["FOLLOW_LEAD", "FIX_LEVERAGE"][self.cb_leverage.current()],
            "leverageValue": self._num(self.aj_vars["leverageValue"].get()),
            "totalStopLossRate": self._num(self.aj_vars["totalStopLossRate"].get()),
            "takeProfitRate": self._num(self.aj_vars["takeProfitRate"].get()),
            "stopLostRate": self._num(self.aj_vars["stopLostRate"].get()),
            "maxPositionPerSymbolRate": None,
        }
        cfg["defaults"] = defaults
        cfg["targets"] = {
            iid: {"nickname": self.fav_tree.item(iid, "values")[0],
                  "investAmount": defaults["investAmount"]}
            for iid in ids
        }
        (ROOT / "gui" / "autojoin_config.json").write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        self._append(f"自动跟单配置已保存：{len(ids)} 人，"
                     f"{'实盘' if cfg['live'] else '只提示'}，"
                     f"每人 {defaults['investAmount']} U", "ok")

    def start_autojoin(self) -> None:
        ids = list(self.fav_tree.selection())
        if not ids:
            messagebox.showwarning("提示", "先在左侧选中要监控的带单员")
            return
        if self.var_aj_live.get():
            if not messagebox.askokcancel(
                    "确认实盘",
                    f"将对选中的 {len(ids)} 个带单员执行真实跟单，"
                    f"每个投入 {self.aj_vars['investAmount'].get()} U。\n\n确定继续？"):
                return
        self.save_aj()
        self.start_job("autojoin", pass_only=False, script="auto_join.py")

    # --------------------------------------------------------------- state
    def _refresh_env_labels(self) -> None:
        has = bool(L.TG_TOKEN and L.TG_CHAT)
        self.var_tg.set("Telegram 已配置" if has else "未配置")
        self.var_proxy.set(L.TG_PROXY or "直连")
        sp = ROOT / "session.json"
        if not sp.exists():
            sp = ROOT / "probe" / "session.json"
        if not sp.exists():
            self.var_session.set("缺失，请点「更新登录态」")
        else:
            try:
                sess = json.loads(sp.read_text(encoding="utf-8-sig"))
                ok = bool(sess.get("cookie")) and bool(sess.get("csrftoken"))
                self.var_session.set("已加载" if ok else "字段不完整")
            except Exception:  # noqa: BLE001
                self.var_session.set("无法解析")

    # --------------------------------------------------------------- data
    def refresh(self) -> None:
        if self._alive("monitor") or self._alive("shadow"):
            messagebox.showinfo("提示", "监听正在运行，先停止再刷新列表")
            return
        self._append("读取跟单列表…", "info")
        threading.Thread(target=self._refresh_worker, daemon=True).start()

    def _refresh_worker(self) -> None:
        try:
            L.load_session()
            rows = L.discover_portfolios()
            self.q.put(("rows", rows))
        except SystemExit as e:
            self.q.put(("error", f"登录态不可用：{e}"))
        except Exception as e:  # noqa: BLE001
            self.q.put(("error", f"{type(e).__name__}: {e}"))

    def _fill_tree(self, rows: list[dict]) -> None:
        self.rows = rows
        if not self.monitored:
            self.monitored = {pf["copyPortfolioId"] for pf in rows}
        self.tree.delete(*self.tree.get_children())

        for pf in rows:
            pid = pf["copyPortfolioId"]
            self.tree.insert("", "end", iid=pid, values=(
                pf["nickname"], pf["leadPortfolioId"] or "-", pid,
                f"{float(pf.get('marginBalance') or 0):,.2f}",
                "✓" if pid in self.monitored else "○",
                "✓" if pid in self.copied else "○",
            ))
        if not rows:
            self._append("没有读到任何跟单组合（可能未跟单，或登录态已失效）", "warn")
        else:
            self._append(f"共 {len(rows)} 个跟单组合", "ok")
        self._update_hint()

    def _selected_ids(self) -> list[str]:
        try:
            return list(self.tree.selection())
        except Exception:  # noqa: BLE001
            return []

    # ------------------------------------------------------------ process
    JOB_LABEL = {"monitor": "监控", "shadow": "影子", "autojoin": "跟单"}

    def _alive(self, name: str) -> bool:
        proc = self.jobs.get(name)
        return bool(proc and proc.poll() is None)

    def start_job(self, name: str, ids: list[str] | None = None,
                  script: str = "listen_copy.py", pass_only: bool = True) -> None:
        if self._alive(name):
            messagebox.showinfo("提示", f"{self.JOB_LABEL[name]}已经在运行了")
            return

        other = {"monitor": "shadow", "shadow": "monitor"}.get(name)
        if other and self._alive(other):
            if not messagebox.askokcancel(
                    "冲突",
                    f"{self.JOB_LABEL[other]}正在运行，两者都要占用 listenKey，\n"
                    f"同时跑会互相踢下线。要先停止它吗？"):
                return
            self.stop_job(other)

        ids = ids or []
        if pass_only and not ids:
            messagebox.showinfo("提示", "至少勾选一个「监控」的带单员")
            return

        cmd = [PY, "-u", str(ROOT / "bot" / script)]
        if pass_only and ids:
            cmd += ["--only", ",".join(ids)]
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        try:
            proc = subprocess.Popen(
                cmd, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                env=env, creationflags=CREATE_NO_WINDOW)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("启动失败", str(e))
            return

        self.jobs[name] = proc
        self._refresh_job_status()
        self._append(f"启动 {self.JOB_LABEL[name]}："
                     f"{'全部组合' if not ids else '选中 ' + str(len(ids)) + ' 个'}", "ok")
        self._save_state()
        threading.Thread(target=self._reader, args=(proc, name), daemon=True).start()

    def _reader(self, proc: subprocess.Popen, name: str) -> None:
        stream = getattr(proc, "stdout", None)
        if stream is None:
            return
        tag = self.JOB_LABEL.get(name, name)
        try:
            for line in stream:
                self.q.put(("log", f"[{tag}] {line.rstrip()}"))
        except Exception:  # noqa: BLE001
            pass

    def stop_job(self, name: str) -> None:
        proc = self.jobs.get(name)
        if not proc or proc.poll() is not None:
            self.jobs.pop(name, None)
            self._refresh_job_status()
            return
        self._append(f"正在停止 {self.JOB_LABEL[name]}…", "warn")
        try:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        except Exception as e:  # noqa: BLE001
            self._append(f"停止失败：{e}", "err")
        self.jobs.pop(name, None)
        self._refresh_job_status()

    def stop_all(self) -> None:
        for name in list(self.jobs):
            self.stop_job(name)

    def _poll(self) -> None:
        for name, proc in list(self.jobs.items()):
            if proc.poll() is not None:
                code = proc.returncode
                self.jobs.pop(name, None)
                self._append(f"{self.JOB_LABEL[name]}已退出，code={code}",
                             "warn" if code else "info")
        self._refresh_job_status()
        self.root.after(800, self._poll)

    def _refresh_job_status(self) -> None:
        self.var_status.set("运行中" if self._alive("monitor") else "未运行")
        self.var_shadow.set("运行中" if self._alive("shadow") else "未运行")
        self.var_join.set("运行中" if self._alive("autojoin") else "未运行")

    # ------------------------------------------------------------- push
    def test_telegram(self) -> None:
        token = self.var_tg_token.get().strip()
        chat = self.var_tg_chat.get().strip()
        if not token or not chat:
            self.var_tg_result.set("先填 Token 和 Chat ID")
            return
        self.var_tg_result.set("正在发送…")
        proxy = self.var_tg_proxy.get().strip()

        def work() -> None:
            ok, msg = self._tg_send_raw(token, chat, proxy)
            self.root.after(0, lambda: self.var_tg_result.set(
                "✅ 已发送，检查手机" if ok else f"❌ 失败：{msg}"))

        threading.Thread(target=work, daemon=True).start()

    @staticmethod
    def _tg_send_raw(token: str, chat: str, proxy: str) -> tuple[bool, str]:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = urllib.parse.urlencode({
            "chat_id": chat, "text": "✅ 测试消息：币安跟单监听配置正常",
            "disable_web_page_preview": "true",
        }).encode()
        handlers = []
        if proxy:
            handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        opener = urllib.request.build_opener(*handlers)
        try:
            with opener.open(url, data=payload, timeout=10) as r:
                return (r.status == 200), ""
        except Exception as e:  # noqa: BLE001
            return False, str(e)[:120]

    def save_telegram(self) -> None:
        token = self.var_tg_token.get().strip()
        chat = self.var_tg_chat.get().strip()
        if not token or not chat:
            messagebox.showwarning("提示", "Token 和 Chat ID 不能为空")
            return
        proxy = self.var_tg_proxy.get().strip()
        env_path = ROOT / ".env"
        updates = {"TELEGRAM_BOT_TOKEN": token, "TELEGRAM_CHAT_ID": chat,
                   "TELEGRAM_PROXY": proxy}
        lines: list[str] = []
        if env_path.exists():
            lines = env_path.read_text(encoding="utf-8").splitlines()
        seen: set[str] = set()
        out: list[str] = []
        for line in lines:
            m = re.match(r"^([A-Z0-9_]+)\s*=", line)
            if m and m.group(1) in updates:
                out.append(f"{m.group(1)}={updates[m.group(1)]}")
                seen.add(m.group(1))
            else:
                out.append(line)
        for k, v in updates.items():
            if k not in seen:
                out.append(f"{k}={v}")
        env_path.write_text("\n".join(out) + "\n", encoding="utf-8")

        os.environ["TELEGRAM_BOT_TOKEN"] = token
        os.environ["TELEGRAM_CHAT_ID"] = chat
        os.environ["TELEGRAM_PROXY"] = proxy
        L.TG_TOKEN, L.TG_CHAT, L.TG_PROXY = token, chat, proxy
        self._refresh_env_labels()
        self._append("推送配置已保存到 .env", "ok")
        self.var_tg_result.set("已保存。正在运行的监听器需重启后生效。")

    # --------------------------------------------------------------- misc
    def update_session(self) -> None:
        if any(self._alive(n) for n in self.jobs):
            messagebox.showinfo("提示", "先停止所有任务再更新登录态")
            return
        if not messagebox.askokcancel(
                "更新登录态",
                "请先把浏览器里的 cURL 复制到剪贴板：\n\n"
                "F12 → Network → 刷新 copy-management 页面\n"
                "→ 任一 user-data 请求 → Copy → Copy as cURL\n\n"
                "已复制好并继续？"):
            return
        self._append("运行 update_session.py …", "info")

        def work() -> None:
            try:
                p = subprocess.run(
                    [PY, "-u", str(ROOT / "bot" / "update_session.py"), "--clipboard"],
                    cwd=str(ROOT), capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=60)
                for line in (p.stdout or "").splitlines():
                    self.q.put(("log", line))
                for line in (p.stderr or "").splitlines():
                    self.q.put(("log", line))
                self.q.put(("session_done", p.returncode))
            except Exception as e:  # noqa: BLE001
                self.q.put(("error", f"更新失败：{e}"))

        threading.Thread(target=work, daemon=True).start()

    def open_data(self) -> None:
        path = ROOT / "bot" / "data"
        path.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            os.startfile(path)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(path)])

    # -------------------------------------------------------------- queue
    def _drain(self) -> None:
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self._append(payload, self._tag_for(payload))
                elif kind == "rows":
                    self._fill_tree(payload)
                    self._refresh_env_labels()
                elif kind == "favs":
                    self._fill_favs(payload)
                elif kind == "error":
                    self._append(payload, "err")
                elif kind == "session_done":
                    if payload == 0:
                        self._append("登录态更新成功", "ok")
                        self._refresh_env_labels()
                        self.refresh()
                    else:
                        self._append("登录态更新失败，看上面的输出", "err")
        except queue.Empty:
            pass
        self.root.after(120, self._drain)

    @staticmethod
    def _tag_for(line: str) -> str:
        low = line.lower()
        if "🟢" in line:
            return "fill"
        if "🔴" in line:
            return "close"
        if "error" in low or "warn" in low or "失败" in line or "中断" in line:
            return "warn"
        if "connected" in low or "keepalive" in low or "watching" in low:
            return "ok"
        return "info"

    def _append(self, line: str, tag: str = "info") -> None:
        self.log.insert("end", line + "\n", tag)
        self.log.see("end")
        try:
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with LOG_PATH.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:  # noqa: BLE001
            pass

    def _load_state(self) -> None:
        self._pending_selection: list[str] = []
        try:
            if STATE_PATH.exists():
                self._pending_selection = json.loads(
                    STATE_PATH.read_text(encoding="utf-8")).get("selected", [])
        except Exception:  # noqa: BLE001
            pass
        try:
            p = ROOT / "gui" / "copy_config.json"
            if p.exists():
                cfg = json.loads(p.read_text(encoding="utf-8"))
                self.monitored = set(cfg.get("monitored") or [])
                self.copied = {pid for pid, t in (cfg.get("targets") or {}).items()
                               if t.get("enabled")}
        except Exception:  # noqa: BLE001
            pass

    def _save_state(self) -> None:
        try:
            STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            STATE_PATH.write_text(json.dumps(
                {"selected": self._selected_ids()}, ensure_ascii=False, indent=1),
                encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    def on_close(self) -> None:
        if any(self._alive(n) for n in self.jobs):
            if not messagebox.askokcancel("退出", "还有任务在运行，退出会全部停止。继续？"):
                return
            self.stop_all()
        self._save_state()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    app = App(root)
    for iid in getattr(app, "_pending_selection", []):
        if app.tree.exists(iid):
            app.tree.selection_add(iid)
    root.mainloop()


if __name__ == "__main__":
    main()
