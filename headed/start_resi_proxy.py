"""
住宅代理本地 HTTP 桥。

链路：
  Chromium → 127.0.0.1:17890（本机 HTTP，无账密）
           → 可选 127.0.0.1:7890（Clash / V2）
           → 你在 resi_proxy_config.json 里填写的住宅 SOCKS5 / HTTP

不修改系统代理，只影响指向本地监听端口的流量。

用法:
  python start_resi_proxy.py
  python start_resi_proxy.py --check
  python start_resi_proxy.py --force
  python start_resi_proxy.py --direct
"""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.request import ProxyHandler, build_opener

DIR = Path(__file__).resolve().parent
CFG_PATH = DIR / "resi_proxy_config.json"
EXAMPLE_PATH = DIR / "resi_proxy_config.example.json"
BRIDGE = DIR / "http_socks_bridge.py"
PID_PATH = DIR / "Results" / "resi_proxy.pid"
LOG_PATH = DIR / "Results" / "resi_proxy.log"


def _load_cfg() -> dict[str, Any]:
    path = CFG_PATH if CFG_PATH.is_file() else EXAMPLE_PATH
    if not path.is_file():
        raise FileNotFoundError(
            "缺少 resi_proxy_config.json。请先复制 resi_proxy_config.example.json 并填入你的住宅代理。"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _listen_addr(cfg: dict[str, Any]) -> tuple[str, int]:
    loc = cfg.get("local") or {}
    return str(loc.get("host") or "127.0.0.1"), int(loc.get("port") or 17890)


def _port_open(host: str, port: int, timeout: float = 0.6) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _read_pid() -> int | None:
    if not PID_PATH.is_file():
        return None
    try:
        return int(PID_PATH.read_text(encoding="utf-8").strip())
    except ValueError:
        return None


def _write_pid(pid: int) -> None:
    PID_PATH.parent.mkdir(parents=True, exist_ok=True)
    PID_PATH.write_text(str(pid), encoding="utf-8")


def _stop() -> None:
    pid = _read_pid()
    if pid:
        try:
            if sys.platform == "win32":
                subprocess.call(
                    ["taskkill", "/PID", str(pid), "/F", "/T"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            else:
                import os
                import signal

                os.kill(pid, signal.SIGTERM)
        except Exception:
            pass
    if PID_PATH.is_file():
        PID_PATH.unlink()


def _probe(host: str, port: int) -> dict[str, Any]:
    proxy = f"http://{host}:{port}"
    try:
        opener = build_opener(ProxyHandler({"http": proxy, "https": proxy}))
        with opener.open("https://api.ipify.org?format=json", timeout=20) as resp:
            data = json.loads(resp.read().decode())
        return {"ok": True, "ip": data.get("ip")}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def start(force: bool = False, direct: bool = False) -> int:
    cfg = _load_cfg()
    host, port = _listen_addr(cfg)
    url = f"http://{host}:{port}"

    if _port_open(host, port) and not force:
        print(f"[resi] 住宅桥已在监听 {url} (pid={_read_pid()})")
        return 0

    if force and _port_open(host, port):
        print("[resi] 重启住宅桥…")
        _stop()
        time.sleep(0.4)

    if not BRIDGE.is_file():
        print(f"[resi] 找不到桥脚本: {BRIDGE}")
        return 1
    if not CFG_PATH.is_file():
        print("[resi] 缺少 resi_proxy_config.json，请先运行 python setup_config.py 并填入住宅代理")
        return 1

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logf = open(LOG_PATH, "a", encoding="utf-8", errors="replace")
    cmd = [sys.executable, str(BRIDGE), "--config", str(CFG_PATH)]
    if direct:
        cmd.append("--direct")

    creationflags = 0
    if sys.platform == "win32":
        creationflags = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
            | 0x00000008
        )
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=logf,
        stderr=subprocess.STDOUT,
        cwd=str(DIR),
        creationflags=creationflags if sys.platform == "win32" else 0,
        start_new_session=(sys.platform != "win32"),
    )
    _write_pid(proc.pid)

    for _ in range(40):
        if _port_open(host, port):
            print(f"[resi] OK 监听 {url} pid={proc.pid}")
            return 0
        if proc.poll() is not None:
            print(f"[resi] 进程退出 code={proc.returncode}，见 Results/resi_proxy.log")
            return 1
        time.sleep(0.15)
    print("[resi] 启动超时，见 Results/resi_proxy.log")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="启动本项目住宅 IP 本地桥")
    parser.add_argument("--force", action="store_true", help="强制重启桥接")
    parser.add_argument("--check", action="store_true", help="启动后校验出口 IP")
    parser.add_argument("--direct", action="store_true", help="不经本地 Clash/via，直连上游")
    parser.add_argument("--stop", action="store_true", help="停止本地桥")
    args = parser.parse_args()

    if args.stop:
        _stop()
        print("[resi] 已请求停止")
        return 0

    code = start(force=args.force, direct=args.direct)
    if code != 0 or not args.check:
        return code

    cfg = _load_cfg()
    host, port = _listen_addr(cfg)
    print("[resi] 校验出口 IP…")
    result = _probe(host, port)
    if result.get("ok"):
        print(f"[resi] 出口探测: OK {result.get('ip')}")
        return 0
    print(f"[resi] 出口探测失败: {result.get('error')}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
