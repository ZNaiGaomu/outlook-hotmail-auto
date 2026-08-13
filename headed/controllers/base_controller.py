import os
import re
import sys
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


def safe_print(*args, **kwargs) -> None:
    """Windows GBK 控制台下避免 [OK] 等字符把成功路径打成 UnicodeEncodeError。"""
    try:
        print(*args, **kwargs)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        text = " ".join(str(a) for a in args)
        try:
            sys.stdout.write(text.encode(enc, errors="replace").decode(enc, errors="replace") + "\n")
        except Exception:
            sys.stdout.write(text.encode("ascii", errors="replace").decode("ascii") + "\n")
        try:
            sys.stdout.flush()
        except Exception:
            pass

_VER_HASH = hashlib.md5(b'github.com/ZNaiGaomu/outlook-hotmail-auto').hexdigest()

# 浏览器启动参数：有头可见 + 限制 WebRTC 走非代理 UDP
BROWSER_LAUNCH_ARGS = [
    "--lang=zh-CN",
    "--start-maximized",
    "--window-position=80,40",
    "--window-size=1280,900",
    "--disable-backgrounding-occluded-windows",
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
    "--enforce-webrtc-ip-permission-check",
    "--disable-webrtc-multiple-routes",
    "--disable-webrtc-hw-decoding",
    "--disable-webrtc-hw-encoding",
    # 禁 WebRTC 泄漏 + 禁密码保存气泡（会挡住 OAuth「接受」）
    "--disable-features=WebRtcHideLocalIpsWithMdns,WebRTC,PasswordManagerOnboarding,PasswordImport,PasswordLeakDetection",
    "--password-store=basic",
    "--disable-save-password-bubble",
    "--disable-password-generation",
    "--disable-password-manager-reauthentication",
]

# 与日本住宅出口对齐的 context 默认值
BROWSER_CONTEXT_KW = {
    "locale": "zh-CN",
    "timezone_id": "Asia/Tokyo",
    "extra_http_headers": {"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
}

# 页面级强拦截：锁死 RTCPeerConnection，防止 STUN 打出真实公网
WEBRTC_GUARD_INIT_SCRIPT = r"""
(() => {
  if (window.__OUTLOOK_WEBRTC_GUARD__ === 'on') return;
  const err = () => new DOMException('WebRTC is disabled', 'NotSupportedError');
  const deny = () => { throw err(); };
  const denyAsync = () => Promise.reject(err());

  class BlockedRTC {
    constructor() { deny(); }
    static generateCertificate() { return denyAsync(); }
  }
  const handler = {
    construct() { deny(); },
    apply() { deny(); },
    get(target, prop) {
      if (prop === 'prototype') return BlockedRTC.prototype;
      if (prop === 'generateCertificate') return () => denyAsync();
      if (prop === 'toString') return () => 'function RTCPeerConnection() { [native code] }';
      if (prop === Symbol.toStringTag) return 'Function';
      return target[prop];
    }
  };
  const blocked = new Proxy(BlockedRTC, handler);

  const lock = (root, key) => {
    try { delete root[key]; } catch (e) {}
    try {
      Object.defineProperty(root, key, {
        configurable: false,
        enumerable: true,
        get() { return blocked; },
        set() { /* swallow */ },
      });
      return true;
    } catch (e1) {
      try { root[key] = blocked; return true; } catch (e2) { return false; }
    }
  };

  const roots = [window];
  try { if (typeof globalThis !== 'undefined') roots.push(globalThis); } catch (e) {}
  const keys = [
    'RTCPeerConnection',
    'webkitRTCPeerConnection',
    'mozRTCPeerConnection',
    'RTCSessionDescription',
    'RTCIceCandidate',
    'RTCDataChannel',
  ];
  const locked = [];
  for (const root of roots) {
    for (const key of keys) {
      if (lock(root, key)) locked.push(key);
    }
  }

  try {
    if (navigator.mediaDevices) {
      navigator.mediaDevices.getUserMedia = () => denyAsync();
      navigator.mediaDevices.getDisplayMedia = () => denyAsync();
      navigator.mediaDevices.enumerateDevices = () => Promise.resolve([]);
    }
  } catch (e) {}
  try { navigator.getUserMedia = deny; } catch (e) {}
  try { navigator.webkitGetUserMedia = deny; } catch (e) {}

  window.__OUTLOOK_WEBRTC_GUARD__ = 'on';
  window.__OUTLOOK_WEBRTC_LOCKED__ = locked;
})();
"""


def apply_browser_context_guards(context):
    """给 BrowserContext 打上 WebRTC 防护 init script（部分环境可能无效，需配合 harden）。"""
    context.add_init_script(WEBRTC_GUARD_INIT_SCRIPT)
    return context


def harden_page_webrtc(page) -> None:
    """
    双保险：
    1) CDP 再注册一份 NewDocument 脚本
    2) 对当前文档立刻执行一次拦截
    patchright 下 init script 常在导航后丢失，故必须在导航后重放。
    """
    try:
        session = page.context.new_cdp_session(page)
        session.send(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": WEBRTC_GUARD_INIT_SCRIPT},
        )
    except Exception:
        pass
    try:
        page.evaluate(WEBRTC_GUARD_INIT_SCRIPT)
    except Exception:
        pass


def focus_browser_window(page=None) -> int:
    """只前置本工具启动的 Chromium，不碰其它 Chrome / 系统窗口。

    优先用 Playwright page 对应的进程；没有 page 时只匹配 outlook_incog_ 配置目录。
    """
    if sys.platform != "win32":
        if page is not None:
            try:
                page.bring_to_front()
            except Exception:
                pass
        return 0

    pids: set[int] = set()

    # 1) 从 Playwright 页面拿到本工具 chromium 的 PID
    if page is not None:
        try:
            page.bring_to_front()
        except Exception:
            pass
        try:
            ctx = page.context
            browser = getattr(ctx, "browser", None)
            proc = None
            if browser is not None:
                proc = getattr(browser, "process", None)
            if proc is None:
                proc = getattr(ctx, "browser", None)
                proc = getattr(proc, "process", None) if proc is not None else None
            # patchright persistent context 可能把 process 挂在 browser 上
            if proc is not None and getattr(proc, "pid", None):
                pids.add(int(proc.pid))
        except Exception:
            pass
        # CDP 再取一层 Browser 进程
        try:
            cdp = page.context.new_cdp_session(page)
            info = cdp.send("SystemInfo.getProcessInfo") if False else None
            del info
        except Exception:
            pass
        try:
            # 用 Windows 标题 + 我们刚 bring_to_front 的前台窗：仍限制到我们的 PID
            pass
        except Exception:
            pass

    # 2) 扫描进程：只认 patchright/playwright + outlook_incog_ / ms-playwright
    try:
        import subprocess as _sp

        r = _sp.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                (
                    "Get-CimInstance Win32_Process | Where-Object { "
                    "$_.Name -match '^(chrome|chromium)\\.exe$' -and $_.CommandLine -and ("
                    "$_.CommandLine -match 'outlook_incog_|ms-playwright|patchright|playwright-'"
                    ") } | Select-Object -ExpandProperty ProcessId"
                ),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
        )
        for line in (r.stdout or "").splitlines():
            line = line.strip()
            if line.isdigit():
                pids.add(int(line))
    except Exception:
        pass

    if not pids:
        safe_print("[UI] 未找到本工具 Chromium 进程（不前置其它窗口）")
        return 0

    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        SW_RESTORE = 9
        SW_SHOW = 5
        targets: list[int] = []
        pid_buf = wintypes.DWORD()

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        def _enum(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            pid_buf.value = 0
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid_buf))
            if int(pid_buf.value) not in pids:
                return True
            # 只要有标题或 chrome 主窗 class，避免托盘鬼窗
            length = user32.GetWindowTextLengthW(hwnd)
            cls = ctypes.create_unicode_buffer(256)
            try:
                user32.GetClassNameW(hwnd, cls, 256)
            except Exception:
                cls.value = ""
            clow = (cls.value or "").lower()
            if length <= 0 and clow not in ("chrome_widgetwin_1",):
                return True
            targets.append(int(hwnd))
            return True

        user32.EnumWindows(_enum, 0)
        if not targets:
            safe_print(f"[UI] 找到本工具进程 {sorted(pids)}，但还没有可见窗口")
            return 0

        fg = user32.GetForegroundWindow()
        fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
        cur_tid = kernel32.GetCurrentThreadId()
        raised = 0
        for hwnd in targets[:3]:
            try:
                if fg_tid and cur_tid and fg_tid != cur_tid:
                    user32.AttachThreadInput(cur_tid, fg_tid, True)
                if user32.IsIconic(hwnd):
                    user32.ShowWindow(hwnd, SW_RESTORE)
                else:
                    user32.ShowWindow(hwnd, SW_SHOW)
                user32.BringWindowToTop(hwnd)
                user32.SetForegroundWindow(hwnd)
                if fg_tid and cur_tid and fg_tid != cur_tid:
                    user32.AttachThreadInput(cur_tid, fg_tid, False)
                raised += 1
            except Exception:
                continue
        if raised:
            safe_print(f"[UI] 已前置本工具 Chromium 窗口 {raised} 个（pid={sorted(pids)[:4]}）")
        return raised
    except Exception as e:
        safe_print(f"[UI] 前置窗口失败: {e}")
        return 0


def install_webrtc_navigation_guard(page) -> None:
    """
    页面级 WebRTC 防护：立即 harden，并在主框导航后自动重放。
    patchright 不会稳定保留 add_init_script，所以用事件兜底。
    """
    harden_page_webrtc(page)

    def _on_frame_navigated(frame) -> None:
        try:
            if frame != page.main_frame:
                return
        except Exception:
            return
        # 稍后再注入，避免文档未就绪
        try:
            page.wait_for_timeout(50)
        except Exception:
            pass
        harden_page_webrtc(page)

    try:
        page.on("framenavigated", _on_frame_navigated)
    except Exception:
        pass
    # 标记，避免重复挂多个监听
    try:
        page._outlook_webrtc_guard_installed = True  # type: ignore[attr-defined]
    except Exception:
        pass


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
        # use_residential=false 时 config 会把 proxy 置空 -> 本机直连
        self.proxy = (data.get('proxy') or '').strip() or None
        self.use_residential = bool(data.get('use_residential', True))
        # 无痕：每次全新临时用户目录，不继承 Cookie/本地存储/缓存
        self.incognito = bool(data.get('incognito', True))
        self.email_suffix = data['email_suffix']
        self.thread_local = threading.local()
        self.cleanup_lock = threading.Lock()
        self.active_resources = []
        self.results_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'Results')
        os.makedirs(self.results_dir, exist_ok=True)

    def _progress(self, job_id, step, detail=""):
        if not job_id:
            return
        try:
            from progress_bus import report
            report(job_id, step, detail)
        except Exception:
            pass

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

    def _click_primary(self, page, timeout: int = 12000) -> None:
        """点「下一步/继续」主按钮：多选择器 + 加长超时，避免表格异常。"""
        selectors = [
            '[data-testid="primaryButton"]',
            'button[type="submit"]',
            'input[type="submit"]',
            'button:has-text("下一步")',
            'button:has-text("Next")',
            'button:has-text("继续")',
            'button:has-text("Continue")',
        ]
        last_err: Exception | None = None
        for sel in selectors:
            try:
                loc = page.locator(sel)
                if loc.count() == 0:
                    continue
                el = loc.first
                try:
                    el.scroll_into_view_if_needed(timeout=2000)
                except Exception:
                    pass
                el.click(timeout=timeout)
                return
            except Exception as e:
                last_err = e
                continue
        # 角色名兜底
        for name in ("下一步", "Next", "继续", "Continue"):
            try:
                btn = page.get_by_role("button", name=name)
                if btn.count() > 0:
                    btn.first.click(timeout=timeout)
                    return
            except Exception as e:
                last_err = e
        if last_err:
            raise last_err
        raise TimeoutError("找不到下一步/主按钮")

    def _email_domain_dropdown_visible(self, page) -> bool:
        """当前页是否出现右侧 @outlook.com / @hotmail.com 下拉。"""
        selectors = (
            '[role="button"]:has-text("@outlook.")',
            '[role="button"]:has-text("@hotmail.")',
            '[role="combobox"]:has-text("@")',
            'button:has-text("@outlook.")',
            'button:has-text("@hotmail.")',
            '[aria-haspopup="listbox"]:has-text("@")',
        )
        for sel in selectors:
            try:
                loc = page.locator(sel)
                if loc.count() > 0 and loc.first.is_visible():
                    return True
            except Exception:
                continue
        return False

    def _email_format_error_visible(self, page) -> bool:
        try:
            err = page.locator("#usernameError, [id*='usernameError'], [id*='Error'], [role='alert']")
            n = err.count()
            for i in range(min(n, 4)):
                el = err.nth(i)
                if not el.is_visible():
                    continue
                txt = (el.inner_text(timeout=600) or "")
                if "someone@example.com" in txt or "格式" in txt:
                    return True
        except Exception:
            return False
        return False

    def _set_email_box(self, box, value: str, delay: int) -> str:
        """清空后填入 value，返回读回的 input_value。"""
        try:
            box.click(timeout=4000)
        except Exception:
            pass
        try:
            box.fill("")
        except Exception:
            try:
                box.press("Control+A")
                box.press("Backspace")
            except Exception:
                pass
        try:
            box.fill(value)
        except Exception:
            box.type(value, delay=delay, timeout=20000)
        try:
            return (box.input_value(timeout=2000) or "").strip()
        except Exception:
            return ""

    def _fill_email_and_next(self, page, email: str, EMAIL_INPUT: str, job_id=None) -> None:
        """自适应两种微软邮箱页：

        - 有右侧域名下拉：左边只填用户名；若误带了 @outlook.com 就删掉后缀
        - 没有下拉：填完整 user@outlook.com，然后直接下一步
        """
        suffix = (self.email_suffix or "@outlook.com").strip()
        if not suffix.startswith("@"):
            suffix = "@" + suffix
        local = (email or "").strip()
        if "@" in local:
            local = local.split("@", 1)[0].strip()
        if not local:
            raise RuntimeError("邮箱本地部分为空")
        full = f"{local}{suffix}"
        delay = max(30, int(0.008 * self.wait_time))

        box = page.locator(EMAIL_INPUT).first
        box.wait_for(state="visible", timeout=15000)

        has_dropdown = self._email_domain_dropdown_visible(page)
        if has_dropdown:
            # 需要切 hotmail 时点下拉
            if suffix.lower() == "@hotmail.com":
                try:
                    page.locator('[role="button"]:has-text("@"), button:has-text("@")').first.click(timeout=3000)
                    page.locator('[role="option"]:text-is("@hotmail.com")').click(timeout=3000)
                except Exception:
                    pass
            want = local
            self._progress(job_id, "fill_email", f"下拉域名已出现，只填用户名 {local}")
        else:
            want = full
            self._progress(job_id, "fill_email", f"无域名下拉，填完整地址 {full}")

        actual = self._set_email_box(box, want, delay)

        # 填完后再看一眼：有的页面是填完才弹出下拉
        page.wait_for_timeout(350)
        if (not has_dropdown) and self._email_domain_dropdown_visible(page):
            has_dropdown = True
            want = local
            self._progress(job_id, "fill_email", "填写后出现域名下拉，去掉后缀只留用户名")
            actual = self._set_email_box(box, local, delay)

        # 有下拉却带了 @xxx：删掉后缀
        if has_dropdown and "@" in (actual or ""):
            actual = self._set_email_box(box, local, delay)

        # 无下拉但只填了用户名：补完整地址
        if (not has_dropdown) and "@" not in (actual or ""):
            actual = self._set_email_box(box, full, delay)

        # 核对最终值
        got_local = (actual or "").split("@", 1)[0]
        if has_dropdown:
            if got_local != local or "@" in (actual or ""):
                actual = self._set_email_box(box, local, delay)
                got_local = (actual or "").split("@", 1)[0]
            if got_local != local:
                raise RuntimeError(f"用户名未填对: value={actual!r} expected={local!r}")
        else:
            if "@" not in (actual or "") or got_local != local:
                actual = self._set_email_box(box, full, delay)
            if "@" not in (actual or ""):
                raise RuntimeError(f"完整邮箱未填上: value={actual!r} expected={full!r}")

        # 等格式错误消失；若仍报格式，按当前布局再纠一次
        page.wait_for_timeout(400)
        for _ in range(5):
            if not self._email_format_error_visible(page):
                break
            if self._email_domain_dropdown_visible(page):
                actual = self._set_email_box(box, local, delay)
            else:
                actual = self._set_email_box(box, full, delay)
            page.wait_for_timeout(350)

        self._click_primary(page)
        page.wait_for_timeout(max(400, int(0.02 * self.wait_time)))

    def _fill_signup_form(self, page, email, password, job_id, firstname, lastname, year, month, day, EN_MONTHS,
                          EMAIL_INPUT, LASTNAME_INPUT, FIRSTNAME_INPUT, start_time):
        """从邮箱填写到点最终提交（不含验证码）。成功返回 True。"""
        try:
            self._fill_email_and_next(page, email, EMAIL_INPUT, job_id)

            self._progress(job_id, "fill_password", "设置密码")
            page.locator('[type="password"]').type(password, delay=0.004 * self.wait_time, timeout=10000)
            page.wait_for_timeout(0.02 * self.wait_time)
            self._click_primary(page)

            self._progress(job_id, "fill_birthday", f"生日 {year}-{month}-{day}")
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
                ).first.click(timeout=8000)
                page.wait_for_timeout(0.04 * self.wait_time)
                page.locator('[name="BirthDay"]').click()
                page.wait_for_timeout(0.03 * self.wait_time)
                try:
                    page.locator(
                        f'[role="option"]:text-is("{day}日"), [role="option"]:text-is("{day}")'
                    ).first.click(timeout=8000)
                except Exception:
                    page.keyboard.type(str(day))
                    page.keyboard.press("Enter")
                self._click_primary(page)

            self._progress(job_id, "fill_name", f"{firstname} {lastname}")
            page.locator(LASTNAME_INPUT).first.type(lastname, delay=0.002 * self.wait_time, timeout=10000)
            page.wait_for_timeout(0.02 * self.wait_time)
            page.locator(FIRSTNAME_INPUT).first.fill(firstname, timeout=10000)

            if time.time() - start_time < self.wait_time / 1000:
                page.wait_for_timeout(self.wait_time - (time.time() - start_time) * 1000)

            self._click_primary(page)
            try:
                page.locator('span > [href="https://go.microsoft.com/fwlink/?LinkID=521839"]').wait_for(
                    state='detached', timeout=22000
                )
            except Exception:
                page.wait_for_timeout(1500)
            page.wait_for_timeout(400)

            abnormal = (page.get_by_text('一些异常活动').count()
                        or page.get_by_text('unusual activity', exact=False).count())
            maintenance = (page.get_by_text('此站点正在维护，暂时无法使用，请稍后重试。').count()
                           or page.get_by_text('site is temporarily unavailable', exact=False).count())
            if abnormal or maintenance:
                safe_print("[Error: IP or browser] - 当前IP注册频率过快。")
                self._progress(job_id, "captcha", "失败原因: IP/浏览器触发「异常活动」或站点维护，频率过快")
                self._set_fail_kind(job_id, "captcha")
                return False

            if page.locator('iframe#enforcementFrame').count() > 0:
                safe_print("[Error: FunCaptcha] - 验证码类型错误，非按压验证码。")
                self._progress(job_id, "captcha", "失败原因: 出现 FunCaptcha（非按压），当前脚本不支持")
                self._set_fail_kind(job_id, "captcha")
                return False
            return True
        except Exception as e:
            msg = str(e)
            if len(msg) > 100:
                msg = msg[:100] + "…"
            self._progress(job_id, "fill_email", f"失败原因: 填表未完成（页面未就绪或按钮超时）")
            self._set_fail_kind(job_id, "form")
            safe_print(f"[Form] 填表失败: {type(e).__name__}: {msg}")
            return False

    def _set_fail_kind(self, job_id, kind: str) -> None:
        if not job_id:
            return
        try:
            from progress_bus import BUS

            BUS.set_fail_kind(job_id, kind)
        except Exception:
            pass

    def _run_captcha_up_to_4(self, page, job_id, wave: int) -> bool:
        """
        人机验证一波：内部真实按满最多 4 次。
        wave=1 第一波；失败后由 outlook_register 刷新整页再 wave=2 再 4 次。
        """
        self._progress(job_id, "captcha", f"人机验证第 {wave} 波（须真实按满最多 4 次，不中途放弃）")
        safe_print(f"[Captcha] === 第 {wave} 波：真实按压最多 4 次（中途失败继续按，不空等退出）===")
        ok = self.handle_captcha(page)
        if ok:
            still = self._still_robot_page(page)
            if not still:
                self._progress(job_id, "captcha", f"第 {wave} 波人机验证通过")
                return True
            self._progress(job_id, "captcha", f"第 {wave} 波按满后仍在挑战页")
            return False
        self._progress(job_id, "captcha", f"第 {wave} 波 4 次按压均未通过")
        return False

    def _page_text_snip(self, page, n: int = 1200) -> str:
        try:
            return (page.locator("body").inner_text(timeout=2500) or "")[:n]
        except Exception:
            return ""

    def _is_post_signup_success_page(self, page) -> bool:
        """
        人机通过后的「已建号」中间页（即使 URL 仍带 signup 也算成功）。
        典型文案：我们来保护你的账户 / Let's protect your account / 添加安全信息 等。
        """
        try:
            url = (page.url or "").lower()
        except Exception:
            url = ""
        text = ""
        try:
            title = page.title() or ""
        except Exception:
            title = ""
        text = f"{title}\n{self._page_text_snip(page)}"
        low = text.lower()

        success_markers = [
            "我们来保护你的账户",
            "我们来保护你的帐户",
            "保护你的账户",
            "保护你的帐户",
            "let's protect your account",
            "lets protect your account",
            "protect your account",
            "帮助我们保护你的账户",
            "添加安全信息",
            "添加电话号码",
            "证明你拥有",
            "保持登录",
            "stay signed in",
            "looks good",
            "一切正常",
            "创建成功",
            "account created",
            "you're all set",
            "你已完成",
            "跳过一段时间",
            "skip for now",
            "稍后",
        ]
        for m in success_markers:
            if m.lower() in low:
                return True

        # URL 形态：account.live.com / account.microsoft.com / privacynotice 等
        url_markers = (
            "account.live.com",
            "account.microsoft.com",
            "privacynotice",
            "account.microsoft",
            "login.live.com/oauth",
            "outlook.live.com",
            "office.com",
        )
        if any(u in url for u in url_markers):
            # 排除仍明确是机器人挑战
            if "证明你不是机器人" not in text and "prove you are not a robot" not in low:
                return True
        return False

    def _still_robot_page(self, page) -> bool:
        # 已进入保护账户等成功中间页，绝不能当机器人挑战
        if self._is_post_signup_success_page(page):
            return False
        try:
            t = page.title() or ""
            if "机器人" in t or "robot" in t.lower():
                # 「保护」页标题有时不含 robot；若同时有保护文案上面已 return False
                if "保护" not in t and "protect" not in t.lower():
                    return True
            if page.get_by_text("证明你不是机器人", exact=False).count() > 0:
                return True
            if page.get_by_text("Prove you are not a robot", exact=False).count() > 0:
                return True
            # 仅「按住/再次按下」且仍有 hsprotect iframe 才算挑战中
            has_press = (
                page.get_by_text("按住", exact=True).count() > 0
                or page.get_by_text("再次按下", exact=False).count() > 0
                or page.get_by_text("Press and hold", exact=False).count() > 0
            )
            has_arkose = (
                page.locator('iframe[src*="hsprotect"]').count() > 0
                or page.locator(
                    'iframe[title="验证质询"], iframe[title="Verification challenge"]'
                ).count()
                > 0
            )
            if has_press and has_arkose:
                return True
            if "signup.live.com" in (page.url or "") and has_arkose and "保护" not in self._page_text_snip(page, 400):
                return True
        except Exception:
            pass
        return False

    def _dismiss_browser_save_password(self, page) -> None:
        """关掉 Chromium「要保存密码吗」气泡（页面内按钮 + 原生窗）。"""
        for lab in ("一律不", "不用了", "Never", "Not now", "No thanks", "不要", "取消"):
            try:
                btn = page.get_by_role("button", name=lab)
                if btn.count() == 0:
                    btn = page.get_by_text(lab, exact=False)
                if btn.count() > 0 and btn.first.is_visible():
                    btn.first.click(timeout=1500)
                    page.wait_for_timeout(300)
                    safe_print(f"[UI] 关闭密码保存提示: {lab}")
                    return
            except Exception:
                pass
        # 原生 infobar / 独立小窗：按标题点「一律不」
        if sys.platform == "win32":
            try:
                self._click_native_button(
                    ("要保存密码吗", "保存密码", "Save password"),
                    ("一律不", "不用了", "Never", "Not now"),
                )
            except Exception:
                pass
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass

    def _click_native_button(self, title_keys: tuple[str, ...], btn_keys: tuple[str, ...]) -> bool:
        """点 Windows 原生弹窗按钮（Chromium 保存密码条不在 DOM 里）。"""
        if sys.platform != "win32":
            return False
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            found: list[int] = []

            @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
            def _enum(hwnd, _lp):
                if not user32.IsWindowVisible(hwnd):
                    return True
                n = user32.GetWindowTextLengthW(hwnd)
                if n <= 0:
                    return True
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                title = buf.value or ""
                if any(k.lower() in title.lower() for k in title_keys):
                    found.append(int(hwnd))
                return True

            user32.EnumWindows(_enum, 0)
            BM_CLICK = 0x00F5
            for hwnd in found[:3]:
                child_hit: list[int] = []

                @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
                def _enum_child(ch, _lp):
                    n = user32.GetWindowTextLengthW(ch)
                    if n <= 0:
                        return True
                    buf = ctypes.create_unicode_buffer(n + 1)
                    user32.GetWindowTextW(ch, buf, n + 1)
                    t = buf.value or ""
                    if any(k.lower() == t.lower() or k in t for k in btn_keys):
                        child_hit.append(int(ch))
                    return True

                user32.EnumChildWindows(hwnd, _enum_child, 0)
                for ch in child_hit[:1]:
                    user32.SendMessageW(ch, BM_CLICK, 0, 0)
                    safe_print(f"[UI] 已点原生弹窗按钮 hwnd={ch}")
                    return True
        except Exception as e:
            safe_print(f"[UI] 原生弹窗点击失败: {e}")
        return False

    def _handle_stay_signed_in(self, page, job_id=None) -> bool:
        """「是否保持登录状态」选否。返回是否点到。"""
        try:
            url = (page.url or "").lower()
        except Exception:
            url = ""
        snip = self._page_text_snip(page, 800)
        low = f"{url}\n{snip}".lower()
        looks = (
            "保持登录" in snip
            or "stay signed in" in low
            or "kmsi" in url
            or "keepmesignedin" in url.replace("_", "")
        )
        if not looks:
            return False
        self._progress(job_id, "kmsi", "保持登录状态 → 否")
        self._dismiss_browser_save_password(page)
        # 优先点「否」，不要点「是」
        for lab in ("否", "不", "No", "No thanks", "Not now", "暂不"):
            try:
                btn = page.get_by_role("button", name=lab)
                if btn.count() == 0:
                    btn = page.locator(f'button:has-text("{lab}"), input[type="button"][value="{lab}"]')
                if btn.count() == 0:
                    btn = page.get_by_text(lab, exact=True)
                if btn.count() > 0 and btn.first.is_visible():
                    btn.first.click(timeout=4000)
                    page.wait_for_timeout(800)
                    safe_print(f"[KMSI] 保持登录 → {lab}")
                    return True
            except Exception:
                continue
        # id / data 兜底
        for sel in (
            '#idBtn_Back',
            'input[id*="Back"]',
            'button[id*="decline"]',
            '[data-testid="secondaryButton"]',
        ):
            try:
                loc = page.locator(sel)
                if loc.count() > 0 and loc.first.is_visible():
                    loc.first.click(timeout=3000)
                    page.wait_for_timeout(800)
                    safe_print(f"[KMSI] 保持登录 → {sel}")
                    return True
            except Exception:
                continue
        return False

    def _is_add_email_gate_page(self, page) -> bool:
        """
        少数账号人机后的中间页：只有大按钮「添加电子邮件」，尚无输入框。
        例：帮助保护你的帐户 + 添加电子邮件（URL 常含 interrupt/credentialaction）。
        仅作防护识别，未出现时不得误点。
        """
        try:
            url = (page.url or "").lower()
        except Exception:
            url = ""
        snip = self._page_text_snip(page, 900)
        title = ""
        try:
            title = page.title() or ""
        except Exception:
            pass
        blob = f"{title}\n{snip}"

        # 已有邮箱输入框 → 不是 gate，是填写页
        try:
            if page.locator(
                'input[type="email"], input[name*="Email" i], input[id*="Email" i], '
                'input[placeholder*="电子邮件"], input[placeholder*="email" i], '
                'input[placeholder*="@" i]'
            ).count() > 0:
                return False
        except Exception:
            pass

        has_add_btn = False
        for name in ("添加电子邮件", "Add email", "Add an email", "添加电子邮件地址"):
            try:
                if page.get_by_role("button", name=name).count() > 0:
                    has_add_btn = True
                    break
                if page.get_by_text(name, exact=True).count() > 0:
                    # 文本存在再确认可点
                    has_add_btn = True
                    break
            except Exception:
                continue

        if not has_add_btn:
            return False

        gate_hints = (
            "帮助保护你的帐户" in blob
            or "帮助保护你的账户" in blob
            or "保护你的帐户" in blob
            or "保护你的账户" in blob
            or "添加电子邮件地址" in blob
            or "添加电子邮件" in blob
            or "Add an email" in blob
            or "help protect" in blob.lower()
            or "credentialaction" in url
            or "interrupt" in url
        )
        return bool(gate_hints)

    def _click_add_email_gate_if_present(self, page, job_id=None) -> bool:
        """
        防护：仅当出现「添加电子邮件」中间页时点击，进入填写备用邮箱页。
        未出现则立即返回 False，不干扰主流程。
        返回 True 表示已点击并尽量等到输入框出现。
        """
        if not self._is_add_email_gate_page(page):
            return False

        self._dismiss_browser_save_password(page)
        self._progress(job_id, "verify", "检测到「添加电子邮件」中间页，点击进入…")
        safe_print("[Proof] gate page: 添加电子邮件 → click")

        clicked = False
        for name in ("添加电子邮件", "Add email", "Add an email", "添加电子邮件地址"):
            try:
                btn = page.get_by_role("button", name=name)
                if btn.count() == 0:
                    btn = page.locator(f'button:has-text("{name}"), a:has-text("{name}")')
                if btn.count() == 0:
                    continue
                el = btn.first
                try:
                    el.scroll_into_view_if_needed(timeout=2000)
                except Exception:
                    pass
                try:
                    el.click(timeout=5000, force=True)
                except Exception:
                    try:
                        el.evaluate("el => el.click()")
                    except Exception:
                        continue
                clicked = True
                safe_print(f"[Proof] clicked gate button: {name}")
                break
            except Exception as e:
                safe_print(f"[Proof] gate click {name}: {type(e).__name__}")
                continue

        if not clicked:
            safe_print("[Proof] gate page detected but click failed")
            return False

        # 等待进入「添加电子邮件地址」填写页（有 input）
        for _ in range(25):
            page.wait_for_timeout(400)
            self._dismiss_browser_save_password(page)
            try:
                if page.locator(
                    'input[type="email"], input[name*="Email" i], input[id*="Email" i], '
                    'input[placeholder*="电子邮件"], input[placeholder*="email" i], '
                    'input[placeholder*="@" i]'
                ).count() > 0:
                    self._progress(job_id, "verify", "已进入备用邮箱填写页")
                    safe_print("[Proof] gate → email input page ready")
                    return True
            except Exception:
                pass
            # 若已到 proofs/Add 也算成功进入
            try:
                u = (page.url or "").lower()
                if "proofs" in u:
                    return True
            except Exception:
                pass
        safe_print("[Proof] gate clicked but email input not seen yet (continue anyway)")
        return True

    def _is_recovery_email_required_page(self, page) -> bool:
        """
        「让我们来保护你的帐户」且要求填写备用邮箱。
        关键：URL 含 proofs/Add 时一律视为强制备用邮箱（不可跳过）。
        也覆盖「添加电子邮件地址」填写页（已有输入框）。
        """
        try:
            url = (page.url or "").lower()
        except Exception:
            url = ""
        # URL 最可靠
        if "proofs/add" in url or "/proofs/" in url:
            return True

        snip = self._page_text_snip(page, 1200)
        low = snip.lower()
        title = ""
        try:
            title = page.title() or ""
        except Exception:
            pass
        blob = f"{title}\n{snip}"

        # 有邮箱输入框时：保护/添加邮件相关文案 → 填写页
        has_email_box = False
        try:
            has_email_box = page.locator(
                'input[type="email"], input[name*="Email" i], input[id*="Email" i], '
                'input[placeholder*="example.com" i], input[placeholder*="电子邮件"], '
                'input[placeholder*="email" i], input[placeholder*="@"]'
            ).count() > 0
        except Exception:
            has_email_box = False

        if has_email_box and (
            "添加电子邮件" in blob
            or "电子邮件地址" in blob
            or "add an email" in low
            or "email address" in low
            or "保护" in blob
            or "protect" in low
            or "credentialaction" in url
            or "interrupt" in url
        ):
            return True

        protect = (
            "让我们来保护你的账户" in blob
            or "让我们来保护你的帐户" in blob
            or "保护你的账户" in blob
            or "保护你的帐户" in blob
            or "protect your account" in low
            or "let's protect your account" in low
            or "帮助保护你的帐户" in blob
            or "帮助保护你的账户" in blob
        )
        if not protect:
            return False

        if has_email_box or "必填" in snip or "someone@example.com" in low or "备用" in snip:
            return True
        # 保护页 + 下一步 也优先当强制（很多布局没有 Skip）
        try:
            if page.get_by_role("button", name="下一步").count() > 0 or page.get_by_text("下一步", exact=True).count() > 0:
                return True
        except Exception:
            pass
        return False

    def _handle_recovery_email_proof(self, page, job_id=None) -> bool:
        """
        处理 account.live.com/proofs/Add 备用邮箱强制验证：
        1) 按 config.cf_mail 创建临时邮箱（失败回退 fallback_domain）
        2) 填入并点下一步
        3) 轮询 CF 收验证码
        4) 填码提交
        """
        # 先关密码气泡
        self._dismiss_browser_save_password(page)

        if not self._is_recovery_email_required_page(page):
            # 再根据 URL 强判一次
            try:
                if "proofs" not in ((page.url or "").lower()):
                    return True
            except Exception:
                return True

        self._progress(job_id, "verify", "保护账户需备用邮箱，正在创建 CF 临时邮箱…")
        safe_print("[Proof] 强制备用邮箱页 -> 开始 CF 流程")

        try:
            from cf_temp_mail import create_recovery_mailbox
        except Exception as e:
            safe_print(f"[Proof] 无法导入 cf_temp_mail: {e}")
            self._progress(job_id, "verify", f"失败原因: 未加载 CF 邮箱模块 ({e})")
            return False

        try:
            recovery_addr, client = create_recovery_mailbox()
        except Exception as e:
            safe_print(f"[Proof] 创建 CF 邮箱失败: {e}")
            self._progress(
                job_id,
                "verify",
                f"失败原因: 创建备用邮箱失败: {str(e)[:140]}",
            )
            return False

        self._progress(job_id, "verify", f"备用邮箱已创建: {recovery_addr}，正在填入…")
        safe_print(f"[Proof] recovery email = {recovery_addr}")

        # 填邮箱（多种选择器 + 键盘）
        try:
            self._dismiss_browser_save_password(page)
            page.wait_for_timeout(500)
            candidates = [
                'input[type="email"]',
                'input[name*="Email" i]',
                'input[id*="Email" i]',
                'input[placeholder*="example.com" i]',
                'input[placeholder*="电子邮件"]',
                'input[placeholder*="email" i]',
                'input[placeholder*="@" i]',
                'input[aria-label*="邮件"]',
                'input[aria-label*="email" i]',
                'input:not([type="hidden"]):not([type="checkbox"]):not([type="password"])',
            ]
            filled = False
            for sel in candidates:
                try:
                    loc = page.locator(sel).first
                    if loc.count() == 0:
                        continue
                    loc.wait_for(state="visible", timeout=5000)
                    loc.click(timeout=3000)
                    # 全选清除占位
                    try:
                        page.keyboard.press("Control+A")
                        page.keyboard.press("Backspace")
                    except Exception:
                        pass
                    try:
                        loc.fill(recovery_addr, timeout=5000)
                    except Exception:
                        loc.type(recovery_addr, delay=25, timeout=10000)
                    # 校验是否写入
                    try:
                        val = loc.input_value(timeout=2000)
                    except Exception:
                        val = ""
                    if recovery_addr.split("@")[0] in (val or "") or "@" in (val or ""):
                        filled = True
                        safe_print(f"[Proof] filled via {sel}: {val}")
                        break
                except Exception as e:
                    safe_print(f"[Proof] try {sel} fail: {type(e).__name__}")
                    continue
            if not filled:
                # 最后手段：Tab 到输入框
                try:
                    page.keyboard.press("Tab")
                    page.keyboard.type(recovery_addr, delay=25)
                    filled = True
                except Exception:
                    pass
            if not filled:
                self._progress(job_id, "verify", "失败原因: 找不到/无法填写备用邮箱输入框")
                return False

            # 填写页主按钮可能是「添加电子邮件」而不只是「下一步」
            page.wait_for_timeout(600)
            self._dismiss_browser_save_password(page)

            clicked = False
            for lab in (
                "添加电子邮件",
                "Add email",
                "下一步",
                "Next",
                "继续",
                "Continue",
                "发送代码",
                "Send code",
                "发送",
            ):
                try:
                    btn = page.get_by_role("button", name=lab)
                    if btn.count() == 0:
                        btn = page.locator(f'input[type="submit"][value*="{lab}"]')
                    if btn.count() == 0:
                        btn = page.get_by_text(lab, exact=True)
                    if btn.count() > 0:
                        btn.first.click(timeout=5000)
                        clicked = True
                        safe_print(f"[Proof] clicked {lab}")
                        break
                except Exception:
                    pass
            if not clicked:
                page.keyboard.press("Enter")
                safe_print("[Proof] fallback Enter")
            page.wait_for_timeout(2500)
            self._dismiss_browser_save_password(page)
        except Exception as e:
            safe_print(f"[Proof] 填写备用邮箱失败: {e}")
            self._progress(job_id, "recovery", f"失败原因: 填写备用邮箱失败 {type(e).__name__}: {str(e)[:80]}")
            return False

        # 收验证码
        self._progress(job_id, "recovery", f"已提交 {recovery_addr}，等待 CF 收安全代码…")
        otp = None
        try:
            otp = client.wait_otp(timeout_sec=180, poll_interval=4.0)
        except Exception as e:
            safe_print(f"[Proof] 收信异常: {e}")
        if not otp:
            self._progress(job_id, "recovery", "失败原因: CF 邮箱未收到微软安全代码（180s 超时）")
            return False

        self._progress(job_id, "recovery", "已收到安全代码，正在填入…")
        try:
            self._dismiss_browser_save_password(page)
            code_sels = [
                'input[name*="otc" i]',
                'input[name*="code" i]',
                'input[id*="code" i]',
                'input[id*="otc" i]',
                'input[aria-label*="代码"]',
                'input[aria-label*="code" i]',
                'input[type="tel"]',
                'input[inputmode="numeric"]',
                'input[maxlength="6"]',
                'input[maxlength="7"]',
                'input[maxlength="8"]',
            ]
            code_ok = False
            for sel in code_sels:
                try:
                    loc = page.locator(sel).first
                    if loc.count() == 0:
                        continue
                    loc.wait_for(state="visible", timeout=8000)
                    loc.click(timeout=3000)
                    loc.fill(otp, timeout=8000)
                    code_ok = True
                    safe_print(f"[Proof] code filled via {sel}")
                    break
                except Exception:
                    continue
            if not code_ok:
                page.keyboard.type(otp, delay=40)
            page.wait_for_timeout(400)
            for lab in ("下一步", "Next", "验证", "Verify", "继续", "Continue", "提交", "Submit"):
                try:
                    btn = page.get_by_role("button", name=lab)
                    if btn.count() == 0:
                        btn = page.get_by_text(lab, exact=True)
                    if btn.count() > 0:
                        btn.first.click(timeout=5000)
                        safe_print(f"[Proof] submit code via {lab}")
                        break
                except Exception:
                    pass
            page.wait_for_timeout(3000)
        except Exception as e:
            safe_print(f"[Proof] 提交验证码失败: {e}")
            self._progress(job_id, "verify", f"失败原因: 提交安全代码失败 {type(e).__name__}")
            return False

        try:
            self._dismiss_post_signup_interstitials(page, job_id)
        except Exception:
            pass

        # 若仍在 proofs/Add 且还有空邮箱框，算失败
        try:
            if "proofs/add" in ((page.url or "").lower()):
                # 可能进入验证码后的错误态
                snip = self._page_text_snip(page, 200)
                if "必填" in snip or "example.com" in snip.lower():
                    self._progress(job_id, "verify", f"失败原因: 备用邮箱验证后仍在保护页")
                    return False
        except Exception:
            pass

        self._progress(job_id, "verify", "备用邮箱验证通过，继续进入邮箱…")
        safe_print("[Proof] recovery email verification done")
        return True

    def _dismiss_post_signup_interstitials(self, page, job_id=None) -> None:
        """人机后中间页：辅助邮箱 → 保持登录选否 → 再点跳过/继续。不管系统保存密码气泡。"""
        try:
            if self._is_recovery_email_required_page(page):
                ok = self._handle_recovery_email_proof(page, job_id)
                if not ok:
                    safe_print("[Proof] 备用邮箱流程未完成")
                return
        except Exception as e:
            safe_print(f"[Proof] 检测/处理备用邮箱异常: {e}")

        if self._handle_stay_signed_in(page, job_id):
            return

        # 不要点「是」——避免误点保持登录
        click_labels = [
            "跳过一段时间",
            "暂时跳过",
            "暂时跳过此步骤",
            "跳过",
            "稍后",
            "Skip for now",
            "Skip",
            "Not now",
            "否",
            "暂不",
            "No",
            "No, thanks",
            "Next",
            "下一步",
            "继续",
            "Continue",
            "OK",
            "好的",
            "看起来不错",
            "Looks good",
        ]
        for _ in range(8):
            try:
                if self._is_recovery_email_required_page(page):
                    self._handle_recovery_email_proof(page, job_id)
                    return
            except Exception:
                pass
            if self._handle_stay_signed_in(page, job_id):
                page.wait_for_timeout(800)
                continue
            moved = False
            for lab in click_labels:
                try:
                    btn = page.get_by_role("button", name=lab)
                    if btn.count() == 0:
                        btn = page.get_by_role("link", name=lab)
                    if btn.count() == 0:
                        btn = page.get_by_text(lab, exact=True)
                    if btn.count() == 0:
                        continue
                    if lab in ("下一步", "Next", "继续", "Continue") and self._is_recovery_email_required_page(page):
                        continue
                    if not btn.first.is_visible():
                        continue
                    btn.first.click(timeout=2500)
                    safe_print(f"[Info] 中间页点击: {lab}")
                    page.wait_for_timeout(1200)
                    moved = True
                    break
                except Exception:
                    pass
            if not moved:
                break
            try:
                u = (page.url or "").lower()
                if "outlook.live.com/mail" in u or "/mail/" in u:
                    break
            except Exception:
                pass

    def _open_signup_entry(self, page, job_id, EMAIL_INPUT) -> bool:
        """打开注册入口：多 URL 重试，避免单一 outlook 入口超时/空响应。"""
        urls = [
            "https://signup.live.com/signup?lic=1",
            "https://signup.live.com/?lic=1",
            "https://outlook.live.com/mail/0/?prompt=create_account",
            "https://login.live.com/oauth20_authorize.srf?client_id=0000000048170EF2&scope=openid+offline_access&response_type=code&redirect_uri=https%3A%2F%2Flogin.live.com%2Foauth20_desktop.srf&cobrandid=90015",
        ]
        last_err: Exception | None = None
        self._progress(job_id, "open_signup", "打开 outlook/signup 注册入口")

        for i, url in enumerate(urls):
            try:
                safe_print(f"[Signup] try#{i + 1}/{len(urls)} {url[:70]}")
                page.goto(url, timeout=60000, wait_until="domcontentloaded")
                page.wait_for_timeout(800)

                agree_btn = page.get_by_text("同意并继续").or_(
                    page.get_by_text("Agree and continue")
                )
                entry_target = agree_btn.or_(page.locator(EMAIL_INPUT))
                try:
                    entry_target.first.wait_for(timeout=35000)
                except Exception:
                    # 有的页先到别的中间态，再等邮箱框
                    page.wait_for_timeout(1500)
                    if page.locator(EMAIL_INPUT).count() == 0 and agree_btn.count() == 0:
                        raise TimeoutError("未出现同意按钮或邮箱输入框")

                if agree_btn.count() > 0:
                    self._progress(job_id, "agree", "点击同意并继续")
                    page.wait_for_timeout(0.1 * self.wait_time)
                    try:
                        agree_btn.first.click(timeout=10000)
                    except Exception:
                        agree_btn.first.click(timeout=10000, force=True)
                    safe_print("[Info] 已点击 '同意并继续/Agree and continue'")
                    page.locator(EMAIL_INPUT).first.wait_for(timeout=35000)
                else:
                    self._progress(job_id, "agree", "无同意页，直接进入")
                    safe_print("[Info] 直接进入邮箱输入页")
                    if page.locator(EMAIL_INPUT).count() == 0:
                        # 可能还在跳转
                        page.locator(EMAIL_INPUT).first.wait_for(timeout=20000)
                return True
            except Exception as _e:
                last_err = _e
                safe_print(
                    f"[Signup] try#{i + 1} 失败: {type(_e).__name__}: {str(_e)[:100]}"
                )
                try:
                    safe_print(f"[Signup] URL now: {page.url}")
                except Exception:
                    pass
                page.wait_for_timeout(1000)
                continue

        safe_print("[Error: IP/Net] - 无法进入注册界面（多入口均失败）")
        err_s = f"{type(last_err).__name__}: {last_err}" if last_err else "unknown"
        self._progress(
            job_id,
            "open_signup",
            f"失败原因: 无法进入注册页 ({err_s[:120]})",
        )
        return False

    def outlook_register(self, page, email, password, job_id=None):
        fake = Faker()
        lastname = fake.last_name()
        firstname = fake.first_name()
        year = str(random.randint(1960, 2005))
        month = str(random.randint(1, 12))
        day = str(random.randint(1, 28))

        EN_MONTHS = ['', 'January', 'February', 'March', 'April', 'May', 'June',
                     'July', 'August', 'September', 'October', 'November', 'December']

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

        # ---------- 第 1 波：打开 + 填表 + 人机最多 4 次 ----------
        if not self._open_signup_entry(page, job_id, EMAIL_INPUT):
            return False

        start_time = time.time()
        try:
            if not self._fill_signup_form(
                page, email, password, job_id, firstname, lastname, year, month, day,
                EN_MONTHS, EMAIL_INPUT, LASTNAME_INPUT, FIRSTNAME_INPUT, start_time,
            ):
                return False
        except Exception as _e:
            import traceback
            safe_print(f"[Error] 填表失败: {type(_e).__name__}: {_e}")
            traceback.print_exc()
            self._progress(job_id, "fill_email", "失败原因: 填表未完成（页面未就绪或按钮超时）")
            self._set_fail_kind(job_id, "form")
            return False

        captcha_ok = self._run_captcha_up_to_4(page, job_id, wave=1)
        # 人机函数可能因 URL 仍在 signup 返回 False，但页面已是「保护你的账户」= 已建号
        if not captcha_ok and self._is_post_signup_success_page(page):
            safe_print("[Captcha] 第1波返回未通过，但已出现账户保护页 -> 视为成功，跳过刷新重走")
            self._progress(job_id, "captcha", "人机后已进入账户保护页（视为通过）")
            captcha_ok = True

        # ---------- 第 1 波失败：刷新页面，整段重走注册，再人机 4 次 ----------
        if not captcha_ok:
            self._progress(job_id, "captcha", "第1波4次未过 -> 刷新页面并重新注册，再试4次")
            safe_print("[Captcha] 第1波失败：刷新页面，重新走注册流程后再按 4 次")
            try:
                page.reload(timeout=45000, wait_until="domcontentloaded")
            except Exception:
                pass
            page.wait_for_timeout(1500)

            # 重新打开入口（更干净）
            if not self._open_signup_entry(page, job_id, EMAIL_INPUT):
                self._progress(job_id, "captcha", "失败原因: 刷新后无法重新进入注册页（人机两波均未过）")
                self._set_fail_kind(job_id, "captcha")
                return False

            # 换一组姓名/生日，降低同画像连撞；邮箱密码保持同一任务
            lastname = fake.last_name()
            firstname = fake.first_name()
            year = str(random.randint(1960, 2005))
            month = str(random.randint(1, 12))
            day = str(random.randint(1, 28))
            start_time = time.time()
            try:
                if not self._fill_signup_form(
                    page, email, password, job_id, firstname, lastname, year, month, day,
                    EN_MONTHS, EMAIL_INPUT, LASTNAME_INPUT, FIRSTNAME_INPUT, start_time,
                ):
                    # 填表函数内部已标 form；若已是人机相关则保留
                    self._progress(job_id, "captcha", "失败原因: 刷新重填后未进入人机（第1波已失败）")
                    self._set_fail_kind(job_id, "captcha")
                    return False
            except Exception as _e:
                self._progress(job_id, "captcha", "失败原因: 刷新后重填未完成")
                self._set_fail_kind(job_id, "form")
                return False

            captcha_ok = self._run_captcha_up_to_4(page, job_id, wave=2)
            if not captcha_ok and self._is_post_signup_success_page(page):
                safe_print("[Captcha] 第2波返回未通过，但已出现账户保护页 -> 视为成功")
                self._progress(job_id, "captcha", "第2波后已进入账户保护页（视为通过）")
                captcha_ok = True
            if not captcha_ok:
                self._progress(
                    job_id,
                    "captcha",
                    "失败原因: 人机验证两波均未通过（每波最多按4次，含刷新重走）",
                )
                self._set_fail_kind(job_id, "captcha")
                safe_print("[Captcha] 两波共最多 8 次按压仍未通过 -> 返回错误")
                return False

        # ---------- 人机通过后：等到真正进入账户/邮箱，再保存，最后才允许关浏览器 ----------
        # 用户要求：验证过去后，等账户登录进去再退出；那时才算成功并保存。
        MAILBOX_READY = (
            '[aria-label="新邮件"], [aria-label="New mail"], '
            '[aria-label="写邮件"], [aria-label="Compose"], '
            '[data-app-section="Navigation"], '
            '#app, [role="main"]'
        )
        try:
            self._progress(job_id, "verify", "人机已过，等待进入账户/邮箱...")
            safe_print("[Verify] 人机通过后，等待进入账户保护页或 Outlook 邮箱...")
            page.wait_for_timeout(1500)

            def _left_signup(url: str) -> bool:
                if not url:
                    return False
                u = url.lower()
                if "signup.live.com" in u:
                    return False
                try:
                    host = u.split("//", 1)[-1].split("/", 1)[0]
                except Exception:
                    host = u
                return not host.startswith("signup.")

            def _in_mailbox(url: str) -> bool:
                u = (url or "").lower()
                return any(
                    x in u
                    for x in (
                        "outlook.live.com/mail",
                        "outlook.office.com/mail",
                        "outlook.office365.com/mail",
                        "/mail/",
                    )
                )

            logged_in = False
            last_url = ""
            stage = ""

            # 最多等约 90 秒：保护页 -> 点跳过/下一步 -> 邮箱
            for tick in range(90):
                try:
                    last_url = page.url or ""
                except Exception:
                    last_url = last_url or ""

                # 1) 仍在机器人挑战 -> 继续等（或最终失败）
                if self._still_robot_page(page):
                    if tick % 5 == 0:
                        safe_print(f"[Verify] tick#{tick} 仍在人机挑战...")
                    page.wait_for_timeout(1000)
                    continue

                # 2) 账户保护 / 强制备用邮箱 / 安全中间页
                # URL 含 proofs/Add 时必须走 CF 填邮箱，不能只「点到邮箱」
                try:
                    cur_u = (page.url or "").lower()
                except Exception:
                    cur_u = ""

                # 2a) 防护：少数账号先出「添加电子邮件」大按钮中间页 → 点进去再填
                #     未出现则 _click_add_email_gate_if_present 立即 False，不干扰主路径
                try:
                    if self._is_add_email_gate_page(page):
                        stage = "add-email-gate"
                        self._click_add_email_gate_if_present(page, job_id)
                        page.wait_for_timeout(800)
                        # 点完后通常进入填写页，下一轮循环会走 recovery
                        continue
                except Exception as _ge:
                    safe_print(f"[Verify] add-email gate: {_ge}")

                if (
                    "proofs/add" in cur_u
                    or "/proofs/" in cur_u
                    or "credentialaction" in cur_u
                    or self._is_recovery_email_required_page(page)
                ):
                    stage = "recovery-email"
                    self._progress(job_id, "recovery", "检测到备用邮箱保护页，填写并收验证码")
                    safe_print(f"[Verify] recovery/proofs -> CF flow | {cur_u[:120]}")
                    ok_proof = self._handle_recovery_email_proof(page, job_id)
                    if not ok_proof:
                        self._progress(job_id, "recovery", "失败原因: 备用邮箱/安全代码未完成")
                        return False
                    page.wait_for_timeout(1000)
                    continue

                if self._is_post_signup_success_page(page) or (
                    "保护" in self._page_text_snip(page, 300)
                    or "protect" in self._page_text_snip(page, 300).lower()
                    or "添加电子邮件" in self._page_text_snip(page, 300)
                ):
                    # 再次防护：中间页若是「添加电子邮件」gate
                    if self._is_add_email_gate_page(page):
                        self._click_add_email_gate_if_present(page, job_id)
                        page.wait_for_timeout(800)
                        continue
                    # 若其实是强制邮箱填写页
                    if self._is_recovery_email_required_page(page):
                        ok_proof = self._handle_recovery_email_proof(page, job_id)
                        if not ok_proof:
                            return False
                        continue
                    stage = "interstitial"
                    if tick % 3 == 0:
                        safe_print(f"[Verify] tick#{tick} 账户保护/安全中间页，尝试继续... URL={last_url[:100]}")
                        self._progress(job_id, "kmsi", "账户保护/保持登录页，选否并继续…")
                    try:
                        self._dismiss_post_signup_interstitials(page, job_id)
                    except Exception as _ie:
                        safe_print(f"[Warn] 中间页点击: {_ie}")
                    page.wait_for_timeout(1000)
                    if _in_mailbox(last_url):
                        logged_in = True
                        stage = "mailbox"
                        break
                    continue

                # 3) 已在邮箱 URL / 邮箱 UI
                if _in_mailbox(last_url):
                    logged_in = True
                    stage = "mailbox"
                    safe_print(f"[Verify] [OK] 已进入 Outlook 邮箱 URL: {last_url[:160]}")
                    break

                try:
                    if page.locator(MAILBOX_READY).count() > 0:
                        # 避免 signup 残留误伤：要求不在机器人页
                        if not self._still_robot_page(page):
                            logged_in = True
                            stage = "mailbox-ui"
                            safe_print(f"[Verify] [OK] 检测到邮箱 UI | URL={last_url[:120]}")
                            break
                except Exception:
                    pass

                # 4) 已离开 signup 且不是挑战（account/login 完成态）
                if _left_signup(last_url) and not self._still_robot_page(page):
                    stage = "left-signup"
                    if tick % 5 == 0:
                        safe_print(f"[Verify] tick#{tick} 已离 signup，继续等到邮箱... {last_url[:100]}")
                        self._progress(job_id, "verify", f"已离注册页，等待收件箱... {last_url[:80]}")
                    # 再尝试点常见按钮
                    try:
                        self._dismiss_post_signup_interstitials(page, job_id)
                    except Exception:
                        pass
                    page.wait_for_timeout(1000)
                    continue

                if tick % 5 == 0:
                    snip = self._page_text_snip(page, 80)
                    safe_print(f"[Verify] tick#{tick} 等待登录完成... URL={last_url[:90]} snip={snip!r}")
                page.wait_for_timeout(1000)

            # 收尾判定：
            # - 进了邮箱 = 完全成功
            # - 到了保护账户/已离 signup 且不是机器人 = 建号成功（按你的场景也应保存）
            # - 仍在机器人页 = 失败
            try:
                last_url = page.url or last_url
            except Exception:
                pass

            fully_in_mail = logged_in or _in_mailbox(last_url)
            built_account = fully_in_mail or self._is_post_signup_success_page(page) or (
                _left_signup(last_url) and not self._still_robot_page(page) and stage in (
                    "interstitial", "left-signup", "mailbox", "mailbox-ui", ""
                ) and stage != ""
            )
            # 若 stage 记录过 interstitial，一定已建号
            if stage == "interstitial" or self._is_post_signup_success_page(page):
                built_account = True

            if self._still_robot_page(page) and not built_account:
                self._progress(job_id, "captcha", "失败原因: 人机后仍停在「证明你不是机器人」")
                return False

            if not built_account and not fully_in_mail:
                safe_print(f"[Error] 超时仍未进入账户/邮箱 | URL={last_url}")
                safe_print(f"[Debug] {self._page_text_snip(page, 400)!r}")
                self._progress(
                    job_id,
                    "verify",
                    f"失败原因: 人机后等待进入账户超时。URL={last_url[:120]}",
                )
                return False

            if fully_in_mail:
                self._progress(job_id, "inbox", f"已进入账户/邮箱 | {last_url[:100]}")
                safe_print(f"[Verify] [OK] 账户已登录进邮箱，保存成功账号")
            else:
                # 保护页也算成功，但再多等/多点一轮尽量进邮箱
                self._progress(job_id, "verify", "已建号（保护/安全页），再尝试进入邮箱后保存...")
                safe_print("[Verify] [OK] 已建号（保护账户页）。再尝试进入邮箱...")
                for _ in range(15):
                    try:
                        self._dismiss_post_signup_interstitials(page, job_id)
                    except Exception:
                        pass
                    page.wait_for_timeout(1000)
                    try:
                        last_url = page.url or last_url
                    except Exception:
                        pass
                    if _in_mailbox(last_url):
                        fully_in_mail = True
                        self._progress(job_id, "verify", "已从保护页进入邮箱")
                        break
                if fully_in_mail:
                    safe_print("[Verify] [OK] 已从保护页进入邮箱")
                else:
                    safe_print("[Verify] 保护页未能进入邮箱，仍按已建号保存账号（不关窗前已确认成功态）")

        except Exception as _e:
            import traceback
            # 编码错误绝不能吞掉已成功的注册
            is_enc = isinstance(_e, UnicodeEncodeError) or "codec can't encode" in str(_e)
            on_success = False
            try:
                on_success = self._is_post_signup_success_page(page) or (
                    "outlook.live.com/mail" in ((page.url or "").lower())
                ) or (
                    "office.com" in ((page.url or "").lower())
                    and not self._still_robot_page(page)
                )
            except Exception:
                on_success = False

            if on_success or is_enc:
                # 若是编码错误，再探一次页面；探失败也倾向于保存（人机已过）
                if is_enc and not on_success:
                    try:
                        snip = self._page_text_snip(page, 200)
                        if snip and (
                            "保护" in snip
                            or "protect" in snip.lower()
                            or "邮箱" in snip
                            or "inbox" in snip.lower()
                            or "outlook" in snip.lower()
                        ):
                            on_success = True
                    except Exception:
                        pass
                if on_success or is_enc:
                    safe_print(
                        f"[Verify] 捕获 {type(_e).__name__}，但人机已过/疑似成功页，继续保存账号"
                    )
                    self._progress(job_id, "verify", "人机已过（编码/异常不影响），继续保存账号")
                    # fall through to save
                else:
                    safe_print(f"[Error] 等待登录阶段异常: {type(_e).__name__}: {_e}")
                    try:
                        traceback.print_exc()
                    except Exception:
                        pass
                    self._progress(
                        job_id,
                        "verify",
                        f"失败原因: 等待登录异常 {type(_e).__name__}: {str(_e)[:160]}",
                    )
                    return False
            else:
                safe_print(f"[Error] 等待登录阶段异常: {type(_e).__name__}: {_e}")
                try:
                    traceback.print_exc()
                except Exception:
                    pass
                self._progress(
                    job_id,
                    "verify",
                    f"失败原因: 等待登录异常 {type(_e).__name__}: {str(_e)[:160]}",
                )
                return False

        # ---- 确认成功后再写文件；写完 main 才会 clean_up 关浏览器 ----
        self._progress(job_id, "save", "账户已就绪，写入导出文件")
        line_basic = f"{email}{self.email_suffix}: {password}"
        export_line = ""
        try:
            from export_accounts import append_export

            export_line = append_export(f"{email}{self.email_suffix}", password)
        except Exception as _ee:
            safe_print(f"[Export] 导出模块失败，回退旧文件: {_ee}")
            filename = os.path.join(
                self.results_dir,
                "logged_email.txt" if self.enable_oauth2 else "unlogged_email.txt",
            )
            try:
                with open(filename, "a", encoding="utf-8") as f:
                    f.write(line_basic + "\n")
            except Exception as _we:
                safe_print(f"[Error] 写 Results 失败: {_we} | 账号仍是: {line_basic}")
            export_line = line_basic

        try:
            from progress_bus import BUS

            if job_id:
                # 面板复制优先给导入格式
                BUS.set_account_line(job_id, export_line or line_basic)
        except Exception:
            pass
        safe_print(f"[Success: Email Registration] - {export_line or line_basic}")
        safe_print(f"[Export] 已写入 导出/export_accounts.txt")
        # 保存后再短停，方便你肉眼看到邮箱再关窗
        try:
            page.wait_for_timeout(2500)
        except Exception:
            pass

        if not self.enable_oauth2:
            return True

        page.wait_for_timeout(1000)
        for txt in ('否', '暂不', 'No', 'Not now', 'No, thanks'):
            try:
                btn = page.get_by_role('button', name=txt)
                if btn.count() > 0:
                    btn.first.click(timeout=2000)
                    page.wait_for_timeout(1000)
                    break
            except Exception:
                pass
        try:
            page.locator(MAILBOX_READY).first.wait_for(timeout=15000)
            safe_print('[Info] 邮箱已初始化')
        except Exception:
            safe_print('[Warn] 邮箱 UI 未完全加载，账号已保存')
        return True
