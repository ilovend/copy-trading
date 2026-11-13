#!/usr/bin/env python3
"""打发布包：把可发布的文件压成 dist/copy-trading-<版本>.zip。

排除：.env / session.json / gui/*.json / bot/data/* / 日志 / 截图 / __pycache__

用法：
    python tools/build_release.py
    python tools/build_release.py --out dist/自定义名.zip
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bot"))

from version import __version__  # noqa: E402

INCLUDE_FILES = ["README.md", "CHANGELOG.md", "LICENSE", "requirements.txt",
                 ".env.example", "启动.bat", "安装依赖.bat"]
INCLUDE_DIRS = {
    "bot": ["*.py"],
    "bot/strategies": ["*.py"],
    "gui": ["app.py", "theme.py"],
    "docs": ["*.md"],
}


def build(out: Path | None = None) -> Path:
    ver = __version__
    root_name = f"copy-trading-{ver}"
    stage = ROOT / "dist" / root_name
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)

    for name in INCLUDE_FILES:
        src = ROOT / name
        if src.exists():
            shutil.copy2(src, stage / name)
    for d, patterns in INCLUDE_DIRS.items():
        dst = stage / d
        dst.mkdir(parents=True)
        for pat in patterns:
            for src in (ROOT / d).glob(pat):
                if src.is_file():
                    shutil.copy2(src, dst / src.name)
    (stage / "bot" / "data").mkdir(parents=True, exist_ok=True)
    (stage / "bot" / "data" / ".keep").write_text(
        "运行时的日志/台账/状态写在这里，不随包发布\n", encoding="utf-8")

    out = out or ROOT / "dist" / f"{root_name}.zip"
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(stage.rglob("*")):
            z.write(p, p.relative_to(stage.parent))

    digest = hashlib.sha256(out.read_bytes()).hexdigest().upper()
    print(f"包：{out}")
    print(f"大小：{out.stat().st_size / 1024 / 1024:.2f} MB")
    print(f"SHA256：{digest}")
    print(f"条目：{len(zipfile.ZipFile(out).namelist())}")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="打发布包")
    ap.add_argument("--out", default="", help="输出 zip 路径")
    args = ap.parse_args()
    build(Path(args.out) if args.out else None)


if __name__ == "__main__":
    main()
