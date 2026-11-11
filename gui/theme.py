"""界面主题：配色、字体、ttk 样式集中在这里。

底子用 sv-ttk（深浅两套完整主题），本模块只补三件事：
    1. 语义化文本色 —— 其它文件写 style="Muted.TLabel" 而不是硬编码 #666
    2. 中文字体与字号 —— 统一 Microsoft YaHei UI，代码用 Consolas
    3. 深色标题栏 —— 让窗口边框跟着主题走，不出现白边

切换主题时重新 configure 样式，已经创建好的控件会自动跟着变；
只有 tk.Text（日志）和 Treeview 的行底色需要调用方另行刷新。
"""
from __future__ import annotations

import ctypes
import sys
import tkinter.font as tkfont
from tkinter import ttk

try:                                   # 没装也能跑，只是退回 clam 主题
    import sv_ttk
except Exception:                      # noqa: BLE001
    sv_ttk = None

FONT = "Microsoft YaHei UI"
MONO = "Consolas"

# 底色取自 sv-ttk 自带的两套主题，保证和控件背景一致
PALETTES: dict[str, dict[str, str]] = {
    "dark": {
        "bg": "#1c1c1c",
        "fg": "#fafafa",
        "muted": "#b9b9b9",          # 表单标签
        "hint": "#9b9b9b",           # 说明文字
        "dim": "#7a7a7a",            # 更弱的脚注
        "accent": "#57c8ff",
        "ok": "#4ade80",
        "warn": "#fbbf24",
        "err": "#ff7b72",
        "stripe": "#242424",         # 表格隔行
        "log_bg": "#101215",
        "log_fg": "#c9d1d9",
        "log_sel": "#2f60d8",
        "log_info": "#9aa4b2",
        "log_ok": "#4ade80",
        "log_warn": "#fbbf24",
        "log_err": "#ff7b72",
        "log_fill": "#38bdf8",
        "log_close": "#fb7185",
    },
    "light": {
        "bg": "#fafafa",
        "fg": "#1c1c1c",
        "muted": "#4b5563",
        "hint": "#6b7280",
        "dim": "#9aa1ab",
        "accent": "#005fb8",
        "ok": "#15803d",
        "warn": "#b45309",
        "err": "#dc2626",
        "stripe": "#f1f2f4",
        "log_bg": "#f6f7f9",
        "log_fg": "#1f2328",
        "log_sel": "#cfe3ff",
        "log_info": "#57606a",
        "log_ok": "#15803d",
        "log_warn": "#b45309",
        "log_err": "#cf222e",
        "log_fill": "#0969da",
        "log_close": "#bf3989",
    },
}

MODES = ("dark", "light")


def palette(mode: str) -> dict[str, str]:
    return PALETTES.get(mode, PALETTES["dark"])


def apply(root, mode: str = "dark") -> str:
    """套用主题；返回真正生效的模式（未知值回退深色）。"""
    mode = mode if mode in PALETTES else "dark"
    if sv_ttk is not None:
        try:
            sv_ttk.set_theme(mode, root)
        except TypeError:              # 老版本没有 root 参数
            sv_ttk.set_theme(mode)
    else:
        # 没装 sv-ttk 时 clam 只有浅色底子，硬套深色会花，强制浅色
        mode = "light"
        ttk.Style(root).theme_use("clam")

    _fonts(root)
    _styles(root, mode)
    try:
        # 必须在建控件之前设好窗口底色：sv-ttk 的 ttk.Frame 不自己画背景，
        # 继承的是 window 的 bg，晚设就会留一片 #D9D9D9
        root.configure(bg=palette(mode)["bg"])
    except Exception:                  # noqa: BLE001
        pass
    _titlebar(root, mode == "dark")
    return mode


# ------------------------------------------------------------------ internals
def _fonts(root) -> None:
    """改命名字体而不是逐个控件设 font，菜单/提示框也会跟着变。"""
    for name, size in (("TkDefaultFont", 10), ("TkTextFont", 10),
                       ("TkMenuFont", 10), ("TkHeadingFont", 10),
                       ("TkCaptionFont", 9), ("TkSmallCaptionFont", 9),
                       ("TkTooltipFont", 9)):
        try:
            tkfont.nametofont(name, root).configure(family=FONT, size=size)
        except Exception:              # noqa: BLE001
            pass


def _styles(root, mode: str) -> None:
    p = palette(mode)
    st = ttk.Style(root)

    st.configure(".", font=(FONT, 10))
    st.configure("Title.TLabel", font=(FONT, 16, "bold"))
    st.configure("Section.TLabel", font=(FONT, 12, "bold"))
    st.configure("Subtitle.TLabel", font=(FONT, 11, "bold"))

    st.configure("Muted.TLabel", foreground=p["muted"])
    st.configure("Hint.TLabel", foreground=p["hint"])
    st.configure("Dim.TLabel", foreground=p["dim"])
    st.configure("Danger.TLabel", foreground=p["err"])
    st.configure("Ok.TLabel", foreground=p["ok"])
    st.configure("Warn.TLabel", foreground=p["warn"])
    st.configure("Accent.TLabel", foreground=p["accent"])

    st.configure("Treeview", rowheight=30, font=(FONT, 10))
    st.configure("Treeview.Heading", font=(FONT, 10, "bold"))
    st.configure("TNotebook.Tab", font=(FONT, 10), padding=(18, 7))
    st.configure("TButton", padding=(14, 6))
    st.configure("Accent.TButton", font=(FONT, 10, "bold"))


def _titlebar(root, dark: bool) -> None:
    """Windows 10/11 的标题栏跟着深浅色走（不支持的平台静默跳过）。

    注意：这里不能调 update_idletasks()。它会把窗口提前 realize，
    之后创建的 ttk.Frame 会丢掉主题底色（实测整片变成 #D9D9D9）。
    """
    if sys.platform != "win32":
        return
    try:
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        value = ctypes.c_int(1 if dark else 0)
        for attr in (20, 19):          # 20=新系统 19=旧版本
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attr, ctypes.byref(value), ctypes.sizeof(value)) == 0:
                break
    except Exception:                  # noqa: BLE001
        pass
