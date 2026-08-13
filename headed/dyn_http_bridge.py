"""
动态 IP 本地桥：本机 HTTP →（可选经 Clash 7890）→ 上游 HTTP 代理

部分动态代理网关在机房 IP 直连时会拒绝，因此默认可经本地代理再连。
Chromium 只连 127.0.0.1:17990（无账密），其它软件不受影响。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import re
import sys
from pathlib import Path
from typing import Tuple
from urllib.parse import unquote, urlparse

DIR = Path(__file__).resolve().parent
CFG_PATH = DIR / "dyn_proxy_config.json"
CRLF = b"\r\n"
HTTP_REQ_RE = re.compile(rb"^([A-Z]+)\s+(\S+)\s+HTTP/1\.[01]\r\n", re.DOTALL)


def load_cfg(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def basic_auth_header(user: str, password: str) -> bytes:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return f"Proxy-Authorization: Basic {token}\r\n".encode()


async def _read_exact(reader: asyncio.StreamReader, n: int) -> bytes:
    return await reader.readexactly(n)


async def _read_http_headers(
    reader: asyncio.StreamReader, timeout: float = 30.0
) -> bytes:
    """读 HTTP 头，兼容 CRLF 与裸 LF。"""
    deadline = asyncio.get_event_loop().time() + timeout
    buf = b""
    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            raise asyncio.TimeoutError("read HTTP headers timeout")
        try:
            chunk = await asyncio.wait_for(reader.read(1), timeout=remaining)
        except asyncio.TimeoutError as e:
            raise asyncio.TimeoutError(
                f"read HTTP headers timeout partial={buf[:120]!r}"
            ) from e
        if not chunk:
            if buf:
                return buf
            raise ConnectionError("peer closed while reading HTTP headers")
        buf += chunk
        if buf.endswith(b"\r\n\r\n") or buf.endswith(b"\n\n"):
            return buf
        # 1024 偶发把状态行和 body 粘成单段且用 \n
        if (
            len(buf) >= 16
            and buf.startswith(b"HTTP/")
            and b"\n\n" not in buf
            and b"\r\n\r\n" not in buf
            and (b"forbidden" in buf.lower() or b"Proxy-Authenticate" in buf)
            and buf.endswith(b"\n")
        ):
            return buf
        if len(buf) > 65536:
            raise ConnectionError(f"HTTP headers too large: {buf[:120]!r}")


def _status_line(header: bytes) -> str:
    for sep in (b"\r\n", b"\n"):
        if sep in header:
            return header.split(sep, 1)[0].decode("latin-1", errors="replace")
    return header.decode("latin-1", errors="replace")


def _is_http_200(header: bytes) -> bool:
    status = _status_line(header)
    return " 200 " in status or status.endswith(" 200") or status.rstrip().endswith("200")


async def via_http_connect(
    via_host: str,
    via_port: int,
    target_host: str,
    target_port: int,
    timeout: float = 25.0,
) -> Tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(via_host, via_port), timeout=timeout
    )
    req = (
        f"CONNECT {target_host}:{target_port} HTTP/1.1\r\n"
        f"Host: {target_host}:{target_port}\r\n"
        f"Proxy-Connection: keep-alive\r\n\r\n"
    ).encode()
    writer.write(req)
    await writer.drain()
    header = await _read_http_headers(reader, timeout=timeout)
    if not _is_http_200(header):
        writer.close()
        raise ConnectionError(f"via CONNECT 失败: {_status_line(header)}")
    return reader, writer


async def open_upstream(
    cfg: dict, direct: bool
) -> Tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    up = cfg["upstream"]
    host, port = str(up["server"]), int(up["port"])
    via = cfg.get("via") or {}
    use_via = (not direct) and bool(via.get("enabled", True))
    if use_via:
        return await via_http_connect(
            str(via.get("server", "127.0.0.1")),
            int(via.get("port", 7890)),
            host,
            port,
        )
    return await asyncio.wait_for(asyncio.open_connection(host, port), timeout=20)


async def pipe(a: asyncio.StreamReader, b: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await a.read(65536)
            if not data:
                break
            b.write(data)
            await b.drain()
    except Exception:
        pass
    finally:
        try:
            b.close()
        except Exception:
            pass


async def handle_client(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    cfg: dict,
    direct: bool,
) -> None:
    up = cfg["upstream"]
    user = str(up.get("username") or "")
    password = str(up.get("password") or "")
    auth_line = basic_auth_header(user, password) if user else b""
    peer = client_writer.get_extra_info("peername")
    try:
        head = await asyncio.wait_for(client_reader.readuntil(b"\r\n\r\n"), timeout=30)
    except Exception:
        client_writer.close()
        return

    m = HTTP_REQ_RE.match(head)
    if not m:
        client_writer.write(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n")
        await client_writer.drain()
        client_writer.close()
        return

    method = m.group(1).decode("ascii", errors="replace")
    target = m.group(2).decode("latin-1", errors="replace")

    try:
        up_reader, up_writer = await open_upstream(cfg, direct=direct)
    except Exception as e:
        msg = f"HTTP/1.1 502 Bad Gateway\r\nContent-Type: text/plain\r\nConnection: close\r\n\r\nupstream: {e}\n"
        try:
            client_writer.write(msg.encode())
            await client_writer.drain()
        except Exception:
            pass
        client_writer.close()
        return

    try:
        if method.upper() == "CONNECT":
            # Chromium: CONNECT host:443 → 我们转给上游 HTTP 代理同样的 CONNECT + auth
            # 重建请求头，注入 Proxy-Authorization
            hostport = target
            # 丢掉客户端可能自带的 Proxy-Authorization，重写
            lines = head.split(b"\r\n")
            keep = [lines[0]]  # request line
            for ln in lines[1:]:
                if not ln:
                    continue
                low = ln.lower()
                if low.startswith(b"proxy-authorization:"):
                    continue
                if low.startswith(b"proxy-connection:"):
                    continue
                keep.append(ln)
            new_req = b"\r\n".join(keep) + b"\r\n" + auth_line + b"\r\n"
            up_writer.write(new_req)
            await up_writer.drain()
            # 读上游对 CONNECT 的响应（兼容 CRLF / LF）
            resp_head = await _read_http_headers(up_reader, timeout=30)
            # 回给 Chromium 时统一成 CRLF 结尾的标准头
            status = _status_line(resp_head)
            if _is_http_200(resp_head):
                client_writer.write(
                    b"HTTP/1.1 200 Connection Established\r\nProxy-Agent: dyn-http-bridge\r\n\r\n"
                )
            else:
                # 非 200：尽量原样回传（换行规范化）
                norm = resp_head.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
                if not norm.endswith(b"\r\n\r\n"):
                    norm = norm.rstrip(b"\r\n") + b"\r\n\r\n"
                client_writer.write(norm)
            await client_writer.drain()
            if not _is_http_200(resp_head):
                up_writer.close()
                client_writer.close()
                return
            # 双向泵
            await asyncio.gather(
                pipe(client_reader, up_writer),
                pipe(up_reader, client_writer),
            )
        else:
            # 普通 HTTP 绝对 URL 请求：转发给上游，加认证
            lines = head.split(b"\r\n")
            keep = [lines[0]]
            for ln in lines[1:]:
                if not ln:
                    continue
                low = ln.lower()
                if low.startswith(b"proxy-authorization:"):
                    continue
                keep.append(ln)
            new_req = b"\r\n".join(keep) + b"\r\n" + auth_line + b"\r\n"
            up_writer.write(new_req)
            # 若有 body 先不特殊处理（注册流量主要是 CONNECT/TLS）
            await up_writer.drain()
            await asyncio.gather(
                pipe(client_reader, up_writer),
                pipe(up_reader, client_writer),
            )
    except Exception as e:
        try:
            sys.stderr.write(f"[dyn-bridge] peer={peer} err={e}\n")
        except Exception:
            pass
    finally:
        try:
            up_writer.close()
        except Exception:
            pass
        try:
            client_writer.close()
        except Exception:
            pass


async def run_server(cfg: dict, direct: bool) -> None:
    loc = cfg["local"]
    host = str(loc.get("host", "127.0.0.1"))
    port = int(loc["port"])
    up = cfg["upstream"]
    via = cfg.get("via") or {}
    mode = "direct" if direct or not via.get("enabled", True) else f"via http://{via.get('server')}:{via.get('port')}"
    print(
        f"[dyn-bridge] listen=({host!r}, {port}) upstream=http://{up['server']}:{up['port']} mode={mode}",
        flush=True,
    )

    async def _client(r, w):
        await handle_client(r, w, cfg, direct=direct)

    server = await asyncio.start_server(_client, host, port)
    async with server:
        await server.serve_forever()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(CFG_PATH))
    ap.add_argument("--direct", action="store_true", help="不经 Clash（机房 IP 通常 403）")
    args = ap.parse_args()
    cfg = load_cfg(Path(args.config))
    # 强制 upstream type 信息
    if "via" not in cfg:
        cfg["via"] = {"enabled": True, "type": "http", "server": "127.0.0.1", "port": 7890}
    if args.direct:
        cfg["via"]["enabled"] = False
        direct = True
    else:
        direct = not bool(cfg["via"].get("enabled", True))
    try:
        asyncio.run(run_server(cfg, direct=direct))
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
