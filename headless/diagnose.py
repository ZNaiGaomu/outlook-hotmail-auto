"""
诊断脚本：用 patchright 启动浏览器，打开 browserscan + whoer，
让你目测 IP / 浏览器指纹是否干净。
用 config.json 里的 proxy 配置，尽量和 main.py 的启动参数一致。

用法：
    python diagnose.py

窗口打开后手动查看：
1. browserscan.net  —— Bot 检测、WebRTC、指纹一致性是否全绿
2. whoer.net        —— IP / Anonymity 分数
3. ipinfo.io        —— IP 类型 residential / hosting / business

关闭窗口即退出。
"""

import json
from patchright.sync_api import sync_playwright


def main():
    with open("config.json", "r", encoding="utf-8") as f:
        cfg = json.load(f)
    proxy_url = cfg.get("proxy") or None

    proxy_settings = (
        {"server": proxy_url, "bypass": "localhost"} if proxy_url else None
    )

    print(f"[diagnose] 使用代理: {proxy_url or '<无>'}")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=["--lang=zh-CN"],
            proxy=proxy_settings,
        )
        context = browser.new_context()
        page = context.new_page()

        for url in (
            "https://www.browserscan.net/",
            "https://whoer.net/",
            "https://ipinfo.io/",
        ):
            try:
                new_page = context.new_page()
                new_page.goto(url, timeout=30000)
            except Exception as e:
                print(f"[diagnose] 打开 {url} 失败: {e}")

        print("[diagnose] 已打开三个检测页。关闭浏览器窗口即退出。")
        try:
            page.wait_for_event("close", timeout=0)
        except Exception:
            pass

        context.close()
        browser.close()


if __name__ == "__main__":
    main()
