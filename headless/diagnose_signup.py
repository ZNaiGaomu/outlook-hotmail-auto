"""
诊断：用和 main.py 相同的浏览器配置去打开 Outlook 注册页，
把页面 URL / title / 可见文字 / 屏幕截图 dump 出来，便于判断：
- 代理是否生效
- 页面是否被 Microsoft 的地理/IP 封拦截了
- UI 语言是中文还是英文
- "同意并继续" 按钮到底存不存在

用法（服务器上，在项目目录）：
    source .venv/bin/activate
    xvfb-run -a python diagnose_signup.py

输出在控制台，截图保存到 /tmp/signup.png
"""

import json
import sys
from patchright.sync_api import sync_playwright


from urllib.parse import urlparse


def build_proxy_settings(proxy_url):
    if not proxy_url:
        return None
    parsed = urlparse(proxy_url)
    if not parsed.hostname:
        return None
    host = parsed.hostname + (f":{parsed.port}" if parsed.port else "")
    settings = {"server": f"{parsed.scheme or 'http'}://{host}", "bypass": "localhost"}
    if parsed.username:
        settings["username"] = parsed.username
    if parsed.password:
        settings["password"] = parsed.password
    return settings


URL = "https://outlook.live.com/mail/0/?prompt=create_account"
SCREENSHOT_PATH = "/tmp/signup.png"


def main():
    with open("config.json", "r", encoding="utf-8") as f:
        cfg = json.load(f)

    proxy = cfg.get("proxy") or ""
    print(f"[diag] proxy = {proxy or '<none>'}")

    proxy_settings = build_proxy_settings(proxy)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=["--lang=zh-CN"],
            proxy=proxy_settings,
        )
        context = browser.new_context(
            locale="zh-CN",
            timezone_id="America/Anchorage",
        )
        page = context.new_page()

        print(f"[diag] 先测代理出口 IP...")
        try:
            ip_page = context.new_page()
            ip_page.goto("https://api.ipify.org", timeout=15000)
            print(f"[diag] 出口 IP = {ip_page.inner_text('body').strip()}")
            ip_page.close()
        except Exception as e:
            print(f"[diag] 取 IP 失败: {e}")

        print(f"[diag] goto {URL}")
        try:
            page.goto(URL, timeout=30000, wait_until="domcontentloaded")
        except Exception as e:
            print(f"[diag] goto 失败: {e}")

        page.wait_for_timeout(15000)  # 给一点时间让 JS 渲染

        try:
            cur_url = page.url
        except Exception:
            cur_url = "<unknown>"
        try:
            title = page.title()
        except Exception:
            title = "<unknown>"
        try:
            body_text = page.locator("body").inner_text(timeout=5000)
        except Exception as e:
            body_text = f"<读取失败: {e}>"
        try:
            html = page.content()
        except Exception as e:
            html = f"<HTML 读取失败: {e}>"

        print("=" * 60)
        print(f"[diag] 当前 URL: {cur_url}")
        print(f"[diag] 页面标题: {title}")
        print("=" * 60)
        print("[diag] 可见正文（前 2000 字）:")
        print(body_text[:2000])
        print("=" * 60)
        print(f"[diag] HTML 长度: {len(html)}")
        print("[diag] HTML 前 1500 字:")
        print(html[:1500])
        print("=" * 60)

        # 检查常见中英文同意按钮
        markers = [
            "同意并继续", "同意", "接受并继续",
            "Agree and continue", "Agree", "Accept",
        ]
        found = []
        for m in markers:
            try:
                if page.get_by_text(m, exact=False).count() > 0:
                    found.append(m)
            except Exception:
                pass
        print(f"[diag] 找到的按钮关键词: {found if found else '无'}")

        try:
            page.screenshot(path=SCREENSHOT_PATH, full_page=True)
            print(f"[diag] 截图已保存: {SCREENSHOT_PATH}")
        except Exception as e:
            print(f"[diag] 截图失败: {e}")

        context.close()
        browser.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
