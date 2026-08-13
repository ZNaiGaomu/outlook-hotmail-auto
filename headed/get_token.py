"""OAuth2 授权码 + PKCE → refresh_token / access_token。

依赖 config.json 的 oauth2 段：
  enable_oauth2 / client_id / redirect_url / Scopes

关键点（根据实测）：
1. 同意页常被 Chromium「保存密码」弹层挡住 → 必须先关弹层再点「接受」
2. 授权成功后落到 nativeclient?code=... 白页（钓鱼警告文案）
   → 必须从 page.url / request / framenavigated 抓 code，不要等「正常页面」
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import string
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import getproxies

import requests

PROJECT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_DIR / "config.json"

DEFAULT_CLIENT_ID = ""
DEFAULT_REDIRECT = "https://login.microsoftonline.com/common/oauth2/nativeclient"
DEFAULT_SCOPES = [
    "offline_access",
    "https://outlook.office.com/IMAP.AccessAsUser.All",
    "https://outlook.office.com/POP.AccessAsUser.All",
    "https://outlook.office.com/SMTP.Send",
]


def _safe_print(msg: str) -> None:
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        try:
            print(
                str(msg).encode("gbk", errors="replace").decode("gbk", errors="replace"),
                flush=True,
            )
        except Exception:
            try:
                print(str(msg).encode("ascii", errors="replace").decode("ascii"), flush=True)
            except Exception:
                pass


def _load_oauth_cfg() -> dict[str, Any]:
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        data = json.load(f)
    o = data.get("oauth2") or {}
    client_id = str(o.get("client_id") or "").strip() or DEFAULT_CLIENT_ID
    redirect_url = str(o.get("redirect_url") or "").strip() or DEFAULT_REDIRECT
    scopes = o.get("Scopes") or []
    if not isinstance(scopes, list) or not scopes:
        scopes = list(DEFAULT_SCOPES)
    scopes = [str(s).strip() for s in scopes if str(s).strip()]
    email_suffix = str(data.get("email_suffix") or "@outlook.com")
    proxy = ""
    if bool(data.get("use_residential", True)):
        proxy = str(data.get("residential_proxy") or data.get("proxy") or "").strip()
    else:
        proxy = str(data.get("proxy") or "").strip()
    return {
        "client_id": client_id,
        "redirect_url": redirect_url,
        "scopes": scopes,
        "email_suffix": email_suffix,
        "proxy": proxy,
    }


def get_proxy(cfg_proxy: str = "") -> dict[str, str | None]:
    p = (cfg_proxy or "").strip()
    if p:
        return {"http": p, "https": p}
    proxies = getproxies()
    http_proxy = proxies.get("http") or proxies.get("https")
    if http_proxy:
        return {"http": http_proxy, "https": http_proxy}
    return {"http": None, "https": None}


def generate_code_verifier(length: int = 128) -> str:
    alphabet = string.ascii_letters + string.digits + "-._~"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def generate_code_challenge(code_verifier: str) -> str:
    sha256_hash = hashlib.sha256(code_verifier.encode()).digest()
    return base64.urlsafe_b64encode(sha256_hash).decode().rstrip("=")


def _page_blob(page: Any, n: int = 400) -> str:
    try:
        return (page.locator("body").inner_text(timeout=1200) or "")[:n]
    except Exception:
        return ""


def _is_consent_page(page: Any) -> bool:
    """是否还在『是否允许此应用访问你的信息』同意页。"""
    try:
        url = (page.url or "").lower()
    except Exception:
        url = ""
    if "code=" in url and "nativeclient" in url:
        return False
    blob = _page_blob(page, 500)
    keys = (
        "是否允许此应用",
        "接受这些权限",
        "Accept",
        "权限",
        "Thunderbird",
        "consent",
        "需要得到你的许可",
        "读写访问权限",
    )
    if any(k in blob for k in keys):
        return True
    # 底部有「接受」按钮
    try:
        if page.get_by_role("button", name="接受").count() > 0:
            return True
        if page.get_by_role("button", name="Accept").count() > 0:
            return True
    except Exception:
        pass
    return False


def _dismiss_password_bubble(page: Any) -> bool:
    """关掉 Chromium『要保存密码吗？』/ 其它遮挡弹层。返回是否点到了。"""
    closed = False

    # 先试明确按钮（截图：一律不 / 不用了 / 保存）
    for txt in (
        "一律不",
        "不用了",
        "从不",
        "Never",
        "Not now",
        "No thanks",
        "Don't save",
        "Never save",
        "取消",
    ):
        try:
            btn = page.get_by_role("button", name=txt)
            if btn.count() == 0:
                btn = page.get_by_text(txt, exact=True)
            if btn.count() > 0:
                _safe_print(f"[OAuth2] 关闭密码弹层: {txt}")
                try:
                    btn.first.click(timeout=2000, force=True)
                except Exception:
                    try:
                        btn.first.evaluate("el => el.click()")
                    except Exception:
                        continue
                page.wait_for_timeout(500)
                closed = True
                break
        except Exception:
            continue

    # 点弹层右上角 X（若有）
    if not closed:
        try:
            # 气泡里的关闭
            xbtn = page.locator('[aria-label="关闭"], [aria-label="Close"], button[aria-label*="Close"]')
            if xbtn.count() > 0:
                xbtn.first.click(timeout=1000, force=True)
                page.wait_for_timeout(300)
                closed = True
                _safe_print("[OAuth2] 关闭密码弹层: X")
        except Exception:
            pass

    # Escape + 点空白
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(200)
    except Exception:
        pass
    try:
        page.mouse.click(12, 12)
        page.wait_for_timeout(200)
    except Exception:
        pass
    return closed


def _click_accept_consent(page: Any) -> bool:
    """
    严格按你的建议：
      1) 先关『要保存密码吗』
      2) 再点蓝色『接受』
      3) 点完多等一会儿，不要立刻跳去换 token
    """
    # 多轮关弹层，确保不挡
    for _ in range(3):
        _dismiss_password_bubble(page)
        page.wait_for_timeout(400)

    # 等接受按钮真正可点
    page.wait_for_timeout(800)

    clicked = False

    # 1) 精确 role=button name=接受
    for txt in ("接受", "Accept"):
        try:
            loc = page.get_by_role("button", name=txt)
            if loc.count() > 0:
                el = loc.first
                try:
                    el.scroll_into_view_if_needed(timeout=2000)
                except Exception:
                    pass
                # 再关一次弹层
                _dismiss_password_bubble(page)
                page.wait_for_timeout(300)
                _safe_print(f"[OAuth2] >>> 点击同意按钮: {txt}")
                try:
                    el.click(timeout=5000, force=True)
                    clicked = True
                except Exception as e1:
                    _safe_print(f"[OAuth2] force click 失败: {e1}, 试 JS click")
                    try:
                        el.evaluate("el => el.click()")
                        clicked = True
                    except Exception as e2:
                        _safe_print(f"[OAuth2] JS click 失败: {e2}")
                if clicked:
                    break
        except Exception as e:
            _safe_print(f"[OAuth2] 找按钮 {txt} 异常: {e}")

    # 2) 其它选择器
    if not clicked:
        for sel in (
            '[data-testid="appConsentPrimaryButton"]',
            'button:has-text("接受")',
            'button:has-text("Accept")',
            'input[type="submit"][value="接受"]',
            'input[type="submit"][value="Accept"]',
            '#idSIButton9',
        ):
            try:
                b = page.locator(sel)
                if b.count() == 0:
                    continue
                _dismiss_password_bubble(page)
                _safe_print(f"[OAuth2] >>> 点击 consent sel: {sel}")
                try:
                    b.first.click(timeout=5000, force=True)
                except Exception:
                    b.first.evaluate("el => el.click()")
                clicked = True
                break
            except Exception:
                continue

    if not clicked:
        _safe_print("[OAuth2] 未找到/未能点击『接受』")
        return False

    # 点完后故意多等：等页面跳到 nativeclient?code=...
    _safe_print("[OAuth2] 已点『接受』，等待跳转携带 code（不立刻换 token）...")
    for wait_i in range(40):  # 最多约 8s
        page.wait_for_timeout(200)
        try:
            u = page.url or ""
        except Exception:
            u = ""
        if "code=" in u:
            _safe_print(f"[OAuth2] 接受后已跳到 code URL ({wait_i}): {u[:140]}")
            return True
        # 若仍在同意页，再点一次
        if wait_i in (10, 20, 30) and _is_consent_page(page):
            _safe_print("[OAuth2] 仍在同意页，再次关弹层并点接受")
            _dismiss_password_bubble(page)
            try:
                btn = page.get_by_role("button", name="接受")
                if btn.count() == 0:
                    btn = page.get_by_role("button", name="Accept")
                if btn.count() > 0:
                    btn.first.click(timeout=3000, force=True)
            except Exception:
                pass
    _safe_print("[OAuth2] 点接受后暂未看到 code URL，继续由轮询捕获")
    return True


def handle_oauth2_form(page: Any, email: str, password: str = "") -> None:
    page.wait_for_timeout(800)
    _dismiss_password_bubble(page)

    # 账号选择器
    try:
        tile = page.get_by_text(email, exact=False)
        if tile.count() > 0 and tile.first.is_visible(timeout=1200):
            _safe_print(f"[OAuth2] 看到账号选择器, 点击 {email}")
            tile.first.click(timeout=5000)
            page.wait_for_timeout(1500)
    except Exception:
        pass

    # 邮箱
    try:
        lf = page.locator('[name="loginfmt"]')
        if lf.count() > 0 and lf.first.is_visible(timeout=1200):
            _safe_print("[OAuth2] 输入 email")
            lf.fill(email, timeout=10000)
            try:
                page.locator("#idSIButton9").click(timeout=5000)
            except Exception:
                pass
            page.wait_for_timeout(2000)
    except Exception:
        pass

    # 密码（会话掉线时）
    if password:
        try:
            pw = page.locator('[name="passwd"], input[type="password"]')
            if pw.count() > 0 and pw.first.is_visible(timeout=1200):
                _safe_print("[OAuth2] 输入 password")
                pw.first.fill(password, timeout=10000)
                try:
                    page.locator("#idSIButton9").click(timeout=5000)
                except Exception:
                    pass
                page.wait_for_timeout(2000)
                _dismiss_password_bubble(page)
        except Exception:
            pass

    # 保持登录 / KMSI —— 点「否」（不要点是）
    try:
        blob = _page_blob(page, 400)
        if "保持登录" in blob or "stay signed in" in blob.lower():
            for txt in ("否", "暂不", "No", "Not now", "No, thanks"):
                try:
                    btn = page.get_by_role("button", name=txt)
                    if btn.count() == 0:
                        btn = page.locator("#idBtn_Back")
                    if btn.count() > 0 and btn.first.is_visible(timeout=800):
                        _safe_print(f"[OAuth2] 保持登录 → {txt}")
                        btn.first.click(timeout=3000, force=True)
                        page.wait_for_timeout(1000)
                        break
                except Exception:
                    pass
    except Exception:
        pass

    # 同意条款 —— 必须点「接受」
    for _ in range(5):
        if not _is_consent_page(page):
            try:
                u = page.url or ""
            except Exception:
                u = ""
            if "code=" in u:
                break
        if _click_accept_consent(page):
            try:
                if "code=" in (page.url or ""):
                    break
            except Exception:
                pass
        page.wait_for_timeout(700)


def _extract_code_from_url(url: str, redirect_url: str = "") -> str | None:
    if not url or "code=" not in url:
        return None
    try:
        qs = parse_qs(urlparse(url).query)
        codes = qs.get("code") or []
        if codes and codes[0]:
            return codes[0]
    except Exception:
        pass
    try:
        part = url.split("code=", 1)[1]
        return part.split("&", 1)[0].split("#", 1)[0] or None
    except Exception:
        return None


def _url_looks_like_code_redirect(url: str, redirect_url: str) -> bool:
    if not url or "code=" not in url:
        return False
    if "error=" in url and "code=" not in url:
        return False
    base = (redirect_url or DEFAULT_REDIRECT).split("?")[0]
    if base and base in url:
        return True
    if "nativeclient" in url:
        return True
    if "code=" in url:
        return True
    return False


def get_access_token(
    page: Any,
    email: str,
    max_retries: int = 3,
    password: str = "",
) -> tuple[Any, Any, Any]:
    for attempt in range(max_retries):
        _safe_print(f"[OAuth2] 尝试 #{attempt + 1}/{max_retries}")
        result = _try_get_access_token(page, email, password=password)
        if result[0] is not False:
            return result
        # 重试前稍等，清一下可能的中间态
        try:
            page.wait_for_timeout(800)
        except Exception:
            pass
    return False, False, False


def _try_get_access_token(
    page: Any,
    email: str,
    password: str = "",
) -> tuple[Any, Any, Any]:
    cfg = _load_oauth_cfg()
    scopes: list[str] = cfg["scopes"]
    client_id: str = cfg["client_id"]
    redirect_url: str = cfg["redirect_url"]
    email_suffix: str = cfg["email_suffix"]
    proxy_url: str = cfg["proxy"]

    code_verifier = generate_code_verifier()
    code_challenge = generate_code_challenge(code_verifier)
    full_email = email if "@" in email else f"{email}{email_suffix}"
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_url,
        "scope": " ".join(scopes),
        "response_mode": "query",
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "domain_hint": "consumers",
        "login_hint": full_email,
        # 已登录会话下不要强制 select_account，减少干扰
        # "prompt": "select_account",
    }

    authorize_url = (
        "https://login.microsoftonline.com/consumers/oauth2/v2.0/authorize?"
        + "&".join(f"{k}={quote(v, safe='')}" for k, v in params.items())
    )

    captured_url: str | None = None

    def _maybe_capture(u: str, source: str = "") -> None:
        nonlocal captured_url
        if captured_url:
            return
        if u and _url_looks_like_code_redirect(u, redirect_url) and _extract_code_from_url(u):
            captured_url = u
            _safe_print(f"[OAuth2] 捕获 code URL via {source}: {u[:160]}")

    def on_request(request: Any) -> None:
        try:
            _maybe_capture(request.url, "request")
        except Exception:
            pass

    def on_response(response: Any) -> None:
        try:
            _maybe_capture(response.url, "response")
        except Exception:
            pass

    def on_frame(frame: Any) -> None:
        try:
            _maybe_capture(frame.url, "frame")
        except Exception:
            pass

    page.on("request", on_request)
    try:
        page.on("response", on_response)
    except Exception:
        pass
    try:
        page.on("framenavigated", on_frame)
    except Exception:
        pass

    try:
        _safe_print(
            f"[OAuth2] goto authorize (client={client_id[:8]}..., redirect={redirect_url[:60]})"
        )
        try:
            page.wait_for_timeout(200)
            # nativeclient 落地常被当失败导航，用 commit 更稳
            page.goto(authorize_url, timeout=60000, wait_until="commit")
            _safe_print(f"[OAuth2] goto 完成, 当前 URL: {page.url[:160]}")
        except Exception as e:
            try:
                u = page.url
                _maybe_capture(u, "goto-exc")
                if not captured_url:
                    _safe_print(f"[OAuth2] goto 失败: {e}")
                    # 不立刻 return：有时页面其实在，继续走表单
            except Exception:
                _safe_print(f"[OAuth2] goto 失败: {e}")
                return False, False, False

        # 已授权账号可能直接带 code；否则先走登录/同意
        try:
            _maybe_capture(page.url, "page.url-init")
        except Exception:
            pass

        # 关键：若停在同意页，必须先关密码弹窗再点『接受』，点完再等 code
        # 不要一上来就急着换 token
        if not captured_url:
            page.wait_for_timeout(1500)
            _dismiss_password_bubble(page)
            if _is_consent_page(page):
                _safe_print("[OAuth2] 检测到同意页，先关弹层再点『接受』")
                _click_accept_consent(page)
                try:
                    _maybe_capture(page.url, "after-accept")
                except Exception:
                    pass
            else:
                handle_oauth2_form(page, full_email, password=password)
                # 登录/KMSI 之后可能又进入同意页
                page.wait_for_timeout(1000)
                if not captured_url and _is_consent_page(page):
                    _safe_print("[OAuth2] 登录后进入同意页，点『接受』")
                    _click_accept_consent(page)
                    try:
                        _maybe_capture(page.url, "after-login-accept")
                    except Exception:
                        pass

        accept_tries = 0
        for i in range(600):  # ~60s
            page.wait_for_timeout(100)

            if captured_url:
                # 再多停 0.5s，确保导航稳定后再换 token
                page.wait_for_timeout(500)
                break

            try:
                current_url = page.url
            except Exception:
                current_url = ""

            _maybe_capture(current_url, f"poll-{i}")

            if current_url and ("error=" in current_url or "res=error" in current_url) and "code=" not in current_url:
                _safe_print(f"[OAuth2] URL 出现 error: {current_url[:220]}")
                return False, False, False

            # 每 2s：若仍在同意页 → 关弹层 + 点接受（不急着做别的）
            if i > 0 and i % 20 == 0:
                _safe_print(f"[OAuth2] 等 code... iter {i}, URL: {current_url[:120]}")
                _dismiss_password_bubble(page)
                if _is_consent_page(page):
                    accept_tries += 1
                    _safe_print(f"[OAuth2] 仍在同意页，第 {accept_tries} 次点『接受』")
                    _click_accept_consent(page)
                elif accept_tries == 0:
                    # 可能还在账号选择/密码
                    handle_oauth2_form(page, full_email, password=password)
                    if _is_consent_page(page):
                        _click_accept_consent(page)
        else:
            _safe_print("[OAuth2] 50s 超时未捕获 redirect code")
            try:
                out = PROJECT_DIR / "Results" / "oauth_timeout.png"
                out.parent.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(out), full_page=True)
                _safe_print(f"[OAuth2] 截图保存到 {out}")
            except Exception:
                pass
            try:
                body = page.locator("body").inner_text(timeout=3000)[:600]
                _safe_print(f"[OAuth2] 页面文本: {body!r}")
            except Exception:
                pass
            try:
                _safe_print(f"[OAuth2] 最终 URL: {page.url[:220]}")
            except Exception:
                pass
            return False, False, False

    finally:
        for ev, fn in (
            ("request", on_request),
            ("response", on_response),
            ("framenavigated", on_frame),
        ):
            try:
                page.remove_listener(ev, fn)
            except Exception:
                pass

    if not captured_url:
        _safe_print("[OAuth2] captured_url 无效")
        return False, False, False

    auth_code = _extract_code_from_url(captured_url, redirect_url)
    if not auth_code:
        _safe_print("[OAuth2] 无法解析 auth_code")
        return False, False, False

    _safe_print(f"[OAuth2] 拿到 auth_code len={len(auth_code)}, 换 refresh_token...")

    token_data = {
        "client_id": client_id,
        "code": auth_code,
        "redirect_uri": redirect_url,
        "grant_type": "authorization_code",
        "code_verifier": code_verifier,
        "scope": " ".join(scopes),
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    token_url = "https://login.microsoftonline.com/common/oauth2/v2.0/token"

    # 换 token 是服务端 POST：住宅桥 17890 对 login.microsoftonline.com 常 ProxyError
    # 优先本机直连；失败再试住宅代理。浏览器授权已在住宅 IP 下完成，code 换 token 不要求同出口。
    proxy_candidates: list[tuple[str, dict[str, str | None]]] = [
        ("direct", {"http": None, "https": None}),
    ]
    if proxy_url:
        proxy_candidates.append(("resi", get_proxy(proxy_url)))
    # 系统代理兜底
    sys_p = get_proxy("")
    if sys_p.get("http") or sys_p.get("https"):
        proxy_candidates.append(("system", sys_p))

    last_err: str = ""
    for label, proxies in proxy_candidates:
        try:
            _safe_print(f"[OAuth2] token POST via {label}...")
            response = requests.post(
                token_url,
                data=token_data,
                headers=headers,
                proxies=proxies,
                timeout=30,
            )
            resp_json = response.json()
            if "refresh_token" in resp_json:
                _safe_print(f"[OAuth2] refresh_token 获取成功 (via {label})")
                return (
                    resp_json["refresh_token"],
                    resp_json.get("access_token", ""),
                    datetime.now().timestamp()
                    + int(resp_json.get("expires_in") or 3600),
                )
            last_err = f"status={response.status_code} body={resp_json}"
            _safe_print(f"[OAuth2] token 无 refresh_token via {label}: {last_err}")
            # invalid_grant 等业务错误换代理也没用，直接停
            err = str(resp_json.get("error") or "")
            if err in ("invalid_grant", "invalid_client", "unauthorized_client"):
                break
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            _safe_print(f"[OAuth2] token 请求异常 via {label}: {last_err}")
            continue

    _safe_print(f"[OAuth2] 换 token 全部失败: {last_err}")
    return False, False, False
