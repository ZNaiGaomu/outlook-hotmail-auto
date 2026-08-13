"""
动态 IP 本地桥（本项目专用）

链路：
  本项目 Chromium
    → 127.0.0.1:17990   本地 HTTP 桥（无账密）
      → 127.0.0.1:7890  可选本地代理（如 Clash）
        → 上游 SOCKS5 / 白名单节点
          → 动态出口 IP

上游账号只写在本地 config.json / dyn_proxy_config.json。
白名单 API 可通过环境变量 DYN_WHITE_API 覆盖。

用法:
  python start_dyn_proxy.py
  python start_dyn_proxy.py --force
  python start_dyn_proxy.py --rotate   # 重新拉 white 节点
  python start_dyn_proxy.py --check
  python start_dyn_proxy.py --stop
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import socket
import ssl
import string
import struct
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

DIR = Path(__file__).resolve().parent
CFG_PATH = DIR / "dyn_proxy_config.json"
PID_PATH = DIR / "Results" / "dyn_proxy.pid"
LOG_PATH = DIR / "Results" / "dyn_proxy.log"
DEFAULT_RAW = ""
LOCAL_HOST = "127.0.0.1"
LOCAL_PORT = 17990
VIA_HOST = "127.0.0.1"
VIA_PORT = 7890

SOCKS_BRIDGE = DIR / "http_socks_bridge.py"
HTTP_BRIDGE = DIR / "dyn_http_bridge.py"

WHITE_API = os.environ.get(
    "DYN_WHITE_API",
    "https://white.1024proxy.com/white/api"
    "?region={region}&num={num}&time={ttl}&format=1&type=txt",
)
DEFAULT_REGIONS = ("JP", "Rand", "US")


def _parse_proxy_url(raw: str) -> dict[str, Any]:
    s = (raw or "").strip()
    if s.startswith("socks5://") or s.startswith("socks5h://") or s.startswith("socks://"):
        s = "http://" + s.split("://", 1)[1]
    if "://" not in s:
        s = "http://" + s
    u = urlparse(s)
    if not u.hostname or not u.port:
        raise ValueError(f"代理 URL 缺 host/port: {raw[:60]}")
    return {
        "server": u.hostname,
        "port": int(u.port),
        "username": unquote(u.username or ""),
        "password": unquote(u.password or ""),
        "scheme": "http",
    }


_SID_BODY_RE = re.compile(r"(sid-)([A-Za-z0-9]{4,16})(-t-\d+)?")
ORIGINAL_SID_PATH = DIR / "Results" / "dyn_original_proxy.txt"


def _new_sid8() -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(8))


def _rotate_sid(username: str) -> str:
    u = username or ""
    m = _SID_BODY_RE.search(u)
    new8 = _new_sid8()
    if m:
        sticky = m.group(3) or "-t-12"
        return u[: m.start()] + f"sid-{new8}{sticky}" + u[m.end() :]
    if u:
        return f"{u}-sid-{new8}-t-12"
    return f"sid-{new8}-t-12"


def remember_original_proxy(raw: str | None = None) -> str:
    if not raw:
        try:
            proj = json.loads((DIR / "config.json").read_text(encoding="utf-8"))
            raw = (proj.get("dynamic_proxy") or DEFAULT_RAW).strip()
        except Exception:
            raw = DEFAULT_RAW
    ORIGINAL_SID_PATH.parent.mkdir(parents=True, exist_ok=True)
    ORIGINAL_SID_PATH.write_text(raw or DEFAULT_RAW, encoding="utf-8")
    return raw or DEFAULT_RAW


def read_original_proxy() -> str:
    if ORIGINAL_SID_PATH.exists():
        try:
            return ORIGINAL_SID_PATH.read_text(encoding="utf-8").strip() or DEFAULT_RAW
        except Exception:
            pass
    return DEFAULT_RAW


def set_dynamic_proxy_url(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("empty proxy url")
    try:
        proj = json.loads((DIR / "config.json").read_text(encoding="utf-8"))
        proj["dynamic_proxy"] = raw
        (DIR / "config.json").write_text(
            json.dumps(proj, ensure_ascii=False, indent=4) + "\n", encoding="utf-8"
        )
    except Exception as e:
        print(f"[dyn] 写 config.json 失败: {e}")
    return raw


def restore_original_proxy() -> str:
    raw = read_original_proxy()
    set_dynamic_proxy_url(raw)
    return raw


def _save_cfg(data: dict[str, Any]) -> None:
    CFG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _load_cfg() -> dict[str, Any]:
    if CFG_PATH.exists():
        try:
            return json.loads(CFG_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _port_open(host: str, port: int, timeout: float = 0.6) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def _read_pid() -> int | None:
    if not PID_PATH.exists():
        return None
    try:
        return int(PID_PATH.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def _write_pid(pid: int) -> None:
    PID_PATH.parent.mkdir(parents=True, exist_ok=True)
    PID_PATH.write_text(str(pid), encoding="utf-8")


def _clear_pid() -> None:
    if PID_PATH.exists():
        try:
            PID_PATH.unlink()
        except Exception:
            pass


def _stop() -> None:
    pid = _read_pid()
    if pid:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                capture_output=True,
                check=False,
            )
        else:
            try:
                os.kill(pid, 15)
            except Exception:
                pass
        _clear_pid()
        time.sleep(0.5)
    # 兜底：清掉仍占 17990 的监听
    if sys.platform == "win32" and _port_open(LOCAL_HOST, LOCAL_PORT):
        subprocess.run(
            [
                "powershell",
                "-Command",
                (
                    f"Get-NetTCPConnection -LocalPort {LOCAL_PORT} -State Listen "
                    "-ErrorAction SilentlyContinue | ForEach-Object { "
                    "Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }"
                ),
            ],
            capture_output=True,
            check=False,
        )
        time.sleep(0.4)


def local_http_url() -> str:
    return f"http://{LOCAL_HOST}:{LOCAL_PORT}"


def is_listening() -> bool:
    return _port_open(LOCAL_HOST, LOCAL_PORT)


def _via_clash_tcp(host: str, port: int, timeout: float = 12.0) -> socket.socket:
    s = socket.create_connection((VIA_HOST, VIA_PORT), timeout=timeout)
    s.settimeout(timeout)
    s.sendall(
        f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode()
    )
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = s.recv(4096)
        if not chunk:
            break
        buf += chunk
    if b"200" not in buf.split(b"\r\n", 1)[0]:
        s.close()
        raise ConnectionError(f"via 7890 CONNECT 失败: {buf[:80]!r}")
    return s


def _socks5_noauth_exit(host: str, port: int, timeout: float = 12.0) -> str | None:
    """经 7890 探测 white SOCKS 节点出口 IP；失败返回 None。"""
    s = _via_clash_tcp(host, int(port), timeout=timeout)
    try:
        s.sendall(b"\x05\x01\x00")
        greet = s.recv(2)
        if greet != b"\x05\x00":
            return None
        dest = b"api.ipify.org"
        s.sendall(
            b"\x05\x01\x00\x03"
            + bytes([len(dest)])
            + dest
            + struct.pack("!H", 443)
        )
        rep = s.recv(32)
        if not rep or len(rep) < 2 or rep[1] != 0:
            return None
        ss = ssl.create_default_context().wrap_socket(s, server_hostname="api.ipify.org")
        try:
            ss.settimeout(timeout)
            ss.sendall(
                b"GET / HTTP/1.1\r\nHost: api.ipify.org\r\nConnection: close\r\n\r\n"
            )
            data = ss.recv(4096).decode("utf-8", "replace")
            ip = data.split("\r\n\r\n", 1)[-1].strip()
            if ip and ip[0].isdigit():
                return ip
            return None
        finally:
            try:
                ss.close()
            except Exception:
                pass
    except Exception:
        try:
            s.close()
        except Exception:
            pass
        return None


def fetch_white_nodes(
    regions: tuple[str, ...] = DEFAULT_REGIONS,
    num: int = 8,
    ttl: int = 15,
) -> list[str]:
    nodes: list[str] = []
    seen: set[str] = set()

    def _http_get(url: str) -> str:
        # 优先经 7890（本机直连 white 常失败或未加白）
        try:
            import requests

            r = requests.get(
                url,
                proxies={
                    "http": f"http://{VIA_HOST}:{VIA_PORT}",
                    "https": f"http://{VIA_HOST}:{VIA_PORT}",
                },
                timeout=20,
            )
            r.raise_for_status()
            return r.text
        except Exception:
            pass
        req = Request(url, headers={"User-Agent": "OutlookRegister-dyn/1.0"})
        with urlopen(req, timeout=20) as resp:
            return resp.read().decode("utf-8", "replace")

    last_msg = ""
    for region in regions:
        url = WHITE_API.format(region=region, num=num, ttl=ttl)
        try:
            body = _http_get(url)
        except Exception as e:
            print(f"[dyn] white API {region} 失败: {type(e).__name__}: {e}")
            continue
        text = (body or "").strip()
        low = text.lower()
        last_msg = text[:160]
        if "not added to whitelist" in low:
            print(f"[dyn] 机房出口未加白: {text}")
            print("[dyn] 请把上面这个 IP 加到 1024 白名单（机房 7890 出口会变，变了再加）")
            continue
        if "insufficient" in low:
            print(f"[dyn] white API 流量不足: {text}")
            continue
        for line in body.replace("\r", "").split("\n"):
            line = line.strip()
            if not line or ":" not in line:
                continue
            if line in seen:
                continue
            # 只要 host:port
            if re.match(r"^[\w\.-]+:\d+$", line):
                seen.add(line)
                nodes.append(line)
    if not nodes and last_msg:
        print(f"[dyn] white 无节点，最后一条: {last_msg}")
    return nodes


def pick_white_node(
    max_try: int = 16,
    regions: tuple[str, ...] = DEFAULT_REGIONS,
) -> dict[str, Any]:
    """拉 white 节点并经 7890 探测，返回 {ok, node, host, port, ip, error}。"""
    if not _port_open(VIA_HOST, VIA_PORT):
        return {
            "ok": False,
            "node": None,
            "host": None,
            "port": None,
            "ip": None,
            "error": f"机房/Clash 未监听 {VIA_HOST}:{VIA_PORT}",
        }
    nodes = fetch_white_nodes(regions=regions)
    if not nodes:
        return {
            "ok": False,
            "node": None,
            "host": None,
            "port": None,
            "ip": None,
            "error": "white API 未返回节点",
        }
    print(f"[dyn] white 候选 {len(nodes)} 个，开始经 7890 探测…")
    errors: list[str] = []
    for node in nodes[: max(1, max_try)]:
        host, _, port_s = node.partition(":")
        try:
            port = int(port_s)
        except ValueError:
            continue
        print(f"[dyn] 试 {node} …", end=" ", flush=True)
        ip = _socks5_noauth_exit(host, port)
        if ip:
            print(f"OK exit={ip}")
            return {
                "ok": True,
                "node": node,
                "host": host,
                "port": port,
                "ip": ip,
                "error": None,
            }
        print("fail")
        errors.append(node)
    return {
        "ok": False,
        "node": None,
        "host": None,
        "port": None,
        "ip": None,
        "error": f"前 {len(errors)} 个 white 节点均不可用",
    }


def _gateway_raw_from_project() -> str:
    try:
        proj = json.loads((DIR / "config.json").read_text(encoding="utf-8"))
        return (proj.get("dynamic_proxy") or DEFAULT_RAW).strip() or DEFAULT_RAW
    except Exception:
        return DEFAULT_RAW


def _build_white_cfg(host: str, port: int, exit_ip: str, node: str) -> dict[str, Any]:
    raw_gw = _gateway_raw_from_project()
    return {
        "name": "dynamic-white-socks-via-local-proxy",
        "expected_exit_ip": exit_ip or "",
        "upstream": {
            "type": "socks5",
            "server": host,
            "port": int(port),
            "username": "",
            "password": "",
        },
        "via": {
            "enabled": True,
            "type": "http",
            "server": VIA_HOST,
            "port": VIA_PORT,
            "comment": "机房/Clash 7890 → 1024 white SOCKS 节点（无账密）",
        },
        "local": {"host": LOCAL_HOST, "port": LOCAL_PORT},
        "raw_url": f"socks5://{host}:{port}",
        "white_node": node,
        "white_api": WHITE_API.format(region="JP", num=1, ttl=10),
        "gateway_url": raw_gw,
        "mode": "white-socks-noauth-via-7890",
    }


def _update_project_meta(node: str, exit_ip: str) -> None:
    try:
        proj = json.loads((DIR / "config.json").read_text(encoding="utf-8"))
        proj["use_dynamic"] = True
        proj["use_residential"] = False
        proj["dynamic_local_http"] = local_http_url()
        proj["proxy"] = local_http_url()
        proj["white_node"] = node
        proj["info"] = (
            f"动态IP: white节点 {node} 经7890 SOCKS无认证"
            + (f"; 探测出口 {exit_ip}" if exit_ip else "")
        )
        if not (proj.get("dynamic_proxy") or "").strip():
            proj["dynamic_proxy"] = DEFAULT_RAW
        (DIR / "config.json").write_text(
            json.dumps(proj, ensure_ascii=False, indent=4) + "\n", encoding="utf-8"
        )
    except Exception as e:
        print(f"[dyn] 更新 config.json 元信息失败: {e}")


def _spawn_bridge(bridge_script: Path, cfg_path: Path, mode: str) -> subprocess.Popen[Any]:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logf = open(LOG_PATH, "a", encoding="utf-8", errors="replace")
    logf.write(f"\n--- dyn start {time.strftime('%Y-%m-%d %H:%M:%S')} mode={mode} ---\n")
    logf.flush()
    creationflags = 0
    if sys.platform == "win32":
        # DETACHED + NEW_GROUP + NO_WINDOW：不随父进程/会话退出
        creationflags = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
            | 0x00000008  # DETACHED_PROCESS
        )
    cwd = str(bridge_script.parent)
    proc = subprocess.Popen(
        [sys.executable, str(bridge_script), "--config", str(cfg_path)],
        stdin=subprocess.DEVNULL,
        stdout=logf,
        stderr=subprocess.STDOUT,
        cwd=cwd,
        creationflags=creationflags if sys.platform == "win32" else 0,
        start_new_session=(sys.platform != "win32"),
        close_fds=True,
    )
    _write_pid(proc.pid)
    return proc


def start(
    force: bool = False,
    rotate: bool = False,
    via_clash: bool | None = None,
) -> int:
    """启动动态 IP 桥。默认 white SOCKS 经 7890；rotate=True 时重新拉节点。"""
    del via_clash  # 白名单模式强制经 7890
    url = local_http_url()

    if is_listening() and not force and not rotate:
        cfg = _load_cfg()
        up = cfg.get("upstream") or {}
        print(f"[dyn] 动态IP桥已在监听 {url} (pid={_read_pid()})")
        print(
            f"[dyn] 上游: {up.get('type', '?')}://{up.get('server')}:{up.get('port')}"
        )
        print(f"[dyn] mode: {cfg.get('mode') or cfg.get('name')}")
        print(f"[dyn] white_node: {cfg.get('white_node')}")
        print(f"[dyn] via: {VIA_HOST}:{VIA_PORT}")
        return 0

    if is_listening() and (force or rotate):
        print("[dyn] 重启动态IP桥…")
        _stop()

    # 优先：已有可用 white 配置且非 rotate
    cfg_existing = _load_cfg()
    reuse = (
        not rotate
        and (cfg_existing.get("mode") == "white-socks-noauth-via-7890")
        and (cfg_existing.get("upstream") or {}).get("server")
        and (cfg_existing.get("upstream") or {}).get("port")
    )
    host = port = node = exit_ip = None
    if reuse:
        up = cfg_existing["upstream"]
        host, port = str(up["server"]), int(up["port"])
        node = str(cfg_existing.get("white_node") or f"{host}:{port}")
        print(f"[dyn] 复用 white 节点 {node}，快速探测…")
        exit_ip = _socks5_noauth_exit(host, port)
        if not exit_ip:
            print("[dyn] 旧节点失效，重新拉取…")
            reuse = False

    if not reuse:
        picked = pick_white_node()
        if not picked.get("ok"):
            print(f"[dyn] white 模式失败: {picked.get('error')}")
            print("[dyn] 提示: 白名单节点不可用；请确认本地 7890 代理正常，或设置 DYN_WHITE_API / config.json")
            return 2
        host = str(picked["host"])
        port = int(picked["port"])
        node = str(picked["node"])
        exit_ip = str(picked["ip"] or "")

    assert host and port and node
    cfg = _build_white_cfg(host, int(port), exit_ip or "", node)
    _save_cfg(cfg)
    _update_project_meta(node, exit_ip or "")
    remember_original_proxy(_gateway_raw_from_project())

    if not SOCKS_BRIDGE.exists():
        print(f"[dyn] 找不到 SOCKS 桥脚本: {SOCKS_BRIDGE}")
        return 1

    mode = f"via-clash→SOCKS5-white {node}"
    print(f"[dyn] 启动动态IP桥 mode={mode}")
    print(f"[dyn] 本地 HTTP: {url}")
    print(f"[dyn] 上游 SOCKS5: {host}:{port} (noauth)")
    if exit_ip:
        print(f"[dyn] 探测出口: {exit_ip}")

    proc = _spawn_bridge(SOCKS_BRIDGE, CFG_PATH, mode)

    for _ in range(50):
        if is_listening():
            print(f"[dyn] OK 监听 {url} pid={proc.pid}")
            return 0
        if proc.poll() is not None:
            print(f"[dyn] 进程退出 code={proc.returncode}，见 Results/dyn_proxy.log")
            return 1
        time.sleep(0.15)
    print("[dyn] 启动超时，见 Results/dyn_proxy.log")
    return 1


def probe_exit_ip(timeout: float = 25.0) -> dict[str, Any]:
    """经本地桥探测出口。用原始 CONNECT+TLS，比 requests 更稳。"""
    url = local_http_url()
    if not is_listening():
        return {"ok": False, "ip": None, "error": f"桥未监听 {url}"}
    try:
        s = socket.create_connection((LOCAL_HOST, LOCAL_PORT), timeout=timeout)
        s.settimeout(timeout)
        s.sendall(
            b"CONNECT api.ipify.org:443 HTTP/1.1\r\n"
            b"Host: api.ipify.org:443\r\n\r\n"
        )
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        if b"200" not in buf.split(b"\r\n", 1)[0]:
            s.close()
            return {"ok": False, "ip": None, "error": f"CONNECT 失败: {buf[:120]!r}"}
        ss = ssl.create_default_context().wrap_socket(
            s, server_hostname="api.ipify.org"
        )
        ss.sendall(
            b"GET / HTTP/1.1\r\nHost: api.ipify.org\r\nConnection: close\r\n\r\n"
        )
        data = ss.recv(4096).decode("utf-8", "replace")
        ss.close()
        ip = data.split("\r\n\r\n", 1)[-1].strip()
        if ip and ip[0].isdigit():
            return {"ok": True, "ip": ip, "error": None}
        return {"ok": False, "ip": None, "error": f"no ip in {data[:80]!r}"}
    except Exception as e:
        return {"ok": False, "ip": None, "error": f"{type(e).__name__}: {e}"}


def check_exit_ip() -> int:
    url = local_http_url()
    if not is_listening():
        print(f"[dyn] 桥未监听: {url}")
        return 1
    cfg = _load_cfg()
    up = cfg.get("upstream") or {}
    print(f"[dyn] 动态IP桥正在监听 {url} (pid={_read_pid()})")
    print(f"[dyn] 上游: {up.get('type')}://{up.get('server')}:{up.get('port')}")
    print(f"[dyn] white_node: {cfg.get('white_node')}")
    print(f"[dyn] via clash: {bool((cfg.get('via') or {}).get('enabled', True))}")
    pr = probe_exit_ip()
    if pr.get("ok"):
        print(f"[dyn] 出口探测: OK {pr.get('ip')}")
        return 0
    print(f"[dyn] 出口探测失败: {pr.get('error')}")
    return 2


def rotate_and_probe(max_attempts: int = 4) -> dict[str, Any]:
    """换 white 节点 → 重启桥 → 探测。"""
    max_attempts = max(1, min(int(max_attempts or 4), 5))
    attempts: list[dict[str, Any]] = []
    for i in range(max_attempts):
        print(f"[dyn] 换出口尝试 {i + 1}/{max_attempts} …")
        code = start(force=True, rotate=True)
        if code != 0:
            attempts.append({"ok": False, "error": f"start code={code}", "ip": None})
            continue
        time.sleep(0.6)
        pr = probe_exit_ip()
        attempts.append(pr)
        if pr.get("ok"):
            print(f"[dyn] 换出口成功 ip={pr.get('ip')}")
            return {
                "ok": True,
                "ip": pr.get("ip"),
                "restored": False,
                "attempts": attempts,
            }
        print(f"[dyn] 探测失败: {pr.get('error')}")
    return {
        "ok": False,
        "ip": None,
        "restored": False,
        "attempts": attempts,
        "error": "多次换 white 节点失败",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="启动本项目动态 IP 本地桥（white SOCKS 经 7890）")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--rotate", action="store_true", help="重新拉取 white 节点后重启")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--via-clash", action="store_true", help="兼容旧参数（默认即经 7890）")
    ap.add_argument("--direct", action="store_true", help="已废弃：white 模式必须经 7890")
    ap.add_argument("--stop", action="store_true")
    args = ap.parse_args()

    if args.stop:
        _stop()
        print("[dyn] stopped")
        return 0
    if args.check and not (args.force or args.rotate):
        # 仅检查：若未监听则先启动
        if not is_listening():
            rc = start(force=False, rotate=False)
            if rc != 0:
                return rc
        return check_exit_ip()
    if args.direct:
        print("[dyn] 警告: white 模式必须经 7890，已忽略 --direct")
    rc = start(force=args.force, rotate=args.rotate)
    if rc == 0 and args.check:
        time.sleep(0.4)
        return check_exit_ip()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
