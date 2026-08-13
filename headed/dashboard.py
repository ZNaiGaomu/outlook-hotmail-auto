"""
OutlookRegister 本地可视化管理面板

功能：
- 住宅 IP 一键开/关（关 = 本机机房流量，不影响其它软件）
- 实时注册进度（每个邮箱当前步骤 + 历史）
- 启动 / 停止注册任务
- 配置编辑（并发、数量、后缀、等待、住宅代理地址）
- 网络诊断（桥端口、住宅出口、本机直连）
- 结果文件查看 / 复制
- 运行日志

启动：
  python dashboard.py
  浏览器打开 http://127.0.0.1:8765
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

DIR = Path(__file__).resolve().parent
os.chdir(DIR)
if str(DIR) not in sys.path:
    sys.path.insert(0, str(DIR))

from config_store import (  # noqa: E402
    effective_proxy,
    load_config,
    public_view,
    save_config,
    set_dynamic,
    set_residential,
    traffic_mode,
)
from progress_bus import BUS  # noqa: E402

HOST = "127.0.0.1"
PORT = 8765

_run_lock = threading.Lock()
_run_thread: threading.Thread | None = None
_stop_flag = threading.Event()
# 每次 start/stop 递增；旧 worker 即使还活着也不再阻塞新任务
_run_generation = 0
_ip_cache_lock = threading.Lock()
_ip_cache: dict[str, Any] = {
    "direct": None,  # {"ok", "ip", "ts", ...}
    "browser": None,
    "ttl": 45.0,
}


def _json_bytes(obj: Any, code: int = 200) -> tuple[int, bytes, str]:
    body = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
    return code, body, "application/json; charset=utf-8"


def _read_results() -> dict[str, Any]:
    results_dir = DIR / "Results"
    out: dict[str, Any] = {"files": {}, "success_lines": [], "export_lines": [], "export_path": ""}
    # 导入用主文件：邮箱----密码----ClientID----Token
    try:
        from export_accounts import EXPORT_ALL, read_export_lines

        export_lines = read_export_lines()
        out["export_lines"] = export_lines
        out["export_path"] = str(EXPORT_ALL)
        out["files"]["export_accounts.txt"] = {
            "exists": EXPORT_ALL.exists(),
            "count": len(export_lines),
            "lines": export_lines[-100:],
            "raw_lines": export_lines[-100:],
            "path": str(EXPORT_ALL),
        }
        # 面板「一键复制」优先用导入格式
        out["success_lines"] = list(export_lines)
    except Exception as e:
        out["files"]["export_accounts.txt"] = {
            "exists": False,
            "error": str(e),
            "lines": [],
            "count": 0,
        }

    for name in ("unlogged_email.txt", "logged_email.txt", "outlook_token.txt"):
        path = results_dir / name
        if not path.exists():
            out["files"][name] = {"exists": False, "lines": [], "count": 0}
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            lines = [ln for ln in text.splitlines() if ln.strip()]
            display = []
            for ln in lines[-100:]:
                if "---" in ln and ln.count("---") >= 3:
                    parts = ln.split("---")
                    display.append("---".join(parts[:2] + ["<token…>"]))
                else:
                    display.append(ln)
            out["files"][name] = {
                "exists": True,
                "count": len(lines),
                "lines": display,
                "raw_lines": lines[-100:],
                "path": str(path),
            }
            if not out["success_lines"] and name in ("unlogged_email.txt", "logged_email.txt"):
                out["success_lines"].extend(lines)
        except Exception as e:
            out["files"][name] = {"exists": True, "error": str(e), "lines": [], "count": 0}

    try:
        extra = BUS.successful_accounts()
        # BUS 里可能是旧格式，导出格式优先已在上面
        if not out["success_lines"]:
            out["success_lines"] = list(dict.fromkeys(extra))
    except Exception:
        pass
    return out


def _kill_browsers() -> dict[str, Any]:
    """强杀本工具相关 Chromium / patchright 子进程，并强制清空运行状态。"""
    global _run_thread, _run_generation
    killed: list[str] = []
    errors: list[str] = []

    # 1) 发停止信号 + 递增 generation，旧 worker 不再占坑
    with _run_lock:
        _run_generation += 1
        _stop_flag.set()
    try:
        from progress_bus import request_global_stop

        request_global_stop()
    except Exception as e:
        errors.append(f"stop_flag: {e}")

    # 2) 杀本工具 Chromium / patchright（不杀日常 Chrome、不杀 dashboard）
    if sys.platform == "win32":
        try:
            me = os.getpid()
            # 用 -match 而不是错误的嵌套 Where-Object
            ps = (
                f"$me = {me}; "
                "Get-CimInstance Win32_Process | ForEach-Object { "
                "  $n = $_.Name; $c = [string]$_.CommandLine; $id = $_.ProcessId; "
                "  if ($id -eq $me) { return }; "
                "  $isBrowser = $n -match '^(chrome|chromium|msedge)\\.exe$'; "
                "  $isPy = $n -match '^python'; "
                "  $ours = $c -match 'outlook_incog_|ms-playwright|patchright|playwright'; "
                "  $dash = $c -match 'dashboard\\.py'; "
                "  if ($dash) { return }; "
                "  if (($isBrowser -or $isPy) -and $ours) { "
                "    try { Stop-Process -Id $id -Force -ErrorAction Stop; $id } catch {} "
                "  } "
                "}"
            )
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=25,
            )
            pids = [x.strip() for x in (r.stdout or "").splitlines() if x.strip().isdigit()]
            killed.extend(pids)
            if r.stderr:
                errors.append(r.stderr[-300:])
        except Exception as e:
            errors.append(str(e))
        # 兜底：按窗口标题/命令行再杀一次 chrome-win（patchright 解压目录）
        try:
            ps3 = (
                "Get-CimInstance Win32_Process | ForEach-Object { "
                "  $c = [string]$_.CommandLine; $n = $_.Name; $id = $_.ProcessId; "
                "  if ($n -match '^(chrome|chromium)\\.exe$' -and $c -match 'chrome-win|chromium-') { "
                "    try { Stop-Process -Id $id -Force -ErrorAction Stop; $id } catch {} "
                "  } "
                "}"
            )
            r3 = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps3],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
            )
            for x in (r3.stdout or "").splitlines():
                x = x.strip()
                if x.isdigit() and x not in killed:
                    killed.append(x)
        except Exception as e:
            errors.append(str(e))
    else:
        try:
            subprocess.run(["pkill", "-f", "ms-playwright"], check=False)
            killed.append("pkill ms-playwright")
        except Exception as e:
            errors.append(str(e))

    # 3) 强制清空运行状态（避免“已有任务在运行”假死）
    try:
        BUS.abort_run(
            f"已停止并强杀浏览器"
            + (f"（killed={len(killed)}）" if killed else "（未匹配到浏览器进程）")
        )
    except Exception:
        try:
            BUS.finish_run("已停止")
        except Exception as e:
            errors.append(f"finish_run: {e}")

    with _run_lock:
        # 旧线程若还活着也解除占用：下一次 start 只看 generation + 实际 alive 且同代
        _run_thread = None

    return {
        "ok": True,
        "killed_pids": killed,
        "errors": errors,
        "running": False,
        "message": "已停止，可重新开始注册",
    }


def _port_open(host: str, port: int, timeout: float = 0.6) -> bool:
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return True
    except Exception:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def _fetch_ip_via_proxy_raw(proxy_url: str | None, timeout: float = 10.0) -> dict[str, Any]:
    """取出口 IP。本地桥优先用 CONNECT+TLS（比 requests 更稳），再回退 requests/urllib。"""
    url = "https://api.ipify.org?format=json"

    # 0) 本地 HTTP 桥：原始 CONNECT + TLS（与浏览器路径一致，避免 requests 读超时假失败）
    if proxy_url and ("127.0.0.1" in proxy_url or "localhost" in proxy_url):
        try:
            u = urlparse(proxy_url)
            host = u.hostname or "127.0.0.1"
            port = int(u.port or 0)
            if port:
                import ssl as _ssl

                sock = socket.create_connection((host, port), timeout=min(timeout, 8.0))
                sock.settimeout(timeout)
                sock.sendall(
                    b"CONNECT api.ipify.org:443 HTTP/1.1\r\n"
                    b"Host: api.ipify.org:443\r\n\r\n"
                )
                buf = b""
                while b"\r\n\r\n" not in buf and len(buf) < 8192:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                if b"200" not in buf.split(b"\r\n", 1)[0]:
                    sock.close()
                    raise ConnectionError(f"CONNECT 失败: {buf[:120]!r}")
                ctx = _ssl.create_default_context()
                ss = ctx.wrap_socket(sock, server_hostname="api.ipify.org")
                ss.sendall(
                    b"GET /?format=json HTTP/1.1\r\n"
                    b"Host: api.ipify.org\r\n"
                    b"Connection: close\r\n\r\n"
                )
                data = b""
                while True:
                    try:
                        c = ss.recv(4096)
                    except Exception:
                        break
                    if not c:
                        break
                    data += c
                ss.close()
                body = data.decode("utf-8", "replace")
                payload = body.split("\r\n\r\n", 1)[-1].strip()
                ip = None
                try:
                    ip = (json.loads(payload) or {}).get("ip")
                except Exception:
                    # 纯文本 IP
                    if payload and payload[0].isdigit():
                        ip = payload.split()[0]
                if ip:
                    return {
                        "ok": True,
                        "ip": ip,
                        "via": proxy_url,
                        "method": "connect-tls",
                    }
                raise RuntimeError(f"no ip in {payload[:80]!r}")
        except Exception as e0:
            err0 = f"{type(e0).__name__}: {e0}"
    else:
        err0 = ""

    # 1) requests
    try:
        import requests

        proxies = None
        if proxy_url:
            proxies = {"http": proxy_url, "https": proxy_url}
        r = requests.get(url, proxies=proxies, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        return {
            "ok": True,
            "ip": data.get("ip"),
            "via": proxy_url or "direct",
            "method": "requests",
        }
    except Exception as e1:
        err1 = f"{type(e1).__name__}: {e1}"

    # 2) urllib 回退
    try:
        if proxy_url:
            handler = urllib.request.ProxyHandler(
                {"http": proxy_url, "https": proxy_url}
            )
            opener = urllib.request.build_opener(handler)
        else:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
            return {
                "ok": True,
                "ip": data.get("ip"),
                "via": proxy_url or "direct",
                "method": "urllib",
            }
    except Exception as e2:
        err2 = f"{type(e2).__name__}: {e2}"

    parts = [x for x in (err0, err1, err2) if x]
    return {
        "ok": False,
        "error": " | ".join(parts) if parts else "unknown",
        "via": proxy_url or "direct",
    }


def _fetch_ip_via_proxy(
    proxy_url: str | None,
    timeout: float = 10.0,
    *,
    use_cache: bool = True,
    cache_key: str | None = None,
) -> dict[str, Any]:
    """带短缓存的出口 IP 探测，避免开关时反复卡 12s+。"""
    key = cache_key or ("browser" if proxy_url else "direct")
    now = time.time()
    if use_cache:
        with _ip_cache_lock:
            hit = _ip_cache.get(key)
            ttl = float(_ip_cache.get("ttl") or 45.0)
            if isinstance(hit, dict) and hit.get("ts") and now - float(hit["ts"]) < ttl:
                out = dict(hit)
                out["cached"] = True
                return out
    result = _fetch_ip_via_proxy_raw(proxy_url, timeout=timeout)
    store = dict(result)
    store["ts"] = now
    with _ip_cache_lock:
        _ip_cache[key] = store
    result = dict(result)
    result["cached"] = False
    return result


def _network_status(*, probe: bool = False, light: bool = False) -> dict[str, Any]:
    """
    probe=False/light=True：只查端口 + 缓存 IP（秒回，给开关用）
    probe=True：强制重新探测出口 IP
    """
    cfg = load_config()
    mode = traffic_mode(cfg)
    use_res = mode == "residential"
    use_dyn = mode == "dynamic"
    resi = (cfg.get("residential_proxy") or "").strip()
    dyn_local = (cfg.get("dynamic_local_http") or "http://127.0.0.1:17990").strip()
    dyn_raw = (cfg.get("dynamic_proxy") or "").strip()
    eff = effective_proxy(cfg)

    bridge_host, bridge_port = "127.0.0.1", 17990 if use_dyn else 17890
    if use_dyn and dyn_local:
        try:
            u = urlparse(dyn_local)
            if u.hostname:
                bridge_host = u.hostname
            if u.port:
                bridge_port = u.port
        except Exception:
            pass
    elif resi:
        try:
            u = urlparse(resi)
            if u.hostname:
                bridge_host = u.hostname
            if u.port:
                bridge_port = u.port
        except Exception:
            pass

    clash_open = _port_open("127.0.0.1", 7890)
    bridge_open = _port_open(bridge_host, bridge_port)
    resi_bridge_open = _port_open("127.0.0.1", 17890)
    dyn_bridge_open = _port_open("127.0.0.1", 17990)

    def _cached_only(key: str) -> dict[str, Any] | None:
        with _ip_cache_lock:
            hit = _ip_cache.get(key)
            ttl = float(_ip_cache.get("ttl") or 45.0)
            if isinstance(hit, dict) and hit.get("ts") and time.time() - float(hit["ts"]) < ttl:
                out = dict(hit)
                out["cached"] = True
                return out
        return None

    # light：只读缓存，绝不发起外网探测（开关/轮询要秒回）
    # probe：强制刷新；默认：有缓存用缓存，无缓存才探一次
    if light and not probe:
        direct = _cached_only("direct") or {
            "ok": False,
            "error": "未探测（点「探测出口 IP」）",
            "via": "direct",
            "cached": False,
        }
        if eff:
            through = _cached_only(f"browser:{eff}") or {
                "ok": False,
                "error": (
                    f"本地桥未监听 {eff}"
                    if (use_dyn and not dyn_bridge_open) or (use_res and not resi_bridge_open)
                    else "未探测（点「探测出口 IP」）"
                ),
                "via": eff,
                "cached": False,
            }
        else:
            through = {
                "ok": bool(direct.get("ok")),
                "ip": direct.get("ip"),
                "via": "direct(no proxy)",
                "note": "未开代理，与直连相同",
                "error": direct.get("error"),
                "cached": direct.get("cached"),
            }
    else:
        use_cache = not probe
        direct = _fetch_ip_via_proxy(
            None, timeout=6.0 if not probe else 10.0, use_cache=use_cache, cache_key="direct"
        )
        if eff:
            if (use_dyn and not dyn_bridge_open) or (use_res and not resi_bridge_open):
                through = {
                    "ok": False,
                    "error": f"本地桥未监听 {eff}（请先点「启动/检查动态桥」）",
                    "via": eff,
                }
            else:
                through = _fetch_ip_via_proxy(
                    eff,
                    timeout=12.0 if probe else 8.0,
                    use_cache=use_cache,
                    cache_key=f"browser:{eff}",
                )
        else:
            through = {
                "ok": bool(direct.get("ok")),
                "ip": direct.get("ip"),
                "via": "direct(no proxy)",
                "note": "未开代理，与直连相同",
                "error": direct.get("error"),
            }

    if use_dyn:
        hint = "动态IP开：浏览器经 127.0.0.1:17990 → 你配置的动态代理出口；本机其它软件不改。"
    elif use_res:
        hint = "住宅开：浏览器应走住宅出口，本机直连仍是机房。"
    else:
        hint = "直连：注册将用本机当前流量（机房 IP），Outlook 成功率通常极低。"

    isolated = None
    if use_dyn or use_res:
        isolated = bool(
            direct.get("ok")
            and through.get("ok")
            and direct.get("ip")
            and through.get("ip")
            and direct.get("ip") != through.get("ip")
        )

    dyn_show = dyn_raw
    if "@" in dyn_raw:
        try:
            u = urlparse(dyn_raw if "://" in dyn_raw else "http://" + dyn_raw)
            user = u.username or ""
            user_s = (user[:18] + "…") if len(user) > 20 else user
            dyn_show = f"http://{user_s}:***@{u.hostname}:{u.port}"
        except Exception:
            dyn_show = "http://***"

    # 机房 7890 出口：1024 白名单要加这个（会变）
    clash_exit: dict[str, Any]
    if not clash_open:
        clash_exit = {"ok": False, "error": "Clash :7890 未开", "via": "7890"}
    elif light and not probe:
        clash_exit = _cached_only("clash:7890") or {
            "ok": False,
            "error": "未探测（点「探测出口 IP」）",
            "via": "http://127.0.0.1:7890",
            "cached": False,
        }
    else:
        clash_exit = _fetch_ip_via_proxy(
            "http://127.0.0.1:7890",
            timeout=8.0 if probe else 6.0,
            use_cache=not probe,
            cache_key="clash:7890",
        )

    # white 节点提示
    white_node = (cfg.get("white_node") or "").strip()
    if use_dyn and white_node:
        hint = f"{hint} 当前 white 节点: {white_node}"
    if clash_exit.get("ok") and clash_exit.get("ip"):
        hint = f"{hint} 机房出口 {clash_exit.get('ip')}（1024 白名单加这个，变了再加）。"

    return {
        "traffic_mode": mode,
        "use_residential": use_res,
        "use_dynamic": use_dyn,
        "residential_proxy": resi,
        "dynamic_proxy": dyn_show,
        "dynamic_local_http": dyn_local,
        "white_node": white_node,
        "effective_proxy": eff or "(本机直连)",
        "ports": {
            "clash_7890": clash_open,
            "bridge": {"host": bridge_host, "port": bridge_port, "open": bridge_open},
            "resi_bridge_17890": resi_bridge_open,
            "dyn_bridge_17990": dyn_bridge_open,
        },
        "direct_ip": direct,
        "browser_exit_ip": through,
        "clash_exit_ip": clash_exit,
        "isolated": isolated,
        "hint": hint,
        "probed": bool(probe),
        "light": bool(light),
    }


def _is_running() -> bool:
    snap = BUS.snapshot()
    st = snap.get("run", {}).get("status")
    if st == "running":
        return True
    if st == "stopping":
        # stopping 超过 8 秒仍占着 → 视为已死，允许重开
        finished = snap.get("run", {}).get("finished_at")
        started = snap.get("run", {}).get("started_at")
        # stopping 没有 finished_at；用日志不够稳，直接看线程
        if _run_thread is not None and _run_thread.is_alive():
            return True
        return False
    return bool(_run_thread is not None and _run_thread.is_alive() and st == "running")


def _start_registration(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    global _run_thread, _run_generation
    with _run_lock:
        # 若 BUS 已 finished/idle 但旧线程残留，先清掉占用
        snap = BUS.snapshot()
        st = (snap.get("run") or {}).get("status")
        alive = _run_thread is not None and _run_thread.is_alive()
        if alive and st in ("running", "stopping"):
            return {"ok": False, "error": "已有任务在运行（可先点「停止并强杀浏览器」）"}
        if alive and st not in ("running", "stopping"):
            # 僵尸线程：不再阻塞
            _run_generation += 1
            _run_thread = None

        cfg = load_config()
        if overrides:
            for k in (
                "max_tasks",
                "concurrent_flows",
                "email_suffix",
                "bot_protection_wait",
                "max_captcha_retries",
                "use_residential",
                "use_dynamic",
                "residential_proxy",
                "dynamic_proxy",
                "dynamic_local_http",
                "choose_browser",
                "incognito",
            ):
                if k in overrides and overrides[k] is not None:
                    cfg[k] = overrides[k]
        cfg = save_config(cfg)
        if bool(cfg.get("use_dynamic")):
            try:
                br = _ensure_dyn_bridge(rotate=False)
                if not br.get("ok"):
                    # 桥失败仍允许启动会立刻代理失败；直接拦下并给可读原因
                    return {
                        "ok": False,
                        "error": "动态IP桥未就绪: "
                        + str(br.get("error") or br.get("stderr") or br.get("stdout") or br)[
                            :300
                        ],
                        "bridge": br,
                    }
            except Exception as e:
                return {"ok": False, "error": f"动态IP桥启动失败: {e}"}
        elif bool(cfg.get("use_residential")):
            try:
                _ensure_resi_bridge()
            except Exception:
                pass

        try:
            from progress_bus import clear_global_stop

            clear_global_stop()
        except Exception:
            pass
        _stop_flag.clear()
        _run_generation += 1
        my_gen = _run_generation

        def worker(gen: int = my_gen) -> None:
            try:
                from main import run_from_config

                run_from_config(str(DIR / "config.json"))
            except Exception as e:
                BUS.finish_run(f"任务异常退出: {type(e).__name__}: {e}")
                BUS.set_run_message(traceback.format_exc()[-500:])
            finally:
                # 仅清自己这一代
                global _run_thread
                with _run_lock:
                    if _run_generation == gen:
                        _run_thread = None

        _run_thread = threading.Thread(
            target=worker, name="outlook-register", daemon=True
        )
        _run_thread.start()
        return {
            "ok": True,
            "message": "已启动（有头浏览器，请看任务栏 Chromium；可点「前置浏览器窗口」）",
            "config": public_view(cfg),
        }


def _ensure_resi_bridge() -> dict[str, Any]:
    """尽量拉起本仓库的住宅桥 start_resi_proxy.py。"""
    try:
        script = DIR / "start_resi_proxy.py"
        if not script.exists():
            return {"ok": False, "error": "start_resi_proxy.py 不存在"}
        r = subprocess.run(
            [sys.executable, str(script), "--check"],
            cwd=str(DIR),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        return {
            "ok": r.returncode == 0,
            "code": r.returncode,
            "stdout": (r.stdout or "")[-2000:],
            "stderr": (r.stderr or "")[-1000:],
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _ensure_dyn_bridge(rotate: bool = False) -> dict[str, Any]:
    """拉起动态 IP 本地 HTTP→SOCKS5 桥（127.0.0.1:17990）。"""
    try:
        script = DIR / "start_dyn_proxy.py"
        if not script.exists():
            return {"ok": False, "error": "start_dyn_proxy.py 不存在"}
        cmd = [sys.executable, str(script)]
        if rotate:
            cmd.append("--rotate")
        cmd.append("--check")
        r = subprocess.run(
            cmd,
            cwd=str(DIR),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=90,
        )
        return {
            "ok": r.returncode == 0,
            "code": r.returncode,
            "stdout": (r.stdout or "")[-2000:],
            "stderr": (r.stderr or "")[-1000:],
            "rotated": bool(rotate),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>OutlookRegister 管理面板</title>
<style>
:root {
  --bg: #0f1419;
  --panel: #1a2332;
  --panel2: #243044;
  --border: #2d3a4f;
  --text: #e7ecf3;
  --muted: #8b9bb4;
  --accent: #3b82f6;
  --good: #22c55e;
  --bad: #ef4444;
  --warn: #f59e0b;
  --chip: #334155;
}
*, *::before, *::after { box-sizing: border-box; }
html { width: 100%; overflow-x: hidden; }
body {
  margin: 0; font-family: "Segoe UI", system-ui, sans-serif;
  background: var(--bg); color: var(--text); min-height: 100vh;
  width: 100%; max-width: 100vw; overflow-x: hidden;
}
header {
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
  padding: 12px 16px; border-bottom: 1px solid var(--border);
  background: linear-gradient(180deg, #152033, #0f1419);
  position: sticky; top: 0; z-index: 10;
  width: 100%; max-width: 100vw;
}
header h1 { font-size: 16px; margin: 0; font-weight: 600; letter-spacing: .2px; }
header .sub { color: var(--muted); font-size: 12px; margin-top: 2px; }
/* 固定版心：避免超长 token 把整页撑出横向滚动 */
.wrap {
  width: 100%;
  max-width: 1100px;
  margin: 0 auto;
  padding: 14px 16px 24px;
  display: grid;
  gap: 12px;
  min-width: 0;
}
.grid2 {
  display: grid;
  grid-template-columns: minmax(0, 1.05fr) minmax(0, .95fr);
  gap: 12px;
  min-width: 0;
}
.grid3 {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px;
  min-width: 0;
}
@media (max-width: 900px) {
  .grid2, .grid3 { grid-template-columns: minmax(0, 1fr); }
  header { flex-wrap: wrap; }
}
.card {
  background: var(--panel); border: 1px solid var(--border);
  border-radius: 12px; padding: 12px 14px;
  min-width: 0;           /* 关键：允许 grid 子项收缩 */
  overflow: hidden;       /* 不让长串撑破卡片 */
}
.card h2 {
  margin: 0 0 10px; font-size: 12px; color: var(--muted);
  text-transform: uppercase; letter-spacing: .6px; font-weight: 600;
}
.stat { font-size: 22px; font-weight: 700; }
.stat small { font-size: 12px; color: var(--muted); font-weight: 500; margin-left: 6px; }
.row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; min-width: 0; }
.btn {
  border: 0; border-radius: 8px; padding: 8px 12px; cursor: pointer;
  font-weight: 600; font-size: 13px; color: #fff;
  background: var(--accent); transition: .15s opacity;
  white-space: nowrap;
}
.btn:hover { opacity: .9; }
.btn:disabled { opacity: .4; cursor: not-allowed; }
.btn.good { background: var(--good); color: #06210f; }
.btn.bad { background: var(--bad); }
.btn.warn { background: var(--warn); color: #1a1200; }
.btn.ghost { background: var(--panel2); color: var(--text); border: 1px solid var(--border); }
.btn.sm { padding: 3px 8px; font-size: 11px; }
.toggle {
  display: inline-flex; align-items: center; gap: 10px;
  background: var(--panel2); border: 1px solid var(--border);
  border-radius: 999px; padding: 6px 8px 6px 14px;
}
.toggle .knob {
  width: 46px; height: 26px; border-radius: 999px; background: #475569;
  position: relative; cursor: pointer; transition: .2s; flex: 0 0 auto;
}
.toggle .knob::after {
  content: ""; position: absolute; top: 3px; left: 3px;
  width: 20px; height: 20px; border-radius: 50%; background: #fff;
  transition: .2s;
}
.toggle.on .knob { background: var(--good); }
.toggle.on .knob::after { left: 23px; }
.badge {
  display: inline-block; padding: 2px 8px; border-radius: 999px;
  font-size: 11px; font-weight: 700; background: var(--chip);
  flex: 0 0 auto;
}
.badge.run { background: #1d4ed8; }
.badge.ok { background: #166534; }
.badge.err { background: #7f1d1d; }
.badge.idle { background: #334155; }
label.field {
  display: flex; flex-direction: column; gap: 4px;
  font-size: 12px; color: var(--muted);
  min-width: 0; flex: 1 1 140px; max-width: 100%;
}
input, select {
  background: #0b1220; border: 1px solid var(--border); color: var(--text);
  border-radius: 8px; padding: 8px 10px; font-size: 13px;
  width: 100%; max-width: 100%; min-width: 0;
}
.job {
  border: 1px solid var(--border); border-radius: 10px; padding: 12px;
  background: #121a27; margin-bottom: 10px;
  min-width: 0; overflow: hidden;
}
.job .top {
  display: flex; justify-content: space-between; gap: 10px;
  align-items: flex-start; min-width: 0;
}
.job .top > div:first-child { min-width: 0; flex: 1 1 auto; overflow: hidden; }
.job .email {
  font-family: ui-monospace, Consolas, monospace; font-size: 13px;
  word-break: break-all; overflow-wrap: anywhere;
}
.steps { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 10px; }
.step {
  font-size: 10px; padding: 3px 6px; border-radius: 6px;
  background: #0b1220; color: var(--muted); border: 1px solid transparent;
}
.step.done { color: #86efac; border-color: #166534; background: #052e16; }
.step.current { color: #93c5fd; border-color: #1d4ed8; background: #172554; }
.step.todo { opacity: .55; }
.detail {
  margin-top: 8px; font-size: 12px; color: var(--muted);
  word-break: break-word; overflow-wrap: anywhere; line-height: 1.45;
}
/* 账号行：缩写显示，复制仍用完整串 */
.acct-box {
  margin-top: 8px; padding: 8px 10px; border-radius: 8px;
  background: #0b1220; border: 1px solid #14532d;
  min-width: 0;
}
.acct-box .acct-line {
  color: #86efac;
  font-family: ui-monospace, Consolas, monospace;
  font-size: 12px;
  line-height: 1.5;
  word-break: break-all;
  overflow-wrap: anywhere;
  white-space: pre-wrap;
  max-width: 100%;
}
.acct-box .acct-meta {
  margin-top: 6px; display: flex; gap: 8px; flex-wrap: wrap; align-items: center;
}
.logs {
  max-height: 280px; overflow: auto;
  font-family: ui-monospace, Consolas, monospace;
  font-size: 11px; background: #0b1220; border-radius: 8px; padding: 10px;
  border: 1px solid var(--border);
  word-break: break-word; overflow-wrap: anywhere;
  min-width: 0;
}
.logs .ok { color: #86efac; }
.logs .err { color: #fca5a5; }
.logs .info { color: #cbd5e1; }
.pre {
  max-height: 260px; overflow: auto;
  white-space: pre-wrap;
  word-break: break-all;
  overflow-wrap: anywhere;
  font-family: ui-monospace, Consolas, monospace; font-size: 11px;
  background: #0b1220; border-radius: 8px; padding: 10px;
  border: 1px solid var(--border);
  line-height: 1.45;
  min-width: 0;
  max-width: 100%;
}
.kv {
  display: grid;
  grid-template-columns: 110px minmax(0, 1fr);
  gap: 6px 10px; font-size: 13px;
  min-width: 0;
}
.kv span { min-width: 0; overflow-wrap: anywhere; word-break: break-word; }
.kv span:first-child { color: var(--muted); }
.oktext { color: var(--good); }
.badtext { color: var(--bad); }
.muted { color: var(--muted); }
.footer {
  text-align: center; color: var(--muted); font-size: 11px;
  padding: 8px 16px 20px; max-width: 1100px; margin: 0 auto;
}
</style>
</head>
<body>
<header>
  <div>
    <h1>OutlookRegister 管理面板</h1>
    <div class="sub">仅监听 127.0.0.1 · 住宅开关只影响本工具 Chromium · 其它软件不改</div>
  </div>
  <div class="row">
    <span id="runBadge" class="badge idle">IDLE</span>
    <button class="btn ghost" onclick="refreshAll()">刷新</button>
  </div>
</header>

<div class="wrap">
  <div class="grid3">
    <div class="card"><h2>本轮提交</h2><div class="stat" id="sSubmitted">0</div></div>
    <div class="card"><h2>成功</h2><div class="stat" id="sOk" style="color:var(--good)">0</div></div>
    <div class="card"><h2>失败</h2><div class="stat" id="sFail" style="color:var(--bad)">0</div></div>
  </div>

  <div class="grid2">
    <div class="card">
      <h2>流量 / IP</h2>
      <div class="row" style="margin-bottom:10px">
        <div id="dynToggle" class="toggle" title="动态 SOCKS5 / 白名单节点（需自行配置）">
          <span id="dynLabel">动态 IP</span>
          <div class="knob" onclick="toggleDyn()"></div>
        </div>
        <div id="resiToggle" class="toggle" title="固定住宅桥 17890">
          <span id="resiLabel">住宅 IP</span>
          <div class="knob" onclick="toggleResi()"></div>
        </div>
      </div>
      <div class="row" style="margin-bottom:12px">
        <button class="btn ghost" onclick="ensureDynBridge()">启动/检查动态桥</button>
        <button class="btn ghost" onclick="rotateDyn()">立刻换出口 IP</button>
        <button class="btn ghost" onclick="ensureBridge()">启动住宅桥</button>
        <button class="btn ghost" onclick="probeNet()">探测出口 IP</button>
      </div>
      <div class="kv" id="netKv">
        <span>状态</span><span class="muted">加载中…</span>
      </div>
      <div class="muted" style="margin-top:10px;font-size:12px" id="netHint"></div>
    </div>

    <div class="card">
      <h2>运行控制</h2>
      <div class="row" style="margin-bottom:12px">
        <button class="btn good" id="btnStart" onclick="startRun()">开始注册</button>
        <button class="btn bad" id="btnStop" onclick="stopRun()">停止并强杀浏览器</button>
        <button class="btn ghost" onclick="saveCfg()">保存配置</button>
        <button class="btn ghost" onclick="focusBrowser()">前置浏览器窗口</button>
      </div>
      <div class="row" style="margin-bottom:10px">
        <div id="incogToggle" class="toggle" title="每次全新临时用户目录，无 Cookie/缓存">
          <span id="incogLabel">无痕模式</span>
          <div class="knob" onclick="toggleIncog()"></div>
        </div>
        <span class="muted" style="font-size:12px">开=每次干净会话（推荐）</span>
      </div>
      <div class="muted" style="font-size:12px;margin-bottom:8px">
        有头 + 无痕：开始后请看任务栏 <b>Chromium</b>；被挡住点「前置浏览器窗口」。
      </div>
      <div class="row">
        <label class="field">数量 max_tasks（本轮注册几个）
          <input id="max_tasks" type="number" min="1" max="100"/>
        </label>
        <label class="field">并发（同时几个窗口）
          <input id="concurrent_flows" type="number" min="1" max="10"/>
        </label>
        <label class="field">后缀
          <select id="email_suffix">
            <option value="@outlook.com">@outlook.com</option>
            <option value="@hotmail.com">@hotmail.com</option>
          </select>
        </label>
        <label class="field">反检测/号间间隔(秒)
          <input id="bot_protection_wait" type="number" min="0" max="120"/>
        </label>
      </div>
      <div class="row" style="margin-top:10px">
        <label class="field">动态代理 URL（HTTP；兼容 socks5 写法）
          <input id="dynamic_proxy" placeholder="http://user:pass@host:port"/>
        </label>
        <label class="field">动态本地桥
          <input id="dynamic_local_http" placeholder="http://127.0.0.1:17990"/>
        </label>
      </div>
      <div class="row" style="margin-top:10px">
        <label class="field">住宅代理 URL（仅本工具）
          <input id="residential_proxy" placeholder="http://127.0.0.1:17890"/>
        </label>
        <label class="field">浏览器引擎
          <select id="choose_browser">
            <option value="patchright">patchright</option>
            <option value="playwright">playwright</option>
          </select>
        </label>
      </div>
      <div class="muted" style="margin-top:10px;font-size:12px" id="runMsg"></div>
    </div>
  </div>

  <div class="grid2">
    <div class="card">
      <h2>注册进度（每个邮箱当前步骤）</h2>
      <div id="jobs"> <div class="muted">暂无任务</div> </div>
    </div>
    <div class="card">
      <h2>实时日志</h2>
      <div class="logs" id="logs"></div>
      <h2 style="margin-top:14px">结果文件</h2>
      <div class="row" style="margin-bottom:8px">
        <button class="btn ghost" onclick="loadResults()">刷新结果</button>
        <button class="btn good" onclick="copySuccess()">一键复制成功账号</button>
      </div>
      <div id="copyHint" class="muted" style="font-size:12px;margin-bottom:6px"></div>
      <div id="results" class="pre muted">点击刷新结果</div>
    </div>
  </div>
</div>
<div class="footer">http://127.0.0.1:8765 · 数据仅存本机 · 关闭住宅后请知悉 Outlook 对机房 IP 极不友好</div>

<script>
const STEP_ORDER = ["queued","browser","open_signup","agree","fill_email","fill_password","fill_birthday","fill_name","captcha","verify","save","done"];
let state = { config: null, progress: null, net: null, results: null };
let resiOn = false;
let dynOn = true;
let incogOn = true;
// 防抖动：仅在内容变化时改 DOM
let _lastJobsHtml = "";
let _lastLogsHtml = "";
let _lastStatsKey = "";
let _lastNetKey = "";
let _lastResultsKey = "";
let _pollBusy = false;
let _cfgDirty = false; // 用户正在改表单时，不覆盖输入框

async function api(path, opts) {
  const r = await fetch(path, Object.assign({ headers: { "Content-Type": "application/json" } }, opts || {}));
  const j = await r.json();
  if (!r.ok && j && j.error) throw new Error(j.error);
  return j;
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

/** 邮箱----密码----ClientID----Token → 展示用多行/缩写；复制仍用原文 */
function formatAccountDisplay(line) {
  const raw = String(line || "");
  if (!raw) return { html: "", full: "" };
  if (raw.includes("----")) {
    const p = raw.split("----");
    while (p.length < 4) p.push("");
    const [email, pass, cid, tok] = p;
    const tokShort = tok
      ? (tok.length > 28 ? tok.slice(0, 18) + "…" + tok.slice(-8) : tok)
      : "(空)";
    const cidShort = cid && cid.length > 20 ? cid.slice(0, 8) + "…" + cid.slice(-4) : (cid || "(空)");
    const html =
      `<div><b>邮箱</b> ${esc(email)}</div>` +
      `<div><b>密码</b> ${esc(pass)}</div>` +
      `<div><b>ClientID</b> ${esc(cidShort)}</div>` +
      `<div><b>Token</b> ${esc(tokShort)}${tok ? ` <span class="muted">(${tok.length}字)</span>` : ""}</div>`;
    return { html, full: raw };
  }
  // 旧格式 email: pass
  const short = raw.length > 120 ? raw.slice(0, 100) + "…" : raw;
  return { html: esc(short), full: raw };
}

/** 结果区：每行过长时 Token 缩写，避免横向撑破 */
function shortenExportLine(line) {
  const s = String(line || "");
  if (!s.includes("----")) {
    return s.length > 160 ? s.slice(0, 140) + "…" : s;
  }
  const p = s.split("----");
  while (p.length < 4) p.push("");
  const tok = p[3] || "";
  if (tok.length > 36) {
    p[3] = tok.slice(0, 20) + "…" + tok.slice(-10) + `(${tok.length})`;
  }
  return p.join("----");
}

function setHtmlIfChanged(el, html, cacheKey) {
  if (!el) return;
  if (state[cacheKey] === html) return;
  state[cacheKey] = html;
  // 尽量保持滚动位置（日志区）
  const top = el.scrollTop;
  el.innerHTML = html;
  if (el.classList && el.classList.contains("logs")) {
    el.scrollTop = top;
  }
}

function setResiUI(on) {
  resiOn = !!on;
  const el = document.getElementById("resiToggle");
  if (el) {
    el.className = "toggle" + (resiOn ? " on" : "");
    document.getElementById("resiLabel").textContent = resiOn ? "住宅 IP：开" : "住宅 IP：关";
  }
}

function setDynUI(on) {
  dynOn = !!on;
  const el = document.getElementById("dynToggle");
  if (el) {
    el.className = "toggle" + (dynOn ? " on" : "");
    document.getElementById("dynLabel").textContent = dynOn ? "动态 IP：开" : "动态 IP：关";
  }
}

function setIncogUI(on) {
  incogOn = on !== false;
  const el = document.getElementById("incogToggle");
  if (!el) return;
  el.className = "toggle" + (incogOn ? " on" : "");
  document.getElementById("incogLabel").textContent = incogOn ? "无痕模式：开" : "无痕模式：关";
}

function fillConfig(cfg, force) {
  state.config = cfg;
  setDynUI(!!cfg.use_dynamic);
  setResiUI(!!cfg.use_residential && !cfg.use_dynamic);
  setIncogUI(cfg.incognito !== false);
  if (_cfgDirty && !force) return; // 用户编辑中不抢焦点
  for (const k of ["max_tasks","concurrent_flows","bot_protection_wait","residential_proxy","dynamic_proxy","dynamic_local_http"]) {
    const el = document.getElementById(k);
    if (el && document.activeElement !== el && cfg[k] != null) el.value = cfg[k];
  }
  const suf = document.getElementById("email_suffix");
  const br = document.getElementById("choose_browser");
  if (suf && document.activeElement !== suf) suf.value = cfg.email_suffix || "@outlook.com";
  if (br && document.activeElement !== br) br.value = cfg.choose_browser || "patchright";
}

function renderNet(net) {
  state.net = net;
  const bridge = net.ports?.bridge || {};
  const mode = net.traffic_mode || (net.use_dynamic ? "dynamic" : (net.use_residential ? "residential" : "direct"));
  setDynUI(mode === "dynamic");
  setResiUI(mode === "residential");
  const key = JSON.stringify([
    mode, net.effective_proxy, net.ports?.clash_7890, net.ports?.dyn_bridge_17990,
    net.ports?.resi_bridge_17890, bridge.open, bridge.port,
    net.direct_ip, net.browser_exit_ip, net.clash_exit_ip, net.isolated, net.hint, net.dynamic_proxy, net.white_node
  ]);
  if (key === _lastNetKey) return;
  _lastNetKey = key;
  const kv = document.getElementById("netKv");
  const modeLabel = mode === "dynamic"
    ? '<span class="oktext">动态 IP</span>'
    : (mode === "residential" ? '<span class="oktext">住宅 IP</span>' : '<span class="badtext">本机直连</span>');
  const clashIp = net.clash_exit_ip;
  const clashCell = clashIp?.ok
    ? ('<span class="oktext">' + esc(clashIp.ip) + '</span> <span class="muted">（1024 加白用这个）</span>')
    : ('<span class="badtext">' + esc(clashIp?.error || "未探测") + '</span>');
  const rows = [
    ["流量模式", modeLabel],
    ["生效代理", esc(net.effective_proxy)],
    ["动态上游(HTTP)", esc(net.dynamic_proxy || "—")],
    ["white 节点", esc(net.white_node || "—")],
    ["动态桥 :17990", net.ports?.dyn_bridge_17990 ? '<span class="oktext">开</span>' : '<span class="badtext">关</span>'],
    ["住宅桥 :17890", net.ports?.resi_bridge_17890 ? '<span class="oktext">开</span>' : '<span class="badtext">关</span>'],
    ["Clash :7890", net.ports?.clash_7890 ? '<span class="oktext">开</span>' : '<span class="badtext">关</span>'],
    ["机房出口 IP", clashCell],
    ["本机直连 IP", net.direct_ip?.ok ? esc(net.direct_ip.ip) : ('<span class="badtext">' + esc(net.direct_ip?.error) + '</span>')],
    ["浏览器出口 IP", net.browser_exit_ip?.ok ? esc(net.browser_exit_ip.ip) : ('<span class="badtext">' + esc(net.browser_exit_ip?.error) + '</span>')],
    ["流量隔离", net.isolated == null ? '—' : (net.isolated ? '<span class="oktext">是（代理≠本机）</span>' : '<span class="badtext">否</span>')],
  ];
  kv.innerHTML = rows.map(([k,v]) => `<span>${k}</span><span>${v}</span>`).join("");
  document.getElementById("netHint").textContent = net.hint || "";
}

function stepClass(job, key) {
  const idx = STEP_ORDER.indexOf(key);
  const cur = STEP_ORDER.indexOf(job.step);
  if (job.status === "success") return "done";
  if (job.status === "failed") {
    if (idx < cur) return "done";
    if (idx === cur) return "current";
    return "todo";
  }
  if (idx < cur) return "done";
  if (idx === cur) return "current";
  return "todo";
}

function renderProgress(p) {
  state.progress = p;
  const run = p.run || {};
  const statsKey = [run.submitted, run.max_tasks, run.succeeded, run.failed, run.status, run.message].join("|");
  if (statsKey !== _lastStatsKey) {
    _lastStatsKey = statsKey;
    document.getElementById("sSubmitted").innerHTML = `${run.submitted||0}<small>/ ${run.max_tasks||0}</small>`;
    document.getElementById("sOk").textContent = run.succeeded || 0;
    document.getElementById("sFail").textContent = run.failed || 0;
    // 不覆盖用户刚点按钮后的临时提示（除非运行状态变了）
    const runMsg = document.getElementById("runMsg");
    if (!runMsg.dataset.sticky || runMsg.dataset.sticky === "0") {
      runMsg.textContent = run.message || "";
    }
    const badge = document.getElementById("runBadge");
    const st = run.status || "idle";
    badge.textContent = st.toUpperCase();
    badge.className = "badge " + (st === "running" ? "run" : st === "stopping" ? "err" : st === "finished" ? (run.succeeded ? "ok" : "err") : "idle");
    document.getElementById("btnStart").disabled = st === "running" || st === "stopping";
    const btnStop = document.getElementById("btnStop");
    if (btnStop) btnStop.disabled = !(st === "running" || st === "stopping");
  }

  const stepsMeta = p.steps || STEP_ORDER.map(k => ({key:k,label:k}));
  const jobs = p.jobs || [];
  let jobsHtml;
  if (!jobs.length) {
    jobsHtml = '<div class="muted">暂无任务 — 点击「开始注册」。开始后请看任务栏 Chromium 窗口。</div>';
  } else {
    jobsHtml = jobs.map(j => {
      const statusBadge = j.status === "success" ? '<span class="badge ok">成功</span>'
        : j.status === "failed" ? '<span class="badge err">失败</span>'
        : '<span class="badge run">进行中</span>';
      const steps = stepsMeta.map(s =>
        `<span class="step ${stepClass(j, s.key)}">${esc(s.label)}</span>`
      ).join("");
      const errText = j.error || (j.status === "failed" ? j.detail : "");
      const errBox = errText
        ? `<div class="detail" style="margin-top:8px;padding:8px 10px;border-radius:8px;background:#3f1d1d;border:1px solid #7f1d1d;color:#fecaca;line-height:1.45">
            <b>错误原因</b><br/>${esc(errText)}
          </div>`
        : "";
      let acct = "";
      if (j.account_line) {
        const disp = formatAccountDisplay(j.account_line);
        // data-copy 用单引号包，内容做 HTML 属性转义；完整串仍可复制
        acct = `<div class="acct-box">
            <div class="acct-line">${disp.html}</div>
            <div class="acct-meta">
              <button class="btn ghost sm" data-copy="${esc(disp.full)}">复制完整行</button>
              <span class="muted" style="font-size:11px">展示已缩写 Token，复制为 邮箱----密码----ClientID----Token</span>
            </div>
          </div>`;
      }
      // 详情里若含超长 token 也截断展示
      let detailShow = String(j.detail || "");
      if (detailShow.length > 160) detailShow = detailShow.slice(0, 140) + "…";
      return `<div class="job">
        <div class="top">
          <div>
            <div class="email">#${j.seq || "?"} ${esc(j.email || "")}</div>
            <div class="detail">${esc(j.step_label || j.step)} — ${esc(detailShow)}</div>
            ${acct}
          </div>
          ${statusBadge}
        </div>
        <div class="steps">${steps}</div>
        ${errBox}
      </div>`;
    }).join("");
  }
  if (jobsHtml !== _lastJobsHtml) {
    _lastJobsHtml = jobsHtml;
    document.getElementById("jobs").innerHTML = jobsHtml;
    // 事件委托复制，避免 inline onclick 抖动/转义问题
    document.getElementById("jobs").onclick = (ev) => {
      const t = ev.target;
      if (t && t.dataset && t.dataset.copy) copyText(t.dataset.copy);
    };
  }

  const logs = (p.logs || []).slice().reverse();
  const logsHtml = logs.map(l => {
    const t = new Date((l.ts||0)*1000).toLocaleTimeString();
    let msg = String(l.message || "");
    // 日志里超长 token / 账号行也截断，避免撑宽
    if (msg.length > 220) msg = msg.slice(0, 200) + "…";
    return `<div class="${esc(l.level||"info")}">[${t}] ${esc(msg)}</div>`;
  }).join("") || '<div class="info">暂无日志</div>';
  if (logsHtml !== _lastLogsHtml) {
    const logsEl = document.getElementById("logs");
    const atBottom = Math.abs(logsEl.scrollTop + logsEl.clientHeight - logsEl.scrollHeight) < 40;
    const prevTop = logsEl.scrollTop;
    _lastLogsHtml = logsHtml;
    logsEl.innerHTML = logsHtml;
    logsEl.scrollTop = atBottom ? logsEl.scrollHeight : prevTop;
  }
}

function renderResults(data) {
  state.results = data;
  const files = data.files || {};
  let txt = "";
  for (const [name, info] of Object.entries(files)) {
    txt += `=== ${name} (${info.count||0}) ===\n`;
    if (info.error) txt += "ERROR " + info.error + "\n";
    else if (!(info.lines||[]).length) txt += "(空)\n";
    else {
      // 展示缩写，避免超长 token 撑破布局；一键复制仍用 success_lines 原文
      const lines = (info.lines || []).map(shortenExportLine);
      txt += lines.join("\n") + "\n";
    }
    txt += "\n";
  }
  if (data.export_path) {
    txt = `导出主文件: ${data.export_path}\n(下方为缩写预览，一键复制为完整 Token)\n\n` + txt;
  }
  if (txt === _lastResultsKey) return;
  _lastResultsKey = txt;
  const el = document.getElementById("results");
  el.className = "pre";
  el.textContent = txt || "(无)";
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    document.getElementById("copyHint").textContent = "已复制到剪贴板";
  } catch (e) {
    prompt("复制以下内容:", text);
  }
}

async function copySuccess() {
  try {
    const j = state.results || await api("/api/results");
    const lines = j.success_lines || [];
    const fromJobs = ((state.progress||{}).jobs||[])
      .filter(x => x.status === "success" && (x.account_line || x.email))
      .map(x => x.account_line || `${x.email}: ${x.password||""}`);
    const all = Array.from(new Set([].concat(lines, fromJobs).filter(Boolean)));
    if (!all.length) {
      document.getElementById("copyHint").textContent = "暂无成功账号可复制";
      return;
    }
    await copyText(all.join("\n"));
    document.getElementById("copyHint").textContent = `已复制 ${all.length} 条成功账号`;
  } catch (e) {
    alert("复制失败: " + e.message);
  }
}

async function stopRun() {
  if (!confirm("确认停止？将强杀本工具启动的 Chromium 窗口，并清空运行状态。")) return;
  const el = document.getElementById("runMsg");
  el.dataset.sticky = "1";
  el.textContent = "正在停止并强杀浏览器…";
  try {
    const j = await api("/api/run/stop", { method: "POST", body: "{}" });
    const n = (j.killed_pids || []).length;
    el.textContent = (j.message || "已停止") + (n ? ` · 结束进程 ${n} 个` : " · 未匹配到浏览器进程");
    if (j.progress) renderProgress(j.progress);
    // 立刻允许再点开始
    document.getElementById("btnStart").disabled = false;
    const btnStop = document.getElementById("btnStop");
    if (btnStop) btnStop.disabled = true;
    setTimeout(() => { el.dataset.sticky = "0"; refreshAll(); }, 600);
  } catch (e) {
    el.dataset.sticky = "0";
    alert("停止失败: " + e.message);
  }
}

async function focusBrowser() {
  try {
    const j = await api("/api/ui/focus", { method: "POST", body: "{}" });
    const el = document.getElementById("runMsg");
    el.dataset.sticky = "1";
    el.textContent = j.message || "已尝试前置浏览器";
    setTimeout(() => { el.dataset.sticky = "0"; }, 2500);
  } catch (e) {
    alert(e.message);
  }
}

async function refreshAll() {
  try {
    const [cfg, prog, net] = await Promise.all([
      api("/api/config"),
      api("/api/progress"),
      api("/api/network?light=1"),
    ]);
    fillConfig(cfg, false);
    renderProgress(prog);
    renderNet(net);
  } catch (e) {
    document.getElementById("runMsg").textContent = "刷新失败: " + e.message;
  }
}

async function pollProgress() {
  if (_pollBusy) return;
  _pollBusy = true;
  try {
    const prog = await api("/api/progress");
    renderProgress(prog);
  } catch (e) {
    // 忽略瞬时失败，避免刷屏
  } finally {
    _pollBusy = false;
  }
}

async function toggleResi() {
  // 乐观更新 UI，避免等网络
  const next = !resiOn;
  setResiUI(next);
  if (next) setDynUI(false);
  try {
    const j = await api("/api/residential", { method: "POST", body: JSON.stringify({ enabled: next }) });
    fillConfig(j.config, true);
    if (j.network) renderNet(j.network);
  } catch (e) {
    setResiUI(!next);
    alert("切换失败: " + e.message);
  }
}

async function toggleDyn() {
  const next = !dynOn;
  setDynUI(next);
  if (next) setResiUI(false);
  const el = document.getElementById("runMsg");
  el.dataset.sticky = "1";
  el.textContent = next ? "动态 IP 已开（后台检查本地桥…）" : "动态 IP 已关";
  try {
    const j = await api("/api/dynamic", { method: "POST", body: JSON.stringify({ enabled: next }) });
    fillConfig(j.config, true);
    if (j.network) renderNet(j.network);
    if (j.bridge && j.bridge.message) {
      el.textContent = j.bridge.message;
    }
    setTimeout(() => { el.dataset.sticky = "0"; }, 2000);
    // 开动态后异步刷新一次真实出口（不阻塞开关）
    if (next) {
      setTimeout(() => { probeNet().catch(()=>{}); }, 800);
    }
  } catch (e) {
    setDynUI(!next);
    el.dataset.sticky = "0";
    alert("切换动态IP失败: " + e.message);
  }
}

async function ensureDynBridge() {
  document.getElementById("runMsg").textContent = "正在检查/启动动态IP桥…";
  try {
    const j = await api("/api/bridge/dyn", { method: "POST", body: JSON.stringify({ rotate: false }) });
    document.getElementById("runMsg").textContent = j.ok
      ? ("动态桥 OK " + ((j.stdout || "").split("\\n").filter(Boolean).slice(-1)[0] || ""))
      : ("动态桥失败: " + (j.error || j.stderr || j.stdout || ""));
    if (j.network) renderNet(j.network);
    else await probeNet();
  } catch (e) {
    alert(e.message);
  }
}

async function rotateDyn() {
  if (!confirm("立刻换出口 IP：会重新拉取 white 节点并重启本地桥。继续？")) return;
  document.getElementById("runMsg").textContent = "正在换出口并探测…";
  try {
    const j = await api("/api/bridge/dyn", { method: "POST", body: JSON.stringify({ rotate: true }) });
    document.getElementById("runMsg").textContent = j.ok
      ? ("已换出口 " + ((j.stdout || "").split("\n").filter(Boolean).slice(-1)[0] || "OK"))
      : ("换出口失败: " + (j.error || j.stderr || ""));
    if (j.network) renderNet(j.network);
    else await probeNet();
  } catch (e) {
    alert(e.message);
  }
}

async function toggleIncog() {
  const next = !incogOn;
  setIncogUI(next);
  try {
    const j = await api("/api/config", {
      method: "POST",
      body: JSON.stringify({ incognito: next }),
    });
    fillConfig(j.config, true);
  } catch (e) {
    setIncogUI(!next);
    alert("切换无痕失败: " + e.message);
  }
}

async function saveCfg() {
  const body = {
    max_tasks: Number(document.getElementById("max_tasks").value || 1),
    concurrent_flows: Number(document.getElementById("concurrent_flows").value || 1),
    bot_protection_wait: Number(document.getElementById("bot_protection_wait").value || 20),
    email_suffix: document.getElementById("email_suffix").value,
    residential_proxy: document.getElementById("residential_proxy").value.trim(),
    dynamic_proxy: (document.getElementById("dynamic_proxy") || {value:""}).value.trim(),
    dynamic_local_http: (document.getElementById("dynamic_local_http") || {value:""}).value.trim(),
    choose_browser: document.getElementById("choose_browser").value,
    use_residential: resiOn,
    use_dynamic: dynOn,
    incognito: incogOn,
  };
  try {
    const j = await api("/api/config", { method: "POST", body: JSON.stringify(body) });
    _cfgDirty = false;
    fillConfig(j.config, true);
    document.getElementById("runMsg").textContent = "配置已保存";
  } catch (e) {
    alert("保存失败: " + e.message);
  }
}

async function startRun() {
  await saveCfg();
  const el = document.getElementById("runMsg");
  el.dataset.sticky = "1";
  el.textContent = "正在启动…";
  try {
    const j = await api("/api/run/start", { method: "POST", body: "{}" });
    el.textContent = (j.message || "已启动") + " — 请看任务栏 Chromium，可点「前置浏览器窗口」";
    // 启动后连点两次前置，提高弹出成功率
    setTimeout(() => { focusBrowser().catch(()=>{}); }, 1200);
    setTimeout(() => { focusBrowser().catch(()=>{}); }, 2800);
    setTimeout(() => { el.dataset.sticky = "0"; refreshAll(); }, 900);
  } catch (e) {
    el.dataset.sticky = "0";
    alert("启动失败: " + e.message);
  }
}

async function ensureBridge() {
  document.getElementById("runMsg").textContent = "正在检查/启动住宅桥…";
  try {
    const j = await api("/api/bridge/ensure", { method: "POST", body: "{}" });
    document.getElementById("runMsg").textContent = j.ok ? "住宅桥 OK" : ("桥失败: " + (j.error || j.stderr || j.stdout || ""));
    if (j.network) renderNet(j.network);
    else await probeNet();
  } catch (e) {
    alert(e.message);
  }
}

async function probeNet() {
  const el = document.getElementById("runMsg");
  const prev = el.textContent;
  el.textContent = "正在探测出口 IP…";
  try {
    const net = await api("/api/network?probe=1");
    renderNet(net);
    const bip = net.browser_exit_ip || {};
    el.textContent = bip.ok
      ? ("浏览器出口 IP: " + bip.ip + (bip.method ? " · " + bip.method : ""))
      : ("出口探测失败: " + (bip.error || "unknown"));
  } catch (e) {
    el.textContent = prev || ("探测失败: " + e.message);
    throw e;
  }
}

async function loadResults() {
  const j = await api("/api/results");
  renderResults(j);
}

// 用户改表单时标记 dirty，避免轮询覆盖
["max_tasks","concurrent_flows","bot_protection_wait","residential_proxy","dynamic_proxy","dynamic_local_http","email_suffix","choose_browser"].forEach(id => {
  const el = document.getElementById(id);
  if (!el) return;
  el.addEventListener("input", () => { _cfgDirty = true; });
  el.addEventListener("change", () => { _cfgDirty = true; });
});

refreshAll();
loadResults();
// 只轮询进度（轻量）；网络/配置不每秒刷，避免整页抖
setInterval(pollProgress, 1500);
// 结果文件低频刷新
setInterval(() => { loadResults().catch(()=>{}); }, 8000);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "OutlookDashboard/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stdout.write("[dashboard] " + (fmt % args) + "\n")

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def do_GET(self) -> None:  # noqa: N802
        try:
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                body = DASHBOARD_HTML.encode("utf-8")
                self._send(200, body, "text/html; charset=utf-8")
                return
            if path == "/api/config":
                code, body, ct = _json_bytes(public_view())
                self._send(code, body, ct)
                return
            if path == "/api/progress":
                code, body, ct = _json_bytes(BUS.snapshot())
                self._send(code, body, ct)
                return
            if path.startswith("/api/network"):
                qs = urlparse(self.path).query or ""
                params = {k: v[0] for k, v in parse_qs(qs).items()}
                do_probe = str(params.get("probe", "")).lower() in ("1", "true", "yes")
                do_light = str(params.get("light", "")).lower() in ("1", "true", "yes")
                # 默认 light（端口+缓存）；只有 probe=1 才强制重探出口
                code, body, ct = _json_bytes(
                    _network_status(
                        probe=do_probe,
                        light=(do_light or not do_probe),
                    )
                )
                self._send(code, body, ct)
                return
            if path == "/api/results":
                code, body, ct = _json_bytes(_read_results())
                self._send(code, body, ct)
                return
            if path == "/api/status":
                code, body, ct = _json_bytes(
                    {
                        "running": _is_running(),
                        "config": public_view(),
                        "progress": BUS.snapshot(),
                    }
                )
                self._send(code, body, ct)
                return
            self._send(404, b'{"error":"not found"}', "application/json")
        except Exception as e:
            code, body, ct = _json_bytes({"error": str(e), "trace": traceback.format_exc()[-800:]}, 500)
            self._send(code, body, ct)

    def do_POST(self) -> None:  # noqa: N802
        try:
            path = urlparse(self.path).path
            data = self._read_json()

            if path == "/api/residential":
                enabled = bool(data.get("enabled", True))
                cfg = set_residential(enabled)
                # 开关只改配置并秒回；桥启动放到后台，避免 UI 卡住
                if enabled:
                    threading.Thread(
                        target=_ensure_resi_bridge, name="ensure-resi", daemon=True
                    ).start()
                payload = {
                    "ok": True,
                    "config": public_view(cfg),
                    "network": _network_status(light=True),
                    "bridge": {"ok": True, "async": True, "message": "住宅桥后台检查中"},
                }
                code, body, ct = _json_bytes(payload)
                self._send(code, body, ct)
                return

            if path == "/api/dynamic":
                enabled = bool(data.get("enabled", True))
                cfg = set_dynamic(enabled)
                if enabled:
                    threading.Thread(
                        target=lambda: _ensure_dyn_bridge(rotate=False),
                        name="ensure-dyn",
                        daemon=True,
                    ).start()
                payload = {
                    "ok": True,
                    "config": public_view(cfg),
                    "network": _network_status(light=True),
                    "bridge": {
                        "ok": True,
                        "async": True,
                        "message": "动态桥后台检查中（可点「启动/检查动态桥」看结果）",
                    },
                }
                code, body, ct = _json_bytes(payload)
                self._send(code, body, ct)
                return

            if path == "/api/config":
                cfg = load_config()
                for k, v in data.items():
                    if k in (
                        "max_tasks",
                        "concurrent_flows",
                        "bot_protection_wait",
                        "max_captcha_retries",
                        "email_suffix",
                        "residential_proxy",
                        "dynamic_proxy",
                        "dynamic_local_http",
                        "choose_browser",
                        "use_residential",
                        "use_dynamic",
                        "incognito",
                        "account_num",
                    ):
                        cfg[k] = v
                cfg = save_config(cfg)
                code, body, ct = _json_bytes({"ok": True, "config": public_view(cfg)})
                self._send(code, body, ct)
                return

            if path == "/api/run/start":
                result = _start_registration(data or None)
                code = 200 if result.get("ok") else 409
                c, body, ct = _json_bytes(result, code)
                self._send(c, body, ct)
                return

            if path == "/api/run/stop":
                result = _kill_browsers()
                # 附带最新进度，前端可立刻刷新按钮状态
                try:
                    result["progress"] = BUS.snapshot()
                    result["running"] = _is_running()
                except Exception:
                    result["running"] = False
                c, body, ct = _json_bytes(result)
                self._send(c, body, ct)
                return

            if path == "/api/ui/focus":
                try:
                    from controllers.base_controller import focus_browser_window

                    n = focus_browser_window()
                    if isinstance(n, int):
                        msg = (
                            f"已前置本工具 Chromium {n} 个窗口"
                            if n > 0
                            else "未找到本工具 Chromium（其它浏览器不会被前置）"
                        )
                    else:
                        msg = "已尝试前置 Chromium 窗口，请看任务栏"
                except Exception as e:
                    msg = f"前置失败: {e}"
                c, body, ct = _json_bytes({"ok": True, "message": msg})
                self._send(c, body, ct)
                return

            if path == "/api/bridge/ensure":
                result = _ensure_resi_bridge()
                result["network"] = _network_status(probe=True)
                c, body, ct = _json_bytes(result)
                self._send(c, body, ct)
                return

            if path == "/api/bridge/dyn":
                rotate = bool(data.get("rotate", False))
                result = _ensure_dyn_bridge(rotate=rotate)
                result["network"] = _network_status(probe=True)
                c, body, ct = _json_bytes(result)
                self._send(c, body, ct)
                return

            self._send(404, b'{"error":"not found"}', "application/json")
        except Exception as e:
            code, body, ct = _json_bytes({"error": str(e), "trace": traceback.format_exc()[-800:]}, 500)
            self._send(code, body, ct)


def main() -> None:
    # 启动时把 config 按 use_residential 规范一次
    try:
        save_config(load_config())
    except Exception as e:
        print(f"[dashboard] config normalize warn: {e}")

    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{PORT}/"
    print("=" * 58)
    print("  OutlookRegister 可视化管理面板")
    print(f"  打开: {url}")
    print("  功能: 动态IP / 住宅IP / 实时进度 / 停止强杀 / 错误原因 / 复制成功账号 / 网络诊断")
    print("  默认动态IP: 127.0.0.1:17990 → 你在 config.json 中配置的动态代理")
    print("  有头浏览器：开始注册后请看任务栏 Chromium；可用「前置浏览器窗口」")
    print("  Ctrl+C 结束面板")
    print("=" * 58)
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[dashboard] stopped")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
