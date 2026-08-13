import os
import re
import time
import json
import random
import secrets
import string
import hashlib
import threading
from faker import Faker
from abc import ABC, abstractmethod
from urllib.parse import urlparse

_VER_HASH = hashlib.md5(b'github.com/ZNaiGaomu/outlook-hotmail-auto').hexdigest()


def build_proxy_settings(proxy_url):
    if not proxy_url:
        return None
    parsed = urlparse(proxy_url)
    if not parsed.hostname:
        return None
    host = parsed.hostname
    if parsed.port:
        host = f"{host}:{parsed.port}"
    scheme = parsed.scheme or "http"
    settings = {"server": f"{scheme}://{host}", "bypass": "localhost"}
    if parsed.username:
        settings["username"] = parsed.username
    if parsed.password:
        settings["password"] = parsed.password
    return settings


def rotate_cliproxy_sid(proxy_url):
    """每次 launch 换新 sid，拿全新出口 IP。"""
    if not proxy_url or 'sid-' not in proxy_url:
        return proxy_url
    new_sid = ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(8))
    return re.sub(r'sid-[A-Za-z0-9]+', f'sid-{new_sid}', proxy_url)


class BaseBrowserController(ABC):
    def __init__(self):
        with open('config.json', 'r', encoding='utf-8') as f:
            data = json.load(f)
        self.wait_time = data['bot_protection_wait'] * 1000
        self.max_captcha_retries = data['max_captcha_retries']
        self.enable_oauth2 = data["oauth2"]['enable_oauth2']
        self.proxy = data['proxy']
        self.email_suffix = data['email_suffix']
        self.thread_local = threading.local()
        self.cleanup_lock = threading.Lock()
        self.active_resources = []
        self.results_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'Results')
        os.makedirs(self.results_dir, exist_ok=True)

    @abstractmethod
    def launch_browser(self): pass
    @abstractmethod
    def handle_captcha(self, page): pass
    @abstractmethod
    def clean_up(self, page=None, type="all_browser"): pass
    @abstractmethod
    def get_thread_page(self): pass

    def get_thread_browser(self):
        if not hasattr(self.thread_local,"browser"):
            p, b  = self.launch_browser()
            if not p:
                return False
            self.thread_local.playwright = p
            self.thread_local.browser = b
            with self.cleanup_lock:
                self.active_resources.append((p, b))
        return self.thread_local.browser

    def outlook_register(self, page, email, password):
        fake = Faker()
        lastname = fake.last_name()
        firstname = fake.first_name()
        year = str(random.randint(1960, 2005))
        month = str(random.randint(1, 12))
        day = str(random.randint(1, 28))

        EN_MONTHS = ['', 'January','February','March','April','May','June',
                     'July','August','September','October','November','December']

        EMAIL_INPUT = (
            'input[aria-label="新建电子邮件"], '
            'input[aria-label="New email"], '
            'input[name="MemberName"], '
            'input[name="Email"], '
            'input[type="email"], '
            'input[id*="Email" i], '
            'input[id*="Member" i]'
        )
        LASTNAME_INPUT = '#lastNameInput, [name="LastName"]'
        FIRSTNAME_INPUT = '#firstNameInput, [name="FirstName"]'
        MAILBOX_READY = '[aria-label="新邮件"], [aria-label="New mail"]'

        try:
            page.goto("https://outlook.live.com/mail/0/?prompt=create_account", timeout=45000, wait_until="domcontentloaded")
            start_time = time.time()

            agree_btn = page.get_by_text('同意并继续').or_(page.get_by_text('Agree and continue'))
            entry_target = agree_btn.or_(page.locator(EMAIL_INPUT))
            entry_target.first.wait_for(timeout=40000)

            if agree_btn.count() > 0:
                page.wait_for_timeout(0.1 * self.wait_time)
                agree_btn.first.click(timeout=10000)
                print("[Info] 已点击 '同意并继续/Agree and continue'（中文流程）")
                page.locator(EMAIL_INPUT).first.wait_for(timeout=30000)
            else:
                print("[Info] 直接进入邮箱输入页（英文流程）")
        except Exception as _e:
            import traceback
            print("[Error: IP] - IP质量不佳，无法进入注册界面。")
            print(f"[Debug] 失败类型: {type(_e).__name__}: {_e}")
            print(f"[Debug] 当前 URL: {page.url}")
            try:
                print(f"[Debug] 页面标题: {page.title()}")
            except Exception:
                pass
            try:
                body_snip = page.locator('body').inner_text(timeout=3000)[:600]
                print(f"[Debug] 页面片段: {body_snip!r}")
            except Exception:
                pass
            try:
                inputs = page.locator('input').all()
                print(f"[Debug] 页面共 {len(inputs)} 个 <input>:")
                for idx, inp in enumerate(inputs[:15]):
                    try:
                        attrs = inp.evaluate(
                            "el => ({type: el.type, name: el.name, id: el.id, "
                            "ariaLabel: el.getAttribute('aria-label'), "
                            "placeholder: el.placeholder})"
                        )
                        print(f"  [{idx}] {attrs}")
                    except Exception:
                        pass
            except Exception:
                pass
            traceback.print_exc()
            return False

        try:
            if self.email_suffix == "@hotmail.com":
                page.get_by_text("@outlook.com").click(timeout=10000)
                page.locator(f'[role="option"]:text-is("@hotmail.com")').click()

            page.locator(EMAIL_INPUT).first.type(email, delay=0.006 * self.wait_time, timeout=10000)
            page.locator('[data-testid="primaryButton"]').click(timeout=5000)
            page.wait_for_timeout(0.02 * self.wait_time)
            page.locator('[type="password"]').type(password, delay=0.004 * self.wait_time, timeout=10000)
            page.wait_for_timeout(0.02 * self.wait_time)
            page.locator('[data-testid="primaryButton"]').click(timeout=5000)

            page.wait_for_timeout(0.03 * self.wait_time)
            page.locator('[name="BirthYear"]').fill(year, timeout=10000)

            try:
                page.wait_for_timeout(0.02 * self.wait_time)
                page.locator('[name="BirthMonth"]').select_option(value=month, timeout=1000)
                page.wait_for_timeout(0.05 * self.wait_time)
                page.locator('[name="BirthDay"]').select_option(value=day)
            except Exception:
                page.locator('[name="BirthMonth"]').click()
                page.wait_for_timeout(0.02 * self.wait_time)
                page.locator(
                    f'[role="option"]:text-is("{month}月"), [role="option"]:text-is("{EN_MONTHS[int(month)]}")'
                ).first.click()
                page.wait_for_timeout(0.04 * self.wait_time)
                page.locator('[name="BirthDay"]').click()
                page.wait_for_timeout(0.03 * self.wait_time)
                page.locator(
                    f'[role="option"]:text-is("{day}日"), [role="option"]:text-is("{day}")'
                ).first.click()
                page.locator('[data-testid="primaryButton"]').click(timeout=5000)

            page.locator(LASTNAME_INPUT).first.type(lastname, delay=0.002 * self.wait_time, timeout=10000)
            page.wait_for_timeout(0.02 * self.wait_time)
            page.locator(FIRSTNAME_INPUT).first.fill(firstname, timeout=10000)

            if time.time() - start_time < self.wait_time / 1000:
                page.wait_for_timeout(self.wait_time - (time.time() - start_time) * 1000)

            page.locator('[data-testid="primaryButton"]').click(timeout=5000)
            page.locator('span > [href="https://go.microsoft.com/fwlink/?LinkID=521839"]').wait_for(state='detached', timeout=22000)
            page.wait_for_timeout(400)

            abnormal = (page.get_by_text('一些异常活动').count()
                        or page.get_by_text('unusual activity', exact=False).count())
            maintenance = (page.get_by_text('此站点正在维护，暂时无法使用，请稍后重试。').count()
                           or page.get_by_text('site is temporarily unavailable', exact=False).count())
            if abnormal or maintenance:
                print("[Error: IP or browser] - 当前IP注册频率过快。")
                return False

            if page.locator('iframe#enforcementFrame').count() > 0:
                print("[Error: FunCaptcha] - 验证码类型错误，非按压验证码。")
                return False

            captcha_result = self.handle_captcha(page)
            if not captcha_result:
                raise TimeoutError

            # 真相验证：注册真正成功的硬信号是 URL 离开 signup.live.com
            # 仅凭"验证码挑战消失"容易被 Arkose 多轮子挑战骗过（假成功）
            print("[Verify] 等待页面跳离 signup.live.com 以确认注册真实成功...")
            try:
                page.wait_for_url(lambda url: 'signup.live.com' not in url and 'signup.' not in url.split('//',1)[-1].split('/',1)[0], timeout=45000)
                print(f"[Verify] ✅ 已跳离 signup, 当前 URL: {page.url[:120]}")
            except Exception:
                print(f"[Error: FakeSuccess] - 验证码流程结束但页面仍停在 signup, 账号未真正创建")
                print(f"[Debug] 当前 URL: {page.url}")
                try:
                    page.screenshot(path='/tmp/fake_success.png', full_page=True)
                    print('[Debug] 截图: /tmp/fake_success.png')
                except Exception:
                    pass
                try:
                    body = page.locator('body').inner_text(timeout=3000)[:800]
                    print(f'[Debug] 页面文本: {body!r}')
                except Exception:
                    pass
                # 再看看是否出现了 Arkose 的二轮挑战 iframe
                try:
                    n = page.locator('iframe').count()
                    print(f'[Debug] 当前仍有 {n} 个 iframe')
                except Exception:
                    pass
                return False

        except Exception as _e:
            import traceback
            print("[Error: IP] - 加载超时或因触发机器人检测导致按压次数达到最大仍未通过。")
            print(f"[Debug] 失败类型: {type(_e).__name__}: {_e}")
            traceback.print_exc()
            return False

        filename = os.path.join(self.results_dir, 'logged_email.txt' if self.enable_oauth2 else 'unlogged_email.txt')
        with open(filename, 'a', encoding='utf-8') as f:
            f.write(f"{email}{self.email_suffix}: {password}\n")
        print(f'[Success: Email Registration] - {email}{self.email_suffix}: {password}')

        if not self.enable_oauth2:
            return True

        # 注册完之后，OAuth2 只需要 Microsoft 的会话 cookie（注册成功时已种好）。
        # 不必死等邮箱完全初始化（新号初始化很慢），只需跳过常见中间页即可。
        page.wait_for_timeout(3000)

        # 可能出现的 "保持登录? / Stay signed in?" 等中间提示：点"否/No/Not now"跳过
        for txt in ('否', '暂不', 'No', 'Not now', 'No, thanks'):
            try:
                btn = page.get_by_role('button', name=txt)
                if btn.count() > 0:
                    btn.first.click(timeout=2000)
                    print(f"[Info] 跳过中间提示按钮 '{txt}'")
                    page.wait_for_timeout(1500)
                    break
            except Exception:
                pass

        # 尽量等一下邮箱就绪（成功最好，超时也不阻塞 OAuth2）
        try:
            page.locator(MAILBOX_READY).first.wait_for(timeout=20000)
            print('[Info] 邮箱已初始化')
        except Exception:
            print('[Warn] 邮箱未在 20s 内完全加载，继续尝试 OAuth2 拿 token')
        return True
