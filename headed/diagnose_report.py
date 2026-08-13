"""
非交互诊断报告：不重试注册，只收集 IP / 时区 / 指纹关键项并打印清单。
与 main 使用同一 proxy + Asia/Tokyo + WebRTC 防护。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from patchright.sync_api import sync_playwright

from controllers.base_controller import (
    BROWSER_CONTEXT_KW,
    BROWSER_LAUNCH_ARGS,
    apply_browser_context_guards,
    build_proxy_settings,
    harden_page_webrtc,
    install_webrtc_navigation_guard,
)

EXPECTED_EXIT_IP = os.environ.get("EXPECTED_EXIT_IP", "").strip()
OUT_DIR = Path("Results") / "diagnose"
SCREENSHOT = OUT_DIR / "diagnose_snapshot.png"


def _ok(flag: bool) -> str:
    return "PASS" if flag else "FAIL"


def _fetch_json(page, url: str, timeout: int = 45000):
    page.goto(url, timeout=timeout, wait_until="domcontentloaded")
    page.wait_for_timeout(800)
    text = page.inner_text("body").strip()
    try:
        return json.loads(text), text
    except Exception:
        return None, text


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    cfg = json.loads(Path("config.json").read_text(encoding="utf-8"))
    proxy_url = cfg.get("proxy") or None
    proxy_settings = build_proxy_settings(proxy_url)

    report: dict = {
        "proxy_config": proxy_url,
        "proxy_parsed": proxy_settings,
        "context": BROWSER_CONTEXT_KW,
        "launch_args": list(BROWSER_LAUNCH_ARGS),
        "webrtc_guard": True,
        "checks": {},
        "notes": [],
    }

    print("=" * 60)
    print(" OutlookRegister 诊断清单（WebRTC 防护后 / 不注册）")
    print("=" * 60)
    print(f"[cfg] proxy   = {proxy_url}")
    print(f"[cfg] parsed  = {proxy_settings}")
    print(f"[cfg] locale  = {BROWSER_CONTEXT_KW['locale']}")
    print(f"[cfg] tz      = {BROWSER_CONTEXT_KW['timezone_id']}")
    print(f"[cfg] expect  = {EXPECTED_EXIT_IP or '(unset, any public IP is OK)'}")
    print(f"[cfg] webrtc  = guard ON")
    print()

    t0 = time.time()
    with sync_playwright() as p:
        # 有头更接近 main；无显示环境再退回 headless
        try:
            browser = p.chromium.launch(
                headless=False,
                args=list(BROWSER_LAUNCH_ARGS),
                proxy=proxy_settings,
            )
            headed = True
        except Exception:
            browser = p.chromium.launch(
                headless=True,
                args=list(BROWSER_LAUNCH_ARGS),
                proxy=proxy_settings,
            )
            headed = False
        report["headed"] = headed
        print(f"[cfg] headed  = {headed}")

        context = browser.new_context(**BROWSER_CONTEXT_KW)
        apply_browser_context_guards(context)
        page = context.new_page()
        install_webrtc_navigation_guard(page)

        # 1) 出口 IP
        print("[1] 出口 IP (ipify) ...")
        exit_ip = None
        try:
            page.goto("https://api.ipify.org?format=json", timeout=45000)
            harden_page_webrtc(page)  # 导航后强制重放（patchright init 不可靠）
            body = page.inner_text("body").strip()
            exit_ip = json.loads(body).get("ip")
            match = bool(exit_ip) and (not EXPECTED_EXIT_IP or exit_ip == EXPECTED_EXIT_IP)
            report["checks"]["exit_ip"] = {
                "value": exit_ip,
                "expected": EXPECTED_EXIT_IP or None,
                "pass": match,
            }
            print(f"    实际: {exit_ip}")
            if EXPECTED_EXIT_IP:
                print(f"    期望: {EXPECTED_EXIT_IP}  [{_ok(match)}]")
            else:
                print(f"    已取得出口 IP  [{_ok(match)}]")
            if EXPECTED_EXIT_IP and not match:
                report["notes"].append("出口 IP 与 EXPECTED_EXIT_IP 不一致，检查本地桥 / 代理节点")
        except Exception as e:
            report["checks"]["exit_ip"] = {"pass": False, "error": str(e)}
            report["notes"].append(f"取出口 IP 失败: {e}")
            print(f"    FAIL: {e}")

        # 2) ipinfo
        print("[2] IP 画像 (ipinfo.io/json) ...")
        try:
            data, raw = _fetch_json(page, "https://ipinfo.io/json")
            harden_page_webrtc(page)
            if data:
                country = data.get("country")
                city = data.get("city")
                org = data.get("org")
                timezone = data.get("timezone")
                ip = data.get("ip")
                org_l = (org or "").lower()
                hosting_hint = any(
                    k in org_l
                    for k in ("host", "cloud", "server", "data center", "datacenter", "vps")
                )
                jp_ok = country == "JP"
                report["checks"]["ipinfo"] = {
                    "ip": ip,
                    "country": country,
                    "city": city,
                    "org": org,
                    "timezone": timezone,
                    "pass_country_jp": jp_ok,
                    "hosting_hint": hosting_hint,
                }
                print(f"    ip/country/city: {ip} / {country} / {city}  [{_ok(jp_ok)}]")
                print(f"    org: {org}")
                print(f"    ipinfo.timezone: {timezone}")
                print(f"    hosting 启发式: {hosting_hint}")
                if hosting_hint:
                    report["notes"].append("org 名称像机房/云，微软可能仍当低质 IP")
            else:
                report["checks"]["ipinfo"] = {"pass": False, "raw": raw[:300]}
                print(f"    非 JSON: {raw[:200]}")
        except Exception as e:
            report["checks"]["ipinfo"] = {"pass": False, "error": str(e)}
            print(f"    FAIL: {e}")

        # 3) 浏览器环境
        print("[3] 浏览器时区 / 语言 ...")
        try:
            info = page.evaluate(
                """() => ({
                    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
                    locale: Intl.DateTimeFormat().resolvedOptions().locale,
                    languages: navigator.languages,
                    language: navigator.language,
                    platform: navigator.platform,
                    hardwareConcurrency: navigator.hardwareConcurrency,
                    deviceMemory: navigator.deviceMemory || null,
                    webdriver: navigator.webdriver,
                    userAgent: navigator.userAgent,
                    hasRTCPeerConnection: typeof window.RTCPeerConnection,
                    guardFlag: window.__OUTLOOK_WEBRTC_GUARD__ || null,
                    lockedKeys: window.__OUTLOOK_WEBRTC_LOCKED__ || null,
                })"""
            )
            tz_ok = info.get("timezone") == "Asia/Tokyo"
            lang_ok = str(info.get("language", "")).lower().startswith("zh")
            report["checks"]["browser_env"] = {
                **info,
                "pass_timezone_tokyo": tz_ok,
                "pass_lang_zh": lang_ok,
            }
            print(f"    timezone: {info.get('timezone')}  [{_ok(tz_ok)}]")
            print(f"    language: {info.get('language')} / {info.get('languages')}  [{_ok(lang_ok)}]")
            print(f"    webdriver: {info.get('webdriver')}")
            print(f"    RTCPeerConnection typeof: {info.get('hasRTCPeerConnection')}")
            print(f"    guard flag: {info.get('guardFlag')}, locked={info.get('lockedKeys')}")
            if not tz_ok:
                report["notes"].append("浏览器时区不是 Asia/Tokyo")
        except Exception as e:
            report["checks"]["browser_env"] = {"pass": False, "error": str(e)}
            print(f"    FAIL: {e}")

        # 4) WebRTC 泄漏复测
        print("[4] WebRTC ICE / 构造测试 ...")
        harden_page_webrtc(page)
        try:
            rtc = page.evaluate(
                """async () => {
                    const result = {
                        constructError: null,
                        candidates: [],
                        publicIps: [],
                    };
                    let pc = null;
                    try {
                        pc = new RTCPeerConnection({
                            iceServers: [{urls: ['stun:stun.l.google.com:19302']}]
                        });
                    } catch (e) {
                        result.constructError = String(e && e.message ? e.message : e);
                        return result;
                    }
                    try {
                        pc.createDataChannel('x');
                        const offer = await pc.createOffer();
                        await pc.setLocalDescription(offer);
                        await new Promise(r => setTimeout(r, 5000));
                        const sdp = (pc.localDescription && pc.localDescription.sdp) || '';
                        const lines = sdp.split('\\n').filter(l => l.includes('candidate:'));
                        result.candidates = lines.slice(0, 12);
                        for (const line of lines) {
                            const parts = line.replaceAll('/', ' ').split(/\\s+/);
                            for (const p of parts) {
                                if ((p.match(/\\./g) || []).length === 3) {
                                    if (!p.startsWith('127.') && !p.startsWith('0.') &&
                                        !p.startsWith('192.168.') && !p.startsWith('10.') &&
                                        !p.startsWith('172.16.')) {
                                        result.publicIps.push(p);
                                    }
                                }
                            }
                        }
                        result.publicIps = Array.from(new Set(result.publicIps));
                    } catch (e) {
                        result.constructError = String(e && e.message ? e.message : e);
                    } finally {
                        try { pc && pc.close(); } catch (e) {}
                    }
                    return result;
                }"""
            )
            public_ips = rtc.get("publicIps") or []
            construct_error = rtc.get("constructError")
            # 成功防护：构造被拒，或没有任何公网 candidate
            leak_free = (construct_error is not None) or (len(public_ips) == 0)
            # 若只有住宅出口本身，也算可接受
            if public_ips and exit_ip and set(public_ips) <= {exit_ip}:
                leak_free = True
            bad_ips = [ip for ip in public_ips if ip != exit_ip]
            report["checks"]["webrtc"] = {
                "construct_error": construct_error,
                "candidates_sample": rtc.get("candidates") or [],
                "public_ips": public_ips,
                "bad_public_ips": bad_ips,
                "pass_no_leak": leak_free and not bad_ips,
            }
            if construct_error:
                print(f"    RTC 构造/使用被拦截: {construct_error}")
            print(f"    candidate 行数: {len(rtc.get('candidates') or [])}")
            print(f"    公网 IP 候选: {public_ips or '[]'}")
            print(f"    非住宅泄漏: {bad_ips or '[]'}  [{_ok(not bad_ips)}]")
            print(f"    WebRTC 防泄漏: [{_ok(leak_free and not bad_ips)}]")
            if bad_ips:
                report["notes"].append(f"WebRTC 仍泄漏: {bad_ips}")
        except Exception as e:
            report["checks"]["webrtc"] = {"pass_no_leak": False, "error": str(e)}
            print(f"    FAIL: {e}")

        # 5) browserscan
        print("[5] browserscan 可达性 + 截图 ...")
        try:
            t_bs = time.time()
            page.goto("https://www.browserscan.net/", timeout=60000, wait_until="domcontentloaded")
            page.wait_for_timeout(10000)
            load_s = round(time.time() - t_bs, 1)
            title = page.title()
            page.screenshot(path=str(SCREENSHOT), full_page=False)
            report["checks"]["browserscan"] = {
                "title": title,
                "load_seconds": load_s,
                "screenshot": str(SCREENSHOT),
                "pass_reachable": True,
                "pass_speed_ok": load_s < 25,
            }
            print(f"    title: {title}")
            print(f"    加载约 {load_s}s  [{'PASS' if load_s < 25 else 'SLOW'}]")
            print(f"    截图: {SCREENSHOT}")
            if load_s >= 25:
                report["notes"].append(f"browserscan 加载 {load_s}s，链路偏慢")
        except Exception as e:
            report["checks"]["browserscan"] = {"pass_reachable": False, "error": str(e)}
            report["notes"].append(f"browserscan 打开失败: {e}")
            print(f"    FAIL: {e}")

        # 6) 本机直连隔离
        print("[6] 本机直连公网 IP（应 ≠ 住宅）...")
        try:
            import urllib.request

            with urllib.request.urlopen("https://api.ipify.org?format=json", timeout=15) as resp:
                direct = json.loads(resp.read().decode()).get("ip")
            isolated = bool(exit_ip) and direct != exit_ip
            report["checks"]["direct_ip"] = {
                "value": direct,
                "pass_isolated_from_resi": isolated,
            }
            print(f"    本机直连: {direct}")
            print(f"    与住宅隔离: {isolated}  [{_ok(isolated)}]")
            if not isolated:
                report["notes"].append("本机直连 IP 与住宅相同，可能系统代理被全局接管")
        except Exception as e:
            report["checks"]["direct_ip"] = {"error": str(e)}
            print(f"    FAIL: {e}")

        context.close()
        browser.close()

    elapsed = round(time.time() - t0, 1)
    report["elapsed_seconds"] = elapsed

    print()
    print("=" * 60)
    print(" 结论清单")
    print("=" * 60)
    c = report["checks"]
    rows = [
        ("已取得代理出口 IP", c.get("exit_ip", {}).get("pass")),
        ("国家/地区为 JP", c.get("ipinfo", {}).get("pass_country_jp")),
        ("浏览器时区 Asia/Tokyo", c.get("browser_env", {}).get("pass_timezone_tokyo")),
        ("浏览器语言 zh*", c.get("browser_env", {}).get("pass_lang_zh")),
        ("WebRTC 无真实公网泄漏", c.get("webrtc", {}).get("pass_no_leak")),
        ("browserscan 可达", c.get("browserscan", {}).get("pass_reachable")),
        ("链路速度可接受 (<25s)", c.get("browserscan", {}).get("pass_speed_ok")),
        ("本机流量与住宅隔离", c.get("direct_ip", {}).get("pass_isolated_from_resi")),
    ]
    for name, flag in rows:
        if flag is None:
            status = "N/A"
        else:
            status = "PASS" if flag else "FAIL"
        print(f"  [{status}] {name}")

    if report["notes"]:
        print()
        print(" 备注 / 风险:")
        for n in report["notes"]:
            print(f"  - {n}")
    else:
        print()
        print(" 备注: 无额外风险项")

    print()
    print(f"总耗时 {elapsed}s | 详细 JSON: {OUT_DIR / 'report.json'}")
    print("本次 intentionally 不跑 main.py / 不提交注册。")

    (OUT_DIR / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
