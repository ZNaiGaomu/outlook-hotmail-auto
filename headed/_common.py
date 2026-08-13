"""ip覆盖 公共工具：读配置、拼 URI、端口探测。仅本项目使用。"""

from __future__ import annotations

import json
import socket
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

DIR = Path(__file__).resolve().parent
CONFIG_PATH = DIR / "config.json"
PID_PATH = DIR / "proxy.pid"
LOG_PATH = DIR / "proxy.log"


def load_config(path: Path | None = None) -> dict[str, Any]:
    cfg_path = path or CONFIG_PATH
    if not cfg_path.is_file():
        example = DIR / "config.example.json"
        raise FileNotFoundError(
            f"缺少 {cfg_path.name}。请复制 {example.name} 为 config.json 并填入住宅 SOCKS5 账号。"
        )
    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    up = data.get("upstream") or {}
    loc = data.get("local") or {}
    up_type = str(up.get("type") or "socks5").lower()
    if up_type not in ("socks5", "http", "https"):
        raise ValueError(f"upstream.type 仅支持 socks5|http|https，收到 {up.get('type')!r}")
    up["type"] = up_type
    data["upstream"] = up
    for key in ("server", "port"):
        if key not in up or up[key] in (None, ""):
            raise ValueError(f"upstream.{key} 必填")
    # username/password optional for open proxies; required for authed providers
    if "host" not in loc or "port" not in loc:
        raise ValueError("local.host / local.port 必填")
    return data


def local_http_url(cfg: dict[str, Any] | None = None) -> str:
    c = cfg or load_config()
    loc = c["local"]
    return f"http://{loc['host']}:{loc['port']}"


def local_listen_uri(cfg: dict[str, Any] | None = None) -> str:
    """pproxy -l 监听：仅本机 HTTP，避免被其它机器误用。"""
    c = cfg or load_config()
    loc = c["local"]
    return f"http://{loc['host']}:{loc['port']}"


def upstream_socks_uri(cfg: dict[str, Any] | None = None) -> str:
    """pproxy -r 上游 SOCKS5。

    pproxy 认证写法是 host:port#user:pass（不是 user:pass@host）。
    标准 user:pass@ 会被误解析成 cipher 列表而报错。
    """
    c = cfg or load_config()
    up = c["upstream"]
    user = quote(str(up["username"]), safe="")
    pwd = quote(str(up["password"]), safe="")
    return f"socks5://{up['server']}:{up['port']}#{user}:{pwd}"


def port_open(host: str, port: int, timeout: float = 0.6) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def is_proxy_listening(cfg: dict[str, Any] | None = None) -> bool:
    c = cfg or load_config()
    loc = c["local"]
    return port_open(str(loc["host"]), int(loc["port"]))


def read_pid() -> int | None:
    if not PID_PATH.is_file():
        return None
    try:
        return int(PID_PATH.read_text(encoding="utf-8").strip())
    except ValueError:
        return None


def write_pid(pid: int) -> None:
    PID_PATH.write_text(str(pid), encoding="utf-8")


def clear_pid() -> None:
    if PID_PATH.is_file():
        PID_PATH.unlink()


def eprint(*args: object) -> None:
    print(*args, file=sys.stderr)
