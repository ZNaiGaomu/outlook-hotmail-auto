"""
Cloudflare 临时邮箱客户端（cloudflare_temp_email）。

域名、站点地址和口令一律从本地 config.json 的 cf_mail 段或环境变量读取，
源码不内置任何个人域名或后台密码。

说明：
  1) temp-email 的 /api/new_address 会剥掉名称里的「+」等字符。
  2) 优先使用 cf_mail.domain，失败后回退 cf_mail.fallback_domain。
  3) 请在临时邮箱管理后台把所用域名加入允许列表，并保证 Email Routing 全收。

配置见 config.json -> cf_mail
"""

from __future__ import annotations

import json
import os
import random
import re
import string
import time
from typing import Any
from urllib.parse import urljoin

import requests

from controllers.base_controller import safe_print

PREFERRED_DOMAIN = ""
FALLBACK_DOMAIN = ""
DEFAULT_BASE = ""
OTP_RE = re.compile(r"(?<!\d)(\d{4,8})(?!\d)")


def _load_cf_cfg() -> dict[str, Any]:
    cfg: dict[str, Any] = {}
    try:
        with open("config.json", "r", encoding="utf-8") as f:
            raw = json.load(f)
        cfg = dict(raw.get("cf_mail") or {})
    except Exception:
        cfg = {}

    def pick(env_key: str, *keys: str, default: str = "") -> str:
        v = (os.environ.get(env_key) or "").strip()
        if v:
            return v
        for k in keys:
            if cfg.get(k) not in (None, ""):
                return str(cfg.get(k)).strip()
        return default

    return {
        "base_url": pick("CF_MAIL_BASE", "base_url", "base", default=DEFAULT_BASE).rstrip("/"),
        "domain": pick(
            "CF_EMAIL_DOMAIN", "domain", "preferred_domain", default=PREFERRED_DOMAIN
        ),
        "fallback_domain": pick(
            "CF_MAIL_FALLBACK_DOMAIN", "fallback_domain", default=FALLBACK_DOMAIN
        ),
        "site_password": pick("CF_MAIL_SITE_PASSWORD", "site_password", "password"),
        "admin_password": pick("CF_MAIL_ADMIN_PASSWORD", "admin_password", "admin_auth"),
        "enabled": bool(cfg.get("enabled", True)),
    }


class CfTempMail:
    def __init__(self, cfg: dict[str, Any] | None = None):
        self.cfg = cfg or _load_cf_cfg()
        self.base = self.cfg["base_url"].rstrip("/")
        self.domain = self.cfg["domain"] or PREFERRED_DOMAIN
        self.fallback_domain = self.cfg.get("fallback_domain") or FALLBACK_DOMAIN
        self.site_password = self.cfg.get("site_password") or ""
        self.admin_password = self.cfg.get("admin_password") or ""
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "OutlookRegister-CFMail/1.0",
                "x-lang": "zh",
            }
        )
        if self.site_password:
            self.session.headers["x-custom-auth"] = self.site_password
        self.address: str | None = None
        self.jwt: str | None = None
        self.address_id: int | None = None

    def _url(self, path: str) -> str:
        return urljoin(self.base + "/", path.lstrip("/"))

    def health(self) -> dict[str, Any]:
        try:
            r = self.session.get(self._url("/api/open_settings"), timeout=15)
            return {
                "ok": r.status_code < 500,
                "status": r.status_code,
                "base": self.base,
                "domain": self.domain,
                "fallback_domain": self.fallback_domain,
                "has_site_password": bool(self.site_password),
                "has_admin_password": bool(self.admin_password),
                "body_snip": (r.text or "")[:200],
            }
        except Exception as e:
            return {
                "ok": False,
                "error": f"{type(e).__name__}: {e}",
                "base": self.base,
                "domain": self.domain,
            }

    @staticmethod
    def _random_local() -> str:
        # 仅 [a-z0-9]，避免被 Worker NAME_REGEX 剥字符
        return "ms" + "".join(
            random.choice(string.ascii_lowercase + string.digits) for _ in range(10)
        )

    def create_address(self, local_prefix: str | None = None) -> dict[str, Any]:
        """
        创建临时地址。
        域名顺序：config.domain(优先 outlook) → fallback mail → 不传 domain。
        名称仅用字母数字（Worker 会去掉 + 等符号，故不用 ww+xxx 形式建号）。
        """
        name = re.sub(r"[^a-z0-9]", "", (local_prefix or self._random_local()).lower())[:20]
        if not name:
            name = self._random_local()

        domains_try: list[str | None] = []
        for d in (self.domain, PREFERRED_DOMAIN, self.fallback_domain, FALLBACK_DOMAIN, None):
            if d not in domains_try:
                domains_try.append(d)

        last_err = None
        for domain in domains_try:
            # 每次换名，降低「已存在」
            name_try = name if domain == domains_try[0] else self._random_local()
            body: dict[str, Any] = {"name": name_try}
            if domain:
                body["domain"] = domain
            attempts = [("/api/new_address", body, {})]
            if self.admin_password and domain:
                attempts.append(
                    (
                        "/admin/new_address",
                        {"name": name_try, "domain": domain},
                        {"x-admin-auth": self.admin_password},
                    )
                )
            for path, payload, extra_headers in attempts:
                try:
                    headers = dict(self.session.headers)
                    headers.update(extra_headers)
                    r = self.session.post(
                        self._url(path), json=payload, headers=headers, timeout=30
                    )
                    if r.status_code >= 400:
                        last_err = (
                            f"{path} name={payload.get('name')} domain={domain} "
                            f"HTTP {r.status_code}: {(r.text or '')[:200]}"
                        )
                        safe_print(f"[CFMail] create fail {last_err}")
                        continue
                    data = r.json()
                    addr = data.get("address") or data.get("email")
                    jwt = data.get("jwt") or data.get("token")
                    if not addr or not jwt:
                        last_err = f"{path} bad body keys={list(data.keys())}"
                        continue
                    self.address = str(addr)
                    self.jwt = str(jwt)
                    self.address_id = data.get("address_id")
                    if domain and not self.address.lower().endswith(
                        "@" + str(domain).lower()
                    ):
                        safe_print(
                            f"[CFMail] 请求域名 {domain}，实际 {self.address}（按实际地址收信）"
                        )
                    if self.domain and self.domain in self.address:
                        safe_print(f"[CFMail] [优先域名] created {self.address}")
                    else:
                        hint = (
                            f"（若要使用 @{self.domain}，请在 temp-mail 后台添加允许域名）"
                            if self.domain
                            else ""
                        )
                        safe_print(f"[CFMail] created {self.address} {hint}".rstrip())
                    return {
                        "address": self.address,
                        "jwt": self.jwt,
                        "address_id": self.address_id,
                        "raw": data,
                    }
                except Exception as e:
                    last_err = f"{path} domain={domain} {type(e).__name__}: {e}"
                    safe_print(f"[CFMail] create error {last_err}")

        raise RuntimeError(
            f"无法创建 CF 临时邮箱。最后错误: {last_err}。"
            "请在 config.json 的 cf_mail 中填写你自己的 base_url / domain，"
            "并在临时邮箱管理后台把该域名加入允许列表，保证 Email Routing 全收。"
        )

    def _auth_headers(self) -> dict[str, str]:
        if not self.jwt:
            raise RuntimeError("CF mail JWT 为空，请先 create_address")
        h = dict(self.session.headers)
        h["Authorization"] = f"Bearer {self.jwt}"
        return h

    def list_mails(self, limit: int = 20) -> list[dict[str, Any]]:
        headers = self._auth_headers()
        for path in (
            f"/api/parsed_mails?limit={limit}&offset=0",
            f"/api/mails?limit={limit}&offset=0",
        ):
            try:
                r = self.session.get(self._url(path), headers=headers, timeout=20)
                if r.status_code == 401:
                    raise RuntimeError("CF mail JWT 无效/过期")
                if r.status_code >= 400:
                    continue
                data = r.json()
                if isinstance(data, dict):
                    results = data.get("results") or data.get("mails") or data.get("data") or []
                elif isinstance(data, list):
                    results = data
                else:
                    results = []
                return list(results)
            except RuntimeError:
                raise
            except Exception as e:
                safe_print(f"[CFMail] list_mails {path} err: {e}")
        return []

    def get_mail_text(self, mail: dict[str, Any]) -> str:
        parts = [
            str(mail.get("subject") or ""),
            str(mail.get("text") or ""),
            str(mail.get("html") or ""),
            str(mail.get("content") or ""),
            str(mail.get("raw") or ""),
        ]
        mid = mail.get("id")
        if mid is not None and not (mail.get("text") or mail.get("html")):
            headers = self._auth_headers()
            for path in (f"/api/parsed_mail/{mid}", f"/api/mail/{mid}"):
                try:
                    r = self.session.get(self._url(path), headers=headers, timeout=20)
                    if r.status_code < 400:
                        data = r.json()
                        if isinstance(data, dict):
                            parts.extend(
                                [
                                    str(data.get("subject") or ""),
                                    str(data.get("text") or ""),
                                    str(data.get("html") or ""),
                                    str(data.get("raw") or data.get("source") or ""),
                                ]
                            )
                        break
                except Exception:
                    pass
        return "\n".join(parts)

    @staticmethod
    def extract_otp(text: str) -> str | None:
        if not text:
            return None
        for pat in (
            r"(?:安全代码|security code|one-time code|verification code|验证码)[^\d]{0,20}(\d{4,8})",
            r"(?:code is|code:|代码[是为：:])[^\d]{0,10}(\d{4,8})",
        ):
            m = re.search(pat, text, re.I)
            if m:
                return m.group(1)
        cands = OTP_RE.findall(text)
        six = [c for c in cands if len(c) == 6]
        if six:
            return six[0]
        return cands[0] if cands else None

    def wait_otp(
        self,
        timeout_sec: int = 180,
        poll_interval: float = 4.0,
        after_ts: float | None = None,
    ) -> str | None:
        start = time.time()
        seen: set[Any] = set()
        n = 0
        while time.time() - start < timeout_sec:
            n += 1
            mails = self.list_mails(limit=15)
            for mail in mails:
                mid = mail.get("id") or mail.get("message_id") or id(mail)
                if mid in seen:
                    continue
                text = self.get_mail_text(mail)
                blob = (
                    str(mail.get("source") or "")
                    + " "
                    + str(mail.get("sender") or "")
                    + " "
                    + text
                ).lower()
                otp = self.extract_otp(text)
                if otp and (
                    "microsoft" in blob
                    or "account" in blob
                    or "outlook" in blob
                    or "live.com" in blob
                    or "security" in blob
                    or "验证" in text
                    or "code" in blob
                ):
                    safe_print(f"[CFMail] OTP found: {otp} (mail id={mid})")
                    return otp
                if otp and n >= 3:
                    safe_print(f"[CFMail] OTP fallback: {otp} (mail id={mid})")
                    return otp
                seen.add(mid)
            safe_print(f"[CFMail] wait otp poll #{n}, mails={len(mails)}")
            time.sleep(poll_interval)
        return None


def create_recovery_mailbox() -> tuple[str, CfTempMail]:
    client = CfTempMail()
    if not client.cfg.get("enabled", True):
        raise RuntimeError("cf_mail.enabled=false")
    info = client.create_address()
    return info["address"], client
