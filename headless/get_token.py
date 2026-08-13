import json
import base64
import string
import hashlib
import secrets
import requests
from datetime import datetime
from urllib.request import getproxies
from urllib.parse import quote, parse_qs


def get_proxy():
    proxies = getproxies()
    http_proxy = proxies.get('http') or proxies.get('https')
    if http_proxy:
        return {"http": http_proxy, "https": http_proxy}
    return {"http": None, "https": None}


def generate_code_verifier(length=128):
    alphabet = string.ascii_letters + string.digits + '-._~'
    return ''.join(secrets.choice(alphabet) for _ in range(length))


def generate_code_challenge(code_verifier):
    sha256_hash = hashlib.sha256(code_verifier.encode()).digest()
    return base64.urlsafe_b64encode(sha256_hash).decode().rstrip('=')


def handle_oauth2_form(page, email):
    page.wait_for_timeout(2500)

    try:
        tile = page.get_by_text(email, exact=False)
        if tile.count() > 0 and tile.first.is_visible(timeout=1500):
            print(f"[OAuth2] 看到账号选择器, 点击 {email}")
            tile.first.click(timeout=5000)
            page.wait_for_timeout(2000)
    except Exception:
        pass

    try:
        lf = page.locator('[name="loginfmt"]')
        if lf.count() > 0 and lf.first.is_visible(timeout=1500):
            print("[OAuth2] 输入 email")
            lf.fill(email, timeout=10000)
            try:
                page.locator('#idSIButton9').click(timeout=5000)
            except Exception:
                pass
            page.wait_for_timeout(2500)
    except Exception:
        pass

    for txt in ('否', '暂不', 'No', 'Not now', 'No, thanks'):
        try:
            btn = page.get_by_role('button', name=txt)
            if btn.count() > 0 and btn.first.is_visible(timeout=1200):
                print(f"[OAuth2] 跳过 KMSI '{txt}'")
                btn.first.click(timeout=3000)
                page.wait_for_timeout(1500)
                break
        except Exception:
            pass

    consent_selectors = [
        '[data-testid="appConsentPrimaryButton"]',
        'input[type="submit"][value="Accept"]',
        'input[type="submit"][value="接受"]',
        'input[type="submit"][value="是"]',
        'input[type="submit"][value="Yes"]',
        'button:has-text("Accept")',
        'button:has-text("接受")',
        'button:has-text("同意")',
        'button:has-text("Yes")',
        'button:has-text("是")',
        '#idSIButton9',
    ]
    for sel in consent_selectors:
        try:
            b = page.locator(sel)
            if b.count() > 0 and b.first.is_visible(timeout=1200):
                print(f"[OAuth2] 点击 consent: {sel}")
                b.first.click(timeout=5000)
                page.wait_for_timeout(1500)
                break
        except Exception:
            continue


def get_access_token(page, email, max_retries=3):
    for attempt in range(max_retries):
        print(f"[OAuth2] 尝试 #{attempt + 1}/{max_retries}")
        result = _try_get_access_token(page, email)
        if result[0] is not False:
            return result
    return False, False, False


def _try_get_access_token(page, email):
    with open('config.json', 'r', encoding='utf-8') as f:
        data = json.load(f)
    SCOPES = data['oauth2']['Scopes']
    client_id = data['oauth2']['client_id']
    redirect_url = data['oauth2']['redirect_url']
    _email_suffix = data['email_suffix']

    code_verifier = generate_code_verifier()
    code_challenge = generate_code_challenge(code_verifier)
    full_email = f"{email}{_email_suffix}"
    params = {
        'client_id': client_id,
        'response_type': 'code',
        'redirect_uri': redirect_url,
        'scope': ' '.join(SCOPES),
        'response_mode': 'query',
        'code_challenge': code_challenge,
        'code_challenge_method': 'S256',
        'domain_hint': 'consumers',
        'login_hint': full_email,
    }

    authorize_url = (
        "https://login.microsoftonline.com/consumers/oauth2/v2.0/authorize?"
        + "&".join(f"{k}={quote(v)}" for k, v in params.items())
    )

    captured_url = None

    def on_request(request):
        nonlocal captured_url
        if redirect_url in request.url and 'code=' in request.url:
            captured_url = request.url

    page.on("request", on_request)

    try:
        print(f"[OAuth2] goto authorize (client={client_id[:8]}..., redirect={redirect_url[:60]})")
        try:
            page.wait_for_timeout(250)
            page.goto(authorize_url, timeout=30000)
            print(f"[OAuth2] goto 完成, 当前 URL: {page.url[:140]}")
        except Exception as e:
            print(f"[OAuth2] goto 失败: {e}")
            return False, False, False

        handle_oauth2_form(page, full_email)

        refresh_done = False

        for i in range(400):
            page.wait_for_timeout(100)
            if captured_url:
                print(f"[OAuth2] 捕获 redirect (iter {i}): {captured_url[:140]}")
                break

            current_url = page.url
            if 'res=error' in current_url or 'error=' in current_url:
                print(f"[OAuth2] URL 出现 error: {current_url[:220]}")
                return False, False, False

            if i > 0 and i % 50 == 0:
                print(f"[OAuth2] 等 redirect... iter {i}, URL: {current_url[:120]}")
                handle_oauth2_form(page, full_email)

            if i == 200 and not refresh_done:
                print("[OAuth2] 20s 无 redirect, 刷新一次")
                refresh_done = True
                try:
                    page.reload(timeout=10000)
                except Exception as _re:
                    print(f"[OAuth2] reload 失败: {_re}")
        else:
            print("[OAuth2] 40s 超时未捕获 redirect")
            try:
                page.screenshot(path='/tmp/oauth_timeout.png', full_page=True)
                print("[OAuth2] 截图保存到 /tmp/oauth_timeout.png")
            except Exception:
                pass
            try:
                body = page.locator('body').inner_text(timeout=3000)[:600]
                print(f"[OAuth2] 页面文本: {body!r}")
            except Exception:
                pass
            try:
                print(f"[OAuth2] 最终 URL: {page.url[:220]}")
            except Exception:
                pass
            return False, False, False

    finally:
        try:
            page.remove_listener("request", on_request)
        except Exception:
            pass

    if not captured_url or 'code=' not in captured_url:
        print("[OAuth2] captured_url 无效")
        return False, False, False

    auth_code = parse_qs(captured_url.split('?')[1])['code'][0]
    print("[OAuth2] 拿到 auth_code, 换 refresh_token...")

    try:
        response = requests.post(
            'https://login.microsoftonline.com/common/oauth2/v2.0/token',
            data={
                'client_id': client_id,
                'code': auth_code,
                'redirect_uri': redirect_url,
                'grant_type': 'authorization_code',
                'code_verifier': code_verifier,
                'scope': ' '.join(SCOPES),
            },
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
            proxies=get_proxy(),
            timeout=20,
        )
        resp_json = response.json()
        if 'refresh_token' in resp_json:
            print("[OAuth2] ✅ refresh_token 获取成功")
            return (
                resp_json['refresh_token'],
                resp_json.get('access_token', ''),
                datetime.now().timestamp() + resp_json['expires_in'],
            )
        else:
            print(f"[OAuth2] token 接口无 refresh_token, 响应: {resp_json}")
    except Exception as e:
        print(f"[OAuth2] token 请求异常: {e}")
        return False, False, False

    return False, False, False
