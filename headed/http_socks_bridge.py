"""
本机 HTTP 代理桥：
  - upstream socks5: 本机 →(可选 via Clash)→ SOCKS5 → 目标
  - upstream http:   本机 →(可选 via Clash CONNECT 到上游)→ HTTP 代理 CONNECT → 目标

仅监听 127.0.0.1，不改系统代理。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import re
import struct
import sys
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import urlsplit

DIR = Path(__file__).resolve().parent
if str(DIR) not in sys.path:
    sys.path.insert(0, str(DIR))

from _common import CONFIG_PATH, load_config  # noqa: E402

CRLF = b"\r\n"
HTTP_REQ_RE = re.compile(rb"^([A-Z]+)\s+(\S+)\s+HTTP/1\.[01]\r\n", re.DOTALL)


class ProxyConfig:
    def __init__(self, raw: dict):
        self.raw = raw
        up = raw["upstream"]
        loc = raw["local"]
        self.listen_host = str(loc.get("host", "127.0.0.1"))
        self.listen_port = int(loc["port"])
        self.up_type = str(up.get("type") or "socks5").lower()
        self.up_host = str(up["server"])
        self.up_port = int(up["port"])
        self.up_user = str(up.get("username") or "")
        self.up_pass = str(up.get("password") or "")
        # backward-compatible aliases
        self.socks_host = self.up_host
        self.socks_port = self.up_port
        self.socks_user = self.up_user
        self.socks_pass = self.up_pass
        via = raw.get("via") or {}
        self.via_enabled = bool(via.get("enabled", True))
        self.via_host = str(via.get("server", "127.0.0.1"))
        self.via_port = int(via.get("port", 7890))
        self.via_type = str(via.get("type", "http")).lower()


async def _read_exact(reader: asyncio.StreamReader, n: int) -> bytes:
    return await reader.readexactly(n)


async def _http_connect_tunnel(
    host: str,
    port: int,
    target_host: str,
    target_port: int,
    *,
    username: str = "",
    password: str = "",
    timeout: float = 25.0,
) -> Tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """经上游 HTTP 代理 CONNECT 出一条到 target 的隧道。"""
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port),
        timeout=timeout,
    )
    headers = [
        f"CONNECT {target_host}:{target_port} HTTP/1.1",
        f"Host: {target_host}:{target_port}",
        "Proxy-Connection: keep-alive",
    ]
    if username:
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        headers.append(f"Proxy-Authorization: Basic {token}")
    headers.append("")
    headers.append("")
    writer.write("\r\n".join(headers).encode())
    await writer.drain()

    header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=timeout)
    status_line = header.split(b"\r\n", 1)[0].decode("latin-1", errors="replace")
    if " 200 " not in status_line and not status_line.endswith(" 200"):
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        raise ConnectionError(f"HTTP CONNECT 失败: {status_line} body={header[:180]!r}")
    return reader, writer


async def _socks5_handshake(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    username: str,
    password: str,
    dest_host: str,
    dest_port: int,
    timeout: float = 20.0,
) -> None:
    if username:
        writer.write(b"\x05\x01\x02")
    else:
        writer.write(b"\x05\x01\x00")
    await writer.drain()
    resp = await asyncio.wait_for(_read_exact(reader, 2), timeout=timeout)
    if resp[0] != 0x05:
        raise ConnectionError(f"SOCKS5 版本错误: {resp!r}")
    method = resp[1]
    if method == 0xFF:
        raise ConnectionError("SOCKS5 无可用认证方法")
    if method == 0x02:
        u = username.encode("utf-8")
        p = password.encode("utf-8")
        if len(u) > 255 or len(p) > 255:
            raise ConnectionError("SOCKS5 用户名/密码过长")
        writer.write(b"\x01" + bytes([len(u)]) + u + bytes([len(p)]) + p)
        await writer.drain()
        auth = await asyncio.wait_for(_read_exact(reader, 2), timeout=timeout)
        if auth[1] != 0x00:
            raise ConnectionError("SOCKS5 用户名密码认证失败")
    elif method != 0x00:
        raise ConnectionError(f"SOCKS5 不支持的认证方法: {method}")

    host_b = dest_host.encode("idna")
    if len(host_b) > 255:
        raise ConnectionError("目标主机名过长")
    req = b"\x05\x01\x00\x03" + bytes([len(host_b)]) + host_b + struct.pack("!H", dest_port)
    writer.write(req)
    await writer.drain()

    hdr = await asyncio.wait_for(_read_exact(reader, 4), timeout=timeout)
    if hdr[0] != 0x05:
        raise ConnectionError(f"SOCKS5 响应异常: {hdr!r}")
    rep = hdr[1]
    atyp = hdr[3]
    if atyp == 0x01:
        await _read_exact(reader, 4 + 2)
    elif atyp == 0x03:
        ln = (await _read_exact(reader, 1))[0]
        await _read_exact(reader, ln + 2)
    elif atyp == 0x04:
        await _read_exact(reader, 16 + 2)
    else:
        raise ConnectionError(f"SOCKS5 未知 atyp={atyp}")

    if rep != 0x00:
        errors = {
            0x01: "general failure",
            0x02: "not allowed",
            0x03: "network unreachable",
            0x04: "host unreachable",
            0x05: "connection refused",
            0x06: "ttl expired",
            0x07: "command not supported",
            0x08: "address type not supported",
        }
        raise ConnectionError(f"SOCKS5 CONNECT 失败: {errors.get(rep, rep)}")


async def open_remote(
    cfg: ProxyConfig,
    dest_host: str,
    dest_port: int,
) -> Tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """建立到 dest 的隧道，按 upstream.type 走 socks5 或 http。"""
    if cfg.up_type in ("http", "https"):
        # 可选：先经 via(Clash) CONNECT 到上游 HTTP 代理主机，再由上游 CONNECT 目标
        if cfg.via_enabled:
            # Clash → 上游代理 host:port 的 TCP
            base_r, base_w = await _http_connect_tunnel(
                cfg.via_host, cfg.via_port, cfg.up_host, cfg.up_port
            )
            # 在隧道里对上游发 CONNECT dest（带 basic auth）
            headers = [
                f"CONNECT {dest_host}:{dest_port} HTTP/1.1",
                f"Host: {dest_host}:{dest_port}",
                "Proxy-Connection: keep-alive",
            ]
            if cfg.up_user:
                token = base64.b64encode(f"{cfg.up_user}:{cfg.up_pass}".encode()).decode()
                headers.append(f"Proxy-Authorization: Basic {token}")
            headers.append("")
            headers.append("")
            base_w.write("\r\n".join(headers).encode())
            await base_w.drain()
            header = await asyncio.wait_for(base_r.readuntil(b"\r\n\r\n"), timeout=25.0)
            status_line = header.split(b"\r\n", 1)[0].decode("latin-1", errors="replace")
            if " 200 " not in status_line and not status_line.endswith(" 200"):
                base_w.close()
                raise ConnectionError(
                    f"upstream HTTP CONNECT 失败: {status_line} ({header[:160]!r})"
                )
            return base_r, base_w

        return await _http_connect_tunnel(
            cfg.up_host,
            cfg.up_port,
            dest_host,
            dest_port,
            username=cfg.up_user,
            password=cfg.up_pass,
        )

    # socks5 path
    if cfg.via_enabled:
        base_r, base_w = await _http_connect_tunnel(
            cfg.via_host, cfg.via_port, cfg.up_host, cfg.up_port
        )
    else:
        base_r, base_w = await asyncio.wait_for(
            asyncio.open_connection(cfg.up_host, cfg.up_port),
            timeout=15.0,
        )
    try:
        await _socks5_handshake(
            base_r,
            base_w,
            cfg.up_user,
            cfg.up_pass,
            dest_host,
            dest_port,
        )
    except Exception:
        base_w.close()
        try:
            await base_w.wait_closed()
        except Exception:
            pass
        raise
    return base_r, base_w


# backward name
async def open_via_socks(
    cfg: ProxyConfig, dest_host: str, dest_port: int
) -> Tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    return await open_remote(cfg, dest_host, dest_port)


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except Exception:
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def handle_client(
    client_r: asyncio.StreamReader,
    client_w: asyncio.StreamWriter,
    cfg: ProxyConfig,
) -> None:
    try:
        head = await asyncio.wait_for(client_r.readuntil(b"\r\n"), timeout=30.0)
        rest_headers = await asyncio.wait_for(client_r.readuntil(b"\r\n\r\n"), timeout=30.0)
        first = head + rest_headers
        m = HTTP_REQ_RE.match(first)
        if not m:
            client_w.write(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n")
            await client_w.drain()
            return

        method = m.group(1).decode("ascii", errors="replace")
        target = m.group(2).decode("ascii", errors="replace")

        if method.upper() == "CONNECT":
            if ":" in target:
                host, port_s = target.rsplit(":", 1)
                port = int(port_s)
            else:
                host, port = target, 443
            try:
                remote_r, remote_w = await open_remote(cfg, host, port)
            except Exception as exc:
                msg = (
                    f"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n"
                    f"Content-Length: {len(str(exc))}\r\n\r\n{exc}"
                )
                client_w.write(msg.encode("utf-8", errors="replace"))
                await client_w.drain()
                return
            client_w.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await client_w.drain()
            await asyncio.gather(pipe(client_r, remote_w), pipe(remote_r, client_w))
            return

        parts = urlsplit(target)
        host = parts.hostname
        if not host:
            client_w.write(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n")
            await client_w.drain()
            return
        port = parts.port or (443 if parts.scheme == "https" else 80)
        path = parts.path or "/"
        if parts.query:
            path = path + "?" + parts.query

        try:
            remote_r, remote_w = await open_remote(cfg, host, port)
        except Exception as exc:
            body = str(exc).encode("utf-8", errors="replace")
            client_w.write(
                b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\n\r\n"
                + body
            )
            await client_w.drain()
            return

        header_block = first.split(b"\r\n", 1)[1]
        lines = header_block.split(b"\r\n")
        out_lines = [f"{method} {path} HTTP/1.1".encode("ascii", errors="replace")]
        for line in lines:
            if not line:
                continue
            low = line.lower()
            if low.startswith(b"proxy-connection:"):
                continue
            if low.startswith(b"proxy-authorization:"):
                continue
            if low.startswith(b"connection:"):
                out_lines.append(b"Connection: close")
                continue
            out_lines.append(line)
        if not any(l.lower().startswith(b"host:") for l in out_lines):
            out_lines.insert(
                1,
                f"Host: {host}:{port}".encode()
                if port not in (80, 443)
                else f"Host: {host}".encode(),
            )
        raw_req = b"\r\n".join(out_lines) + b"\r\n\r\n"
        remote_w.write(raw_req)
        await remote_w.drain()
        await asyncio.gather(pipe(client_r, remote_w), pipe(remote_r, client_w))
    except Exception as exc:
        try:
            client_w.write(
                f"HTTP/1.1 500 Internal Server Error\r\nConnection: close\r\n\r\n{exc}".encode(
                    "utf-8", errors="replace"
                )
            )
            await client_w.drain()
        except Exception:
            pass
    finally:
        try:
            client_w.close()
            await client_w.wait_closed()
        except Exception:
            pass


async def run_server(cfg: ProxyConfig) -> None:
    server = await asyncio.start_server(
        lambda r, w: handle_client(r, w, cfg),
        host=cfg.listen_host,
        port=cfg.listen_port,
    )
    sockets = ", ".join(str(s.getsockname()) for s in server.sockets or [])
    mode = f"via http://{cfg.via_host}:{cfg.via_port}" if cfg.via_enabled else "direct"
    print(
        f"[bridge] listen={sockets} upstream={cfg.up_type}://{cfg.up_host}:{cfg.up_port} mode={mode}",
        flush=True,
    )
    async with server:
        await server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="HTTP→上游(SOCKS5/HTTP) 桥接")
    parser.add_argument("--config", default=str(CONFIG_PATH))
    parser.add_argument("--direct", action="store_true", help="直连上游，不经 via/Clash")
    parser.add_argument("--via", default=None, help="via HTTP 代理 host:port，默认 127.0.0.1:7890")
    args = parser.parse_args()

    raw = load_config(Path(args.config))
    if "via" not in raw:
        raw["via"] = {"enabled": True, "type": "http", "server": "127.0.0.1", "port": 7890}
    if args.direct:
        raw["via"]["enabled"] = False
    if args.via:
        host, _, port = args.via.partition(":")
        raw["via"]["enabled"] = True
        raw["via"]["server"] = host or "127.0.0.1"
        raw["via"]["port"] = int(port or 7890)

    cfg = ProxyConfig(raw)
    try:
        asyncio.run(run_server(cfg))
    except KeyboardInterrupt:
        print("[bridge] stopped", flush=True)


if __name__ == "__main__":
    main()
