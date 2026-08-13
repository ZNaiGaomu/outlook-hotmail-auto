import os
import random
import shutil
import tempfile
import time
import uuid

from patchright.sync_api import sync_playwright

from .base_controller import (
    BaseBrowserController,
    BROWSER_CONTEXT_KW,
    BROWSER_LAUNCH_ARGS,
    apply_browser_context_guards,
    build_proxy_settings,
    focus_browser_window,
    install_webrtc_navigation_guard,
    rotate_cliproxy_sid,
)


class PatchrightController(BaseBrowserController):

    def launch_browser(self):
        """
        启动有头 Chromium。
        incognito=True（默认）：每次用全新临时 user_data_dir，等价于强无痕
        （无历史 Cookie / 本地存储 / 缓存），更接近「无痕窗口」。
        """
        try:
            p = sync_playwright().start()

            rotated = rotate_cliproxy_sid(self.proxy) if self.proxy else None
            if rotated and rotated != self.proxy:
                try:
                    sid = rotated.split('sid-')[1].split('-')[0].split('@')[0]
                    print(f"[Proxy] 本次使用 sticky session: sid-{sid}")
                except Exception:
                    pass
            proxy_settings = build_proxy_settings(rotated)
            if proxy_settings:
                print(f"[Proxy] 使用代理: {proxy_settings.get('server')}")
            else:
                print("[Proxy] 未使用代理（本机/机房流量）- use_residential 可能为 false")

            args = list(BROWSER_LAUNCH_ARGS)
            # 无痕模式下再加隐私向参数
            if self.incognito:
                for extra in (
                    "--disable-features=TranslateUI,IsolateOrigins,site-per-process",
                    "--disable-notifications",
                    "--disable-default-apps",
                    "--no-first-run",
                    "--no-default-browser-check",
                ):
                    if extra not in args:
                        args.append(extra)

            if self.incognito:
                # 每次独立临时配置目录 = 强无痕会话
                profile_dir = tempfile.mkdtemp(prefix=f"outlook_incog_{uuid.uuid4().hex[:8]}_")
                print(f"[UI] 无痕模式: 临时用户目录 {profile_dir}")
                context = p.chromium.launch_persistent_context(
                    user_data_dir=profile_dir,
                    headless=False,
                    args=args,
                    proxy=proxy_settings,
                    ignore_default_args=["--enable-automation"],
                    **BROWSER_CONTEXT_KW,
                )
                apply_browser_context_guards(context)
                # 用 context 冒充 browser 接口（close/new_page/new_context 兼容层）
                browser = _PersistentBrowserAdapter(context, profile_dir)
                for delay in (0.4, 1.0, 1.8):
                    try:
                        time.sleep(delay)
                    except Exception:
                        pass
                    n = focus_browser_window()
                    if n:
                        break
                print("[UI] 有头浏览器已启动（headless=False / persistent）。")
                return p, browser

            b = p.chromium.launch(
                headless=False,
                args=args,
                proxy=proxy_settings,
                ignore_default_args=["--enable-automation"],
            )
            for delay in (0.5, 1.2, 2.0):
                try:
                    time.sleep(delay)
                except Exception:
                    pass
                n = focus_browser_window()
                if n:
                    break
                print("[UI] 有头浏览器已启动（headless=False）。")
            return p, b

        except Exception as e:
            print(f"启动浏览器失败: {e}")
            return False, False

    def handle_captcha(self, page):
        """
        恢复原先「按住直到进度走完」的按压逻辑：
          mousedown → 等 .draw detached（最长 30s）→ 微抖 → mouseup
        本波最多 4 次；中途小故障用 continue 进入下一次，不提前整波 return False。
        上层仍负责：4 次不过 → 刷新整页再 4 次。
        """
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

        def _still_on_robot_challenge() -> bool:
            """页面是否仍停在『证明你不是机器人』挑战（iframe 短暂为 0 不算过）。"""
            try:
                url = (page.url or "").lower()
            except Exception:
                url = ""
            if "signup.live.com" not in url and "signup." not in url:
                return False
            markers = [
                page.get_by_text("证明你不是机器人", exact=False),
                page.get_by_text("Prove you are not a robot", exact=False),
                page.get_by_text("按住", exact=True),
                page.get_by_text("Press and hold", exact=False),
                page.get_by_text("再次按下", exact=False),
                page.get_by_text("Press again", exact=False),
                page.get_by_text("长按该按钮", exact=False),
                page.locator(CHALLENGE_IFRAME),
                page.locator('iframe[src*="hsprotect"]'),
                page.locator('iframe[src*="arkoselabs"], iframe[src*="funcaptcha"]'),
            ]
            for m in markers:
                if _safe_count(m) > 0:
                    return True
            try:
                title = (page.title() or "")
                if "机器人" in title or "robot" in title.lower():
                    return True
            except Exception:
                pass
            return False

        def _hard_pass():
            """
            真通过硬信号（必须严格）：
            1) URL 已离开 signup.*  -> 通过
            2) 仍在 signup 时：仅 iframe 短暂 count=0 不算过；必须挑战文案/按钮也消失
            """
            try:
                url = (page.url or "").lower()
            except Exception:
                url = ""
            if url and ("signup.live.com" not in url) and not url.split("//", 1)[-1].split("/", 1)[0].startswith("signup."):
                return True
            if _still_on_robot_challenge():
                return False
            if _safe_count(page.locator(CHALLENGE_IFRAME)) == 0 and _safe_count(page.locator('iframe[src*="hsprotect"]')) == 0:
                return True
            return False

        # 单轮注册内：人机验证最多按压 4 次（由上层决定是否刷新重来第二轮）
        max_rounds = 4

        for attempt in range(max_rounds):
            page.wait_for_timeout(300)
            print(f"[Captcha] === 第 {attempt + 1}/{max_rounds} 次按压（须按住直到进度走完）===")

            if _hard_pass():
                print(f"[Captcha] round #{attempt}: 真通过（iframe 消失或 URL 已跳离）")
                return True

            # 等 access-challenge 可点；超时不整波退出，计入本轮后 continue 下一次
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
                print(f"[Captcha] round #{attempt}: access-challenge 15s 内没启用 → 计入本轮，继续下一次按")
                page.wait_for_timeout(500)
                continue

            loc = frame2.locator(ACCESS_CHALLENGE)
            try:
                box = loc.bounding_box()
            except Exception as _ce:
                print(f"[Captcha] round #{attempt} 取 access-challenge bbox 失败: {type(_ce).__name__}: {_ce}")
                if _hard_pass():
                    return True
                continue
            if not box:
                print(f"[Captcha] round #{attempt}: access-challenge 没有 bbox → 继续下一次")
                continue

            x = box['x'] + box['width'] / 2 + random.randint(-10, 10)
            y = box['y'] + box['height'] / 2 + random.randint(-10, 10)
            page.mouse.click(x, y)

            try:
                frame2.locator(PRESS_AGAIN).first.wait_for(state='visible', timeout=8000)
                box2 = frame2.locator(PRESS_AGAIN).bounding_box()
            except Exception as _ce:
                print(f"[Captcha] round #{attempt} 等 press-again 失败: {type(_ce).__name__}")
                if _hard_pass():
                    print(f"[Captcha] round #{attempt}: 但 iframe 已真消失 -> 通过")
                    return True
                # 原先这里 return False；改为 continue，保证能按满 4 次
                print(f"[Captcha] round #{attempt}: 无 press-again → 计入本轮，继续下一次按")
                page.wait_for_timeout(600)
                continue

            if not box2:
                print(f"[Captcha] round #{attempt}: press-again 无 bbox → 继续下一次")
                continue

            target_x = box2['x'] + box2['width'] / 2 + random.randint(-5, 5)
            target_y = box2['y'] + box2['height'] / 2 + random.randint(-5, 5)

            pre_x = target_x + random.randint(-80, 80)
            pre_y = target_y + random.randint(-80, 80)
            page.mouse.move(pre_x, pre_y, steps=random.randint(8, 15))
            page.wait_for_timeout(random.randint(120, 280))

            page.mouse.move(target_x, target_y, steps=random.randint(6, 12))
            page.wait_for_timeout(random.randint(80, 180))

            # ===== 关键：按住不松，直到进度条走完（.draw detached）=====
            page.mouse.down()
            print(f"[Captcha] round #{attempt}: mousedown，开始按住直到进度满…")
            draw_done = False
            try:
                page.locator('.draw').wait_for(state="detached", timeout=30000)
                draw_done = True
                print(f"[Captcha] round #{attempt}: .draw 已 detached（按压进度已满）")
            except Exception as _wd:
                print(f"[Captcha] round #{attempt}: 等 .draw detach 超时(30s): {_wd}")
                # 超时仍保持按住过，再补一段再松（避免完全没按满）
                page.wait_for_timeout(random.randint(1500, 2500))

            for _ in range(random.randint(3, 6)):
                jx = target_x + random.randint(-2, 2)
                jy = target_y + random.randint(-2, 2)
                page.mouse.move(jx, jy)
                page.wait_for_timeout(random.randint(30, 80))

            page.wait_for_timeout(random.randint(300, 900))

            page.mouse.up()
            print(
                f"[Captcha] round #{attempt}: mouseup"
                + ("（进度已满后松开）" if draw_done else "（超时后松开）")
            )

            completed_seen_at = None
            for tick in range(60):
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
                    print(f"[Captcha] round #{attempt}: tick#{tick} 出现 Try again，进入下一次按压")
                    break

                completed = (_safe_count(frame2.get_by_text('已完成', exact=False))
                             or _safe_count(frame2.get_by_text('completed', exact=False))
                             or _safe_count(frame2.get_by_text('Challenge has been completed', exact=False)))
                if completed:
                    if completed_seen_at is None:
                        completed_seen_at = tick
                        print(f"[Captcha] round #{attempt}: tick#{tick} Arkose 已通过, 等 Microsoft 后端处理...")
                    elif (tick - completed_seen_at) % 10 == 0:
                        print(f"[Captcha] round #{attempt}: tick#{tick} Microsoft 已处理 {tick - completed_seen_at}s, 继续等")
                    # 完成后最多再等 20s
                    if tick - completed_seen_at >= 20:
                        print(f"[Captcha] round #{attempt}: 完成后 20s 未跳转 → 下一次按压")
                        break
                    continue

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
                if _hard_pass():
                    return True
                if completed_seen_at is not None:
                    print(f"[Captcha] round #{attempt}: Arkose 已通过但 Microsoft 未跳转 → 下一次按压")
                else:
                    print(f"[Captcha] round #{attempt}: 本轮等待结束，继续下一次按压")

        print(f"[Captcha] 总共 {max_rounds} 轮仍未真通过")
        return False

    def get_thread_page(self):
        browser = self.get_thread_browser()
        if not browser:
            raise RuntimeError("浏览器启动失败，无法创建 page。请检查 chromium 是否已安装 (`patchright install chromium`) 以及代理配置。")

        # 无痕 persistent context：已有 context，直接取/建 page
        if isinstance(browser, _PersistentBrowserAdapter):
            page = browser.new_page()
            install_webrtc_navigation_guard(page)
            try:
                focus_browser_window(page)
                page.bring_to_front()
            except Exception:
                pass
            print("[UI] 无痕有头浏览器已启动。若仍看不见，请看任务栏 Chromium 图标。")
            return page

        context = browser.new_context(**BROWSER_CONTEXT_KW)
        apply_browser_context_guards(context)
        page = context.new_page()
        install_webrtc_navigation_guard(page)
        try:
            focus_browser_window(page)
            page.bring_to_front()
        except Exception:
            pass
        print("[UI] 有头浏览器已启动（headless=False）。若仍看不见，请看任务栏 Chromium/Chrome 图标并点击。")
        return page

    def clean_up(self, page=None, type="all_browser"):
        if type == "done_browser" and page:
            browser = getattr(self.thread_local, 'browser', None)
            pw = getattr(self.thread_local, 'playwright', None)
            profile_dir = None
            try:
                if isinstance(browser, _PersistentBrowserAdapter):
                    profile_dir = browser.profile_dir
                    browser.close()
                else:
                    try:
                        page.context.close()
                    except Exception:
                        pass
                    if browser:
                        browser.close()
            except Exception:
                pass
            try:
                if pw:
                    pw.stop()
            except Exception:
                pass
            if profile_dir:
                try:
                    shutil.rmtree(profile_dir, ignore_errors=True)
                    print(f"[UI] 已清理无痕临时目录")
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
            for p, b in list(self.active_resources):
                try:
                    if isinstance(b, _PersistentBrowserAdapter):
                        profile_dir = b.profile_dir
                        b.close()
                        shutil.rmtree(profile_dir, ignore_errors=True)
                    else:
                        b.close()
                except Exception:
                    pass
                try:
                    p.stop()
                except Exception:
                    pass
            self.active_resources = []


class _PersistentBrowserAdapter:
    """把 BrowserContext 包装成接近 Browser 的对象，便于现有 clean_up/get_thread 逻辑复用。"""

    def __init__(self, context, profile_dir: str):
        self._context = context
        self.profile_dir = profile_dir

    def new_context(self, **kwargs):
        # persistent 模式已有 context，忽略再 new
        return self._context

    def new_page(self):
        # 复用已有空白页，或新建
        try:
            pages = self._context.pages
            if pages:
                # 用第一个空白/已有页
                return pages[0]
        except Exception:
            pass
        return self._context.new_page()

    def close(self):
        try:
            self._context.close()
        except Exception:
            pass

    @property
    def contexts(self):
        return [self._context]
