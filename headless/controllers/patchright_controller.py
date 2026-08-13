import random
from patchright.sync_api import sync_playwright
from .base_controller import BaseBrowserController, build_proxy_settings, rotate_cliproxy_sid


class PatchrightController(BaseBrowserController):

    def launch_browser(self):
        try:
            p = sync_playwright().start()

            rotated = rotate_cliproxy_sid(self.proxy)
            if rotated != self.proxy:
                try:
                    sid = rotated.split('sid-')[1].split('-')[0].split('@')[0]
                    print(f"[Proxy] 本次使用 sticky session: sid-{sid}")
                except Exception:
                    pass
            proxy_settings = build_proxy_settings(rotated)

            b = p.chromium.launch(
                headless=False,
                args=['--lang=zh-CN'],
                proxy=proxy_settings
            )

            return p, b

        except Exception as e:
            print(f"启动浏览器失败: {e}")
            return False, False

    def handle_captcha(self, page):
        CHALLENGE_IFRAME = 'iframe[title="验证质询"], iframe[title="Verification challenge"]'
        ACCESS_CHALLENGE = '[aria-label="可访问性挑战"], [aria-label="Accessibility challenge"]'
        PRESS_AGAIN = '[aria-label="再次按下"], [aria-label="Press again"], [aria-label="Press and hold"]'

        print(f"[Captcha] 进入验证码阶段, URL: {page.url}")
        try:
            print(f"[Captcha] 页面标题: {page.title()}")
            iframes = page.locator('iframe').all()
            print(f"[Captcha] 页面共 {len(iframes)} 个 iframe:")
            for i, ifr in enumerate(iframes[:10]):
                try:
                    a = ifr.evaluate("el => ({title: el.title, id: el.id, name: el.name, src: (el.src||'').slice(0,80)})")
                    print(f"  [iframe {i}] {a}")
                except Exception:
                    pass
        except Exception as _de:
            print(f"[Captcha] dump iframes 失败: {_de}")

        frame1 = page.frame_locator(CHALLENGE_IFRAME)
        frame2 = frame1.frame_locator('iframe[style*="display: block"]')

        def _safe_count(locator):
            try:
                return locator.count()
            except Exception:
                return 0

        def _hard_pass():
            """真通过的硬信号：iframe 整个消失 OR URL 已跳离 signup。"""
            if _safe_count(page.locator(CHALLENGE_IFRAME)) == 0:
                return True
            if 'signup.live.com' not in page.url:
                return True
            return False

        # 一轮 attempt = 按一次。Arkose 可能连续给多轮子挑战，所以循环次数要足够大。
        max_rounds = max(self.max_captcha_retries + 1, 6)

        for attempt in range(max_rounds):
            page.wait_for_timeout(300)

            if _hard_pass():
                print(f"[Captcha] round #{attempt}: 真通过（iframe 消失或 URL 已跳离）")
                return True

            # 等 access-challenge 出现 AND 启用（disabled 状态点了也没用）
            enabled = False
            for _retry in range(30):
                if _hard_pass():
                    print(f"[Captcha] round #{attempt}: 等 access-challenge 时已真通过")
                    return True
                try:
                    el = frame2.locator(ACCESS_CHALLENGE).first
                    if el.count() > 0:
                        aria_dis = el.get_attribute('aria-disabled', timeout=800)
                        if aria_dis != 'true':
                            enabled = True
                            break
                except Exception:
                    pass
                page.wait_for_timeout(500)
            if not enabled:
                print(f"[Captcha] round #{attempt}: access-challenge 15s 内没启用（一直 disabled）")
                return False

            loc = frame2.locator(ACCESS_CHALLENGE)
            try:
                box = loc.bounding_box()
            except Exception as _ce:
                print(f"[Captcha] round #{attempt} 取 access-challenge bbox 失败: {type(_ce).__name__}: {_ce}")
                if _hard_pass():
                    return True
                return False
            if not box:
                print(f"[Captcha] round #{attempt}: access-challenge 没有 bbox")
                return False

            x = box['x'] + box['width'] / 2 + random.randint(-10, 10)
            y = box['y'] + box['height'] / 2 + random.randint(-10, 10)
            page.mouse.click(x, y)

            # 等 press-again 出现
            try:
                frame2.locator(PRESS_AGAIN).first.wait_for(state='visible', timeout=8000)
                box2 = frame2.locator(PRESS_AGAIN).bounding_box()
            except Exception as _ce:
                print(f"[Captcha] round #{attempt} 等 press-again 失败: {type(_ce).__name__}")
                if _hard_pass():
                    print(f"[Captcha] round #{attempt}: 但 iframe 已真消失 → 通过")
                    return True
                # 详细现场：dump iframe 内容，判断 Arkose 换成啥挑战了
                try:
                    page.screenshot(path=f'/tmp/captcha_stuck_{attempt}.png', full_page=True)
                    print(f"[Captcha] 截图: /tmp/captcha_stuck_{attempt}.png")
                except Exception:
                    pass
                try:
                    inner_html = frame2.locator('body').inner_html(timeout=3000)[:2000]
                    print(f"[Captcha] 内层 iframe HTML (截前 2000 字符):\n{inner_html}")
                except Exception as _he:
                    print(f"[Captcha] 取不到内层 HTML: {_he}")
                try:
                    txt = frame2.locator('body').inner_text(timeout=3000)[:500]
                    print(f"[Captcha] 内层 iframe 文本: {txt!r}")
                except Exception:
                    pass
                try:
                    btns = frame2.locator('button, [role="button"], [aria-label]').all()
                    print(f"[Captcha] 内层共 {len(btns)} 个可交互元素:")
                    for i, b in enumerate(btns[:15]):
                        try:
                            info = b.evaluate("el => ({tag: el.tagName, label: el.getAttribute('aria-label'), txt: (el.innerText||'').slice(0,40), cls: el.className})")
                            print(f"  [{i}] {info}")
                        except Exception:
                            pass
                except Exception:
                    pass
                return False

            if not box2:
                print(f"[Captcha] round #{attempt}: press-again 无 bbox")
                return False

            import time
            target_x = box2['x'] + box2['width'] / 2 + random.randint(-5, 5)
            target_y = box2['y'] + box2['height'] / 2 + random.randint(-5, 5)

            # 1) 先把鼠标移到按钮附近的随机点（有过程，非瞬移）
            pre_x = target_x + random.randint(-80, 80)
            pre_y = target_y + random.randint(-80, 80)
            page.mouse.move(pre_x, pre_y, steps=random.randint(8, 15))
            page.wait_for_timeout(random.randint(120, 280))

            # 2) 再分段移到目标（多段 step，贴近人类轨迹）
            page.mouse.move(target_x, target_y, steps=random.randint(6, 12))
            page.wait_for_timeout(random.randint(80, 180))

            page.mouse.down()
            print(f"[Captcha] round #{attempt}: mousedown，开始按住...")
            try:
                page.locator('.draw').wait_for(state="detached", timeout=30000)
                print(f"[Captcha] round #{attempt}: .draw 已 detached（按压完成）")
            except Exception as _wd:
                print(f"[Captcha] round #{attempt}: 等 .draw detach 超时: {_wd}")

            # 3) 按压期间每 100-200ms 微抖（真人不可能完全静止）
            #    这一步其实应该在按压过程中执行，但因为 wait_for 是阻塞的，
            #    我们在 detach 后再补一个"延迟松开 + 微抖"来增加非机械感
            for _ in range(random.randint(3, 6)):
                jx = target_x + random.randint(-2, 2)
                jy = target_y + random.randint(-2, 2)
                page.mouse.move(jx, jy)
                page.wait_for_timeout(random.randint(30, 80))

            # 4) 延迟松开 300-900ms（真人反应时间）
            page.wait_for_timeout(random.randint(300, 900))

            page.mouse.up()
            print(f"[Captcha] round #{attempt}: mouseup（人类化：延迟+微抖）")

            # 按完一轮后 poll 结果。三种可能：
            #   a) 页面跳离 signup → 真通过
            #   b) iframe 消失 → 真通过
            #   c) Try again 出现 → 同一轮重试
            #   d) 出现新的 access-challenge / press-again → Arkose 发了下一轮
            #   e) 限速提示 → 失败
            # 延长到 90 秒：Arkose 通过后 Microsoft 后端可能拖很久做异步风控
            completed_seen_at = None
            for tick in range(90):
                page.wait_for_timeout(1000)

                abnormal = (_safe_count(page.get_by_text('一些异常活动'))
                            or _safe_count(page.get_by_text('unusual activity', exact=False)))
                maintenance = (_safe_count(page.get_by_text('此站点正在维护，暂时无法使用，请稍后重试。'))
                               or _safe_count(page.get_by_text('site is temporarily unavailable', exact=False)))
                if abnormal or maintenance:
                    print("[Error: Rate limit] - IP 被限速")
                    return False

                if _hard_pass():
                    print(f"[Captcha] round #{attempt}: tick#{tick} 真通过（iframe 消失或 URL 跳离）")
                    return True

                if (_safe_count(frame1.get_by_text('请再试一次'))
                        or _safe_count(frame1.get_by_text('Try again'))):
                    print(f"[Captcha] round #{attempt}: tick#{tick} 出现 Try again，重试同一轮")
                    break

                # 检测 'Human Challenge 已完成，请稍候' —— Arkose 已通过, 等 Microsoft 处理
                completed = (_safe_count(frame2.get_by_text('已完成', exact=False))
                             or _safe_count(frame2.get_by_text('completed', exact=False))
                             or _safe_count(frame2.get_by_text('Challenge has been completed', exact=False)))
                if completed:
                    if completed_seen_at is None:
                        completed_seen_at = tick
                        print(f"[Captcha] round #{attempt}: tick#{tick} Arkose 已通过, 等 Microsoft 后端处理...")
                    elif (tick - completed_seen_at) % 10 == 0:
                        print(f"[Captcha] round #{attempt}: tick#{tick} Microsoft 已处理 {tick - completed_seen_at}s, 继续等")
                    continue

                # 下一轮挑战已加载？判断标准：access-challenge ENABLED + press-again 可见
                if tick >= 3 and completed_seen_at is None:
                    try:
                        new_access = frame2.locator(ACCESS_CHALLENGE).first
                        is_disabled = (new_access.get_attribute('aria-disabled', timeout=500) == 'true')
                    except Exception:
                        is_disabled = True
                    new_press = _safe_count(frame2.locator(PRESS_AGAIN))
                    if not is_disabled and new_press > 0:
                        print(f"[Captcha] round #{attempt}: tick#{tick} 检测到下一轮 Arkose 挑战，继续按")
                        break
            else:
                # 90 秒没任何信号
                if _hard_pass():
                    return True
                if completed_seen_at is not None:
                    print(f"[Captcha] round #{attempt}: 90s 内 Arkose 已通过但 Microsoft 后端持续不响应, 该 IP 很可能被风控静默拒绝")
                    return False
                print(f"[Captcha] round #{attempt}: 90s 无信号，继续下一轮尝试")

        print(f"[Captcha] 总共 {max_rounds} 轮仍未真通过")
        return False

    def get_thread_page(self):
        browser = self.get_thread_browser()
        if not browser:
            raise RuntimeError("浏览器启动失败，无法创建 page。请检查 chromium 是否已安装 (`patchright install chromium`) 以及代理配置。")
        context = browser.new_context(
            locale="zh-CN",
            timezone_id="America/Anchorage",
            extra_http_headers={"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
        )
        return context.new_page()

    def clean_up(self, page=None, type="all_browser"):
        if type == "done_browser" and page:
            try:
                page.context.close()
            except Exception:
                pass
            browser = getattr(self.thread_local, 'browser', None)
            pw = getattr(self.thread_local, 'playwright', None)
            try:
                if browser:
                    browser.close()
            except Exception:
                pass
            try:
                if pw:
                    pw.stop()
            except Exception:
                pass
            for attr in ('browser', 'playwright'):
                if hasattr(self.thread_local, attr):
                    delattr(self.thread_local, attr)
            with self.cleanup_lock:
                self.active_resources = [
                    (p, b) for (p, b) in self.active_resources
                    if p is not pw and b is not browser
                ]

        elif type == "all_browser":
            for p, b in self.active_resources:
                try:
                    b.close()
                except Exception: pass
                try:
                    p.stop()
                except Exception: pass
