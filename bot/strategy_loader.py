"""动态插件加载：private/strategies → bot/strategies。

每个插件文件导出 SPEC（dict）和 signal(bars, cfg)；
可选 simulate_basket(...) 用于回放。
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_file(path: Path):
    name = f"strategy_plugin_{path.stem}_{abs(hash(str(path))) % 10**8}"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    meta = getattr(mod, "SPEC", None)
    if not isinstance(meta, dict) or not meta.get("id"):
        return None
    if not callable(getattr(mod, "signal", None)):
        return None
    return meta, mod


def discover():
    out, seen = [], set()
    bases = [ROOT / "bot" / "strategies"]
    if os.environ.get("STRATEGY_DISABLE_PRIVATE") != "1":
        bases.insert(0, ROOT / "private" / "strategies")
    for base in bases:
        if not base.is_dir():
            continue
        for path in sorted(base.glob("*.py")):
            if path.name.startswith("_"):
                continue
            try:
                item = _load_file(path)
            except Exception as e:  # noqa: BLE001
                print(f"[plugin] {path.name} 加载失败: {e}", file=sys.stderr)
                continue
            if not item:
                continue
            meta, mod = item
            sid = str(meta["id"])
            if sid in seen:
                continue
            seen.add(sid)
            out.append((meta, mod))
    return out
