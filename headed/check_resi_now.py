"""Network + residential coverage diagnostics."""
import json
import socket
import urllib.request
from pathlib import Path

from config_store import effective_proxy, load_config, public_view


def port_open(host: str, port: int, timeout: float = 2.0):
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return True, None
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    finally:
        try:
            s.close()
        except Exception:
            pass


def fetch_ip(proxy: str | None = None, timeout: float = 20.0):
    url = "https://api.ipify.org?format=json"
    try:
        if proxy:
            handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        else:
            handler = urllib.request.ProxyHandler({})
        opener = urllib.request.build_opener(handler)
        with opener.open(url, timeout=timeout) as resp:
            return True, json.loads(resp.read().decode())
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def fetch_ipinfo(proxy: str | None = None, timeout: float = 20.0):
    url = "https://ipinfo.io/json"
    try:
        if proxy:
            handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        else:
            handler = urllib.request.ProxyHandler({})
        opener = urllib.request.build_opener(handler)
        with opener.open(url, timeout=timeout) as resp:
            return True, json.loads(resp.read().decode())
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def main():
    cfg = load_config()
    print("=== CONFIG ===")
    print(json.dumps(public_view(cfg), ensure_ascii=False, indent=2))
    print("raw proxy field:", repr(cfg.get("proxy")))
    print("effective_proxy:", repr(effective_proxy(cfg)))

    print("\n=== PORTS ===")
    for host, port, name in [
        ("127.0.0.1", 7890, "clash"),
        ("127.0.0.1", 17890, "resi-bridge"),
        ("127.0.0.1", 8765, "dashboard"),
    ]:
        ok, err = port_open(host, port)
        print(f"{name:12} {host}:{port} -> {'OPEN' if ok else 'CLOSED'} {err or ''}")

    print("\n=== EXIT IPS ===")
    ok, data = fetch_ip(None)
    print("direct:", data if ok else data)
    ok2, data2 = fetch_ip("http://127.0.0.1:17890", timeout=25)
    print("via17890:", data2 if ok2 else data2)
    ok3, data3 = fetch_ip("http://127.0.0.1:7890", timeout=20)
    print("via7890:", data3 if ok3 else data3)

    print("\n=== IPINFO via 17890 ===")
    ok4, info = fetch_ipinfo("http://127.0.0.1:17890", timeout=25)
    print(info if ok4 else info)

    print("\n=== CHROMIUM PROXY CHECK (patchright) ===")
    try:
        from patchright.sync_api import sync_playwright
        from controllers.base_controller import build_proxy_settings, BROWSER_LAUNCH_ARGS

        proxy_url = effective_proxy(cfg) or None
        ps = build_proxy_settings(proxy_url)
        print("launch proxy settings:", ps)
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=list(BROWSER_LAUNCH_ARGS),
                proxy=ps,
            )
            page = browser.new_page()
            page.goto("https://api.ipify.org?format=json", timeout=60000)
            body = page.inner_text("body")
            print("chromium exit:", body)
            page.goto("https://ipinfo.io/json", timeout=60000)
            print("chromium ipinfo:", page.inner_text("body")[:300])
            browser.close()
    except Exception as e:
        print("chromium check FAIL:", type(e).__name__, e)

    # summarize
    print("\n=== SUMMARY ===")
    direct_ip = data.get("ip") if isinstance(data, dict) else None
    via_ip = data2.get("ip") if isinstance(data2, dict) else None
    chrom_ip = None
    try:
        chrom_ip = json.loads(body).get("ip")
    except Exception:
        pass
    print(f"direct(datacenter?): {direct_ip}")
    print(f"bridge 17890:        {via_ip}")
    print(f"chromium via config: {chrom_ip}")
    if via_ip and via_ip != direct_ip:
        print("BRIDGE: exit IP differs from direct — proxy path looks active")
    else:
        print("BRIDGE: residential coverage missing or same as direct IP")
    if chrom_ip and chrom_ip == via_ip:
        print("CHROMIUM: using bridge exit IP")
    elif chrom_ip and chrom_ip == direct_ip:
        print("CHROMIUM: still using machine/direct IP — proxy NOT applied")
    else:
        print("CHROMIUM: unexpected exit", chrom_ip)


if __name__ == "__main__":
    main()
