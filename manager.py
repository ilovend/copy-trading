#!/usr/bin/env python3
"""LuCI manager for shared /opt/ct-app instances."""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.parse
import zlib
from pathlib import Path

BASE = Path("/root/ct-instances")
TRASH = BASE / "trash"
MANIFEST = BASE / "instances.json"
APP = Path("/opt/ct-app")
TICKET = APP / "ticket.py"
NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
WEB_MIN = 9010


def valid(name: str) -> bool:
    return bool(NAME_RE.fullmatch(name or ""))


def root(name: str) -> Path:
    return BASE / name


def cfg(name: str) -> Path:
    return root(name) / "gui" / "copy_config.json"


def envfile(name: str) -> Path:
    return root(name) / ".env"


def session(name: str) -> Path:
    return root(name) / "session.json"


def load_json(path: Path, default):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else default
    except Exception:
        return default


def save_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def env_keys(name: str) -> set[str]:
    keys = set()
    try:
        for line in envfile(name).read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                keys.add(line.split("=", 1)[0].strip())
    except Exception:
        pass
    return keys


def instances() -> list[str]:
    if not BASE.is_dir():
        return []
    return sorted(p.name for p in BASE.iterdir()
                  if p.is_dir() and valid(p.name) and p.name != "trash"
                  and (p / "instance.json").is_file())


def sync() -> list[dict]:
    out = []
    for name in instances():
        meta = load_json(root(name) / "instance.json", {})
        meta["id"] = name
        out.append(meta)
    save_json(MANIFEST, {"schema_version": 2, "instances": [
        {"id": x["id"], "created": x.get("created") or 0} for x in out
    ]})
    return out


def service(name: str, role: str) -> Path:
    return Path("/etc/init.d") / f"copy-{role}-{name}"


def svc_status(name: str, role: str) -> str:
    path = service(name, role)
    if not path.is_file():
        return "missing"
    try:
        r = subprocess.run([str(path), "status"], capture_output=True, text=True, timeout=5)
        return "running" if r.returncode == 0 else "stopped"
    except Exception:
        return "unknown"


def port_used(port: int) -> bool:
    s = socket.socket()
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("0.0.0.0", port))
        return False
    except OSError:
        return True
    finally:
        s.close()


def next_port() -> int:
    used = set()
    for item in sync():
        try:
            used.add(int(item.get("web_port") or 0))
        except Exception:
            pass
    for port in range(WEB_MIN, 65536):
        if port not in used and not port_used(port):
            return port
    raise RuntimeError("no free port")


def write_services(name: str, port: int) -> None:
    lock = 49000 + (zlib.crc32(name.encode()) % 900)
    env = (f"INSTANCE_ROOT={root(name)} WEB_PORT={port} INSTANCE_NAME={name} "
           f"CT_INIT=/etc/init.d/copy-strategy-{name} COPY_INIT=/etc/init.d/copy-copy-{name}")
    script = {
        "web": f"INSTANCE_ROOT={root(name)} WEB_PORT={port} INSTANCE_NAME={name} CT_INIT=/etc/init.d/copy-strategy-{name} COPY_INIT=/etc/init.d/copy-copy-{name}",
        "copy": f"INSTANCE_ROOT={root(name)}",
        "strategy": f"INSTANCE_ROOT={root(name)} STRATEGY_LOCK_PORT={lock}",
    }
    command = {"web": "run_web.py", "copy": "run_copy.py", "strategy": "run_strategy.py"}
    for role in ("web", "copy", "strategy"):
        path = service(name, role)
        text = f"""#!/bin/sh /etc/rc.common
USE_PROCD=1
START=94
STOP=10
start_service() {{
 procd_open_instance
 procd_set_param command /usr/bin/python3 -u {APP}/{command[role]} --instance-root {root(name)}
 procd_set_param env {script[role]}
 procd_set_param respawn 3600 5 5
 procd_set_param stdout 1
 procd_set_param stderr 1
 procd_close_instance
}}
"""
        path.write_text(text, encoding="utf-8")
        path.chmod(0o755)


def create(name: str, port_text: str | None) -> int:
    if not valid(name):
        print("error=invalid name")
        return 2
    if root(name).exists():
        print("error=instance exists")
        return 2
    if port_text:
        try:
            port = int(port_text)
        except ValueError:
            print("error=invalid port")
            return 2
    else:
        port = next_port()
    if not (1 <= port <= 65535) or port_used(port):
        print("error=port unavailable")
        return 2

    dest = root(name)
    (dest / "gui").mkdir(parents=True, exist_ok=False)
    (dest / "bot" / "data").mkdir(parents=True, exist_ok=True)
    (dest / "runtime").mkdir(parents=True, exist_ok=True)
    (dest / ".env").write_text("", encoding="utf-8")
    os.chmod(dest / ".env", 0o600)
    copy_cfg = {
        "mode": "live_dry", "ratio_mode": "net_equity", "exec_equity": 100.0,
        "max_order_notional": 10.0, "max_total_notional": 40.0, "k": 1.0,
        "fixed_notional_usdt": 5.0, "margin_per_trade": 5.0, "leverage": 20,
        "monitored": [], "targets": {},
    }
    save_json(dest / "gui" / "copy_config.json", copy_cfg)
    save_json(dest / "gui" / "strategy_config.json", {})
    save_json(dest / "instance.json", {
        "schema_version": 2, "id": name, "web_port": port,
        "created": int(time.time()), "legacy_source": "",
        "account": {"label": "", "api_state": "missing", "account_group": ""},
        "engines": {
            "copy": {"enabled": False, "config": "gui/copy_config.json"},
            "strategy": {"enabled": False, "config": "gui/strategy_config.json"},
            "active_live": "none",
        },
        "risk": {"trade_lock": "auto"},
        "notify": {"telegram": False, "wechat": False},
    })
    write_services(name, port)
    for role in ("copy", "strategy"):
        subprocess.run([str(service(name, role)), "disable"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run([str(service(name, "web")), "enable"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run([str(service(name, "web")), "start"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    sync()
    print(f"ok={name} port={port}")
    return 0


def ensure_services(name: str, port_text: str | None = None) -> int:
    if not valid(name) or not (root(name) / "instance.json").is_file():
        print("error=instance missing")
        return 2
    meta = load_json(root(name) / "instance.json", {})
    port = int(port_text or meta.get("web_port") or 0)
    if not port:
        port = next_port()
    if port_used(port) and not service(name, "web").is_file():
        print("error=port unavailable")
        return 2
    meta["web_port"] = port
    save_json(root(name) / "instance.json", meta)
    write_services(name, port)
    for role in ("copy", "strategy"):
        subprocess.run([str(service(name, role)), "disable"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run([str(service(name, "web")), "enable"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run([str(service(name, "web")), "start"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    sync()
    print(f"ok={name} port={port}")
    return 0


def ticket(name: str) -> str:
    try:
        r = subprocess.run(
            [sys.executable, str(TICKET)], env={**os.environ, "INSTANCE_ROOT": str(root(name))},
            capture_output=True, text=True, timeout=10,
        )
        return (r.stdout or "").strip()
    except Exception:
        return ""


def session_state(name: str) -> str:
    p = session(name)
    if not p.is_file():
        return "缺失"
    age = time.time() - p.stat().st_mtime
    return "可能过期" if age > 48 * 3600 else "正常"


def status(name: str) -> dict:
    meta = load_json(root(name) / "instance.json", {})
    copy = load_json(cfg(name), {})
    runtime = load_json(root(name) / "runtime" / "runtime-status.json", {})
    return {
        "name": name,
        "port": int(meta.get("web_port") or 0),
        "mode": copy.get("mode", "live_dry"),
        "web": svc_status(name, "web"),
        "copy": svc_status(name, "copy"),
        "strategy": svc_status(name, "strategy"),
        "session": session_state(name),
        "api": "已配置" if {"EXEC_API_KEY", "EXEC_API_SECRET"} <= env_keys(name) else "缺失",
        "runtime": runtime,
    }


def dashboard(host: str) -> str:
    rows = []
    for meta in sync():
        name = meta["id"]
        st = status(name)
        port = st["port"]
        if st["web"] == "running" and port:
            tok = urllib.parse.quote(ticket(name), safe="")
            open_link = (f'<a href="http://{html.escape(host)}:{port}/?ticket={tok}" '
                         f'target="_blank" rel="noopener">打开 :{port} ↗</a>')
        elif port:
            open_link = f'<a href="?do=start_web&amp;id={html.escape(name)}">启动配置页 :{port}</a>'
        else:
            open_link = f'<a href="?do=ensure&amp;id={html.escape(name)}">初始化端口/服务</a>'
        acts = []
        for role, label in (("web", "Web"), ("copy", "复制"), ("strategy", "策略")):
            if st[role] == "running":
                acts.append(f'<a href="?do=stop_{role}&amp;id={name}">停{label}</a>')
                acts.append(f'<a href="?do=restart_{role}&amp;id={name}">重启{label}</a>')
            else:
                acts.append(f'<a href="?do=start_{role}&amp;id={name}">启动{label}</a>')
        acts.append(f'<a href="?log_instance={name}&amp;log_type=copy">copy日志</a>')
        acts.append(f'<a href="?log_instance={name}&amp;log_type=strategy">策略日志</a>')
        if st["copy"] != "running" and st["strategy"] != "running":
            acts.append(f'<a href="?do=delete&amp;id={name}" '
                        f'onclick="return confirm(&apos;删除 {name}？目录会进入 trash&apos;)">删除</a>')
        rows.append(
            "<tr>"
            f"<td><b>{html.escape(name)}</b></td><td>{port or '--'}</td>"
            f"<td>{st['web']}</td><td>{st['copy']}</td><td>{st['strategy']}</td>"
            f"<td>{html.escape(st['mode'])}</td><td>{html.escape(st['session'])}</td>"
            f"<td>{html.escape(st['api'])}</td><td>{open_link}</td>"
            f"<td>{' · '.join(acts)}</td></tr>"
        )
    if not rows:
        rows = ['<tr><td colspan="10">还没有实例</td></tr>']
    return ('<table class="table cbi-section-table" style="min-width:980px"><thead><tr>'
            '<th>实例</th><th>端口</th><th>Web</th><th>复制</th><th>策略</th>'
            '<th>模式</th><th>Session</th><th>API</th><th>配置页</th><th>操作</th>'
            '</tr></thead><tbody>' + "".join(rows) + '</tbody></table>')


def trash_html() -> str:
    rows = []
    if TRASH.is_dir():
        for p in sorted([x for x in TRASH.iterdir() if x.is_dir()], reverse=True):
            name = p.name
            for marker in ((p / ".instance-name"),):
                if marker.is_file():
                    name = marker.read_text(encoding="utf-8").strip() or name
            rows.append(f"<tr><td>{html.escape(name)}</td><td>{html.escape(p.name)}</td>"
                        f'<td><a href="?do=restore&amp;archive={html.escape(p.name)}">恢复</a></td></tr>')
    if not rows:
        rows = ['<tr><td colspan="3">trash 为空</td></tr>']
    return ('<table class="table cbi-section-table"><thead><tr><th>实例</th><th>归档</th><th>操作</th>'
            '</tr></thead><tbody>' + "".join(rows) + '</tbody></table>')


def control(name: str, role: str, action: str) -> int:
    if not valid(name) or not (root(name) / "instance.json").is_file():
        print("error=instance missing")
        return 2
    path = service(name, role)
    if not path.is_file():
        print("error=service missing")
        return 2
    subprocess.run([str(path), action], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"ok={role} {action}")
    return 0


def delete(name: str) -> int:
    if not valid(name) or not (root(name) / "instance.json").is_file():
        print("error=instance missing")
        return 2
    st = status(name)
    if st["copy"] == "running" or st["strategy"] == "running":
        print("error=engine running; stop engines before delete")
        return 3
    for role in ("web", "copy", "strategy"):
        p = service(name, role)
        if p.is_file():
            subprocess.run([str(p), "stop"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run([str(p), "disable"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            p.unlink(missing_ok=True)
    TRASH.mkdir(parents=True, exist_ok=True)
    target = TRASH / f"{name}-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.move(str(root(name)), str(target))
    (target / ".instance-name").write_text(name + "\n", encoding="utf-8")
    sync()
    print(f"ok=moved {target}")
    return 0


def restore(archive: str) -> int:
    name = Path(archive).name
    src = TRASH / name
    if not src.is_dir() or src.parent != TRASH:
        print("error=archive missing")
        return 2
    target_name = name
    marker = src / ".instance-name"
    if marker.is_file():
        target_name = marker.read_text(encoding="utf-8").strip()
    if not valid(target_name) or root(target_name).exists():
        print("error=target exists or invalid")
        return 2
    shutil.move(str(src), str(root(target_name)))
    meta = load_json(root(target_name) / "instance.json", {})
    port = int(meta.get("web_port") or next_port())
    ensure_services(target_name, str(port))
    subprocess.run([str(service(target_name, "web")), "start"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    sync()
    print(f"ok=restored {target_name}")
    return 0


def stop_all() -> int:
    for meta in sync():
        for role in ("copy", "strategy"):
            p = service(meta["id"], role)
            if p.is_file():
                subprocess.run([str(p), "stop"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("ok=all engines stopped")
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        return 2
    cmd = sys.argv[1]
    if cmd == "list":
        print("\n".join(x["id"] for x in sync()))
        return 0
    if cmd == "dashboard" and len(sys.argv) == 3:
        print(dashboard(sys.argv[2]))
        return 0
    if cmd == "trash":
        print(trash_html())
        return 0
    if cmd == "status" and len(sys.argv) == 3:
        print(json.dumps(status(sys.argv[2]), ensure_ascii=False))
        return 0
    if cmd == "ticket" and len(sys.argv) == 3:
        print(ticket(sys.argv[2]))
        return 0
    if cmd == "create" and len(sys.argv) in (3, 4):
        return create(sys.argv[2], sys.argv[3] if len(sys.argv) == 4 else None)
    if cmd == "ensure" and len(sys.argv) in (3, 4):
        return ensure_services(sys.argv[2], sys.argv[3] if len(sys.argv) == 4 else None)
    if cmd == "delete" and len(sys.argv) == 3:
        return delete(sys.argv[2])
    if cmd == "restore" and len(sys.argv) == 3:
        return restore(sys.argv[2])
    if cmd == "stop_all":
        return stop_all()
    if cmd in ("start_web", "stop_web", "restart_web", "start_copy", "stop_copy",
               "restart_copy", "start_strategy", "stop_strategy", "restart_strategy") \
            and len(sys.argv) == 3:
        action, role = cmd.split("_", 1)
        return control(sys.argv[2], role, action)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
