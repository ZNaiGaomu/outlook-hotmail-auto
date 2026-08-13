"""给已有账号补拿 OAuth2 refresh_token，写回 导出/export_accounts.txt。

用法:
  python reauth_token.py
  python reauth_token.py --email kpvyaqqavfoa@outlook.com
  python reauth_token.py --all   # 所有 Token 为空的账号
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

DIR = Path(__file__).resolve().parent
os.chdir(DIR)
sys.path.insert(0, str(DIR))

from export_accounts import (  # noqa: E402
    EXPORT_ALL,
    append_export,
    parse_any_line,
    read_export_lines,
    _load_client_id,
)
from get_token import get_access_token  # noqa: E402


def _load_targets(only_email: str = "", all_empty: bool = False) -> list[tuple[str, str]]:
    rows = []
    for ln in read_export_lines():
        p = parse_any_line(ln)
        if not p:
            continue
        email, password, _cid, token = p
        if only_email and email.lower() != only_email.lower():
            continue
        if all_empty and token:
            continue
        if only_email or all_empty or not token:
            rows.append((email, password))
    # 去重保序
    seen = set()
    out = []
    for e, p in rows:
        k = e.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append((e, p))
    return out


def _launch_page():
    """复用项目有头 + 住宅代理启动方式。"""
    from controllers.patchright_controller import PatchrightController

    ctrl = PatchrightController()
    page = ctrl.get_thread_page()
    return ctrl, page


def reauth_one(email: str, password: str) -> bool:
    print(f"[reauth] 开始: {email}", flush=True)
    ctrl = None
    page = None
    try:
        ctrl, page = _launch_page()
        # 先打开一次 outlook 建立会话（可选）
        try:
            page.goto("https://outlook.live.com/mail/", timeout=45000, wait_until="domcontentloaded")
            page.wait_for_timeout(1500)
        except Exception as e:
            print(f"[reauth] 预开邮箱页: {e}", flush=True)

        # 登录（若未登录）
        try:
            from get_token import handle_oauth2_form, _dismiss_password_bubble

            # 若在登录页
            if "login" in (page.url or "") or page.locator('[name="loginfmt"]').count() > 0:
                handle_oauth2_form(page, email, password=password)
            _dismiss_password_bubble(page)
        except Exception:
            pass

        result = get_access_token(page, email, password=password, max_retries=3)
        if not result[0]:
            print(f"[reauth] FAIL token: {email}", flush=True)
            return False
        refresh, access, exp = result
        line = append_export(email, password, _load_client_id(), refresh or "")
        # 旧 token 文件
        tok_path = DIR / "Results" / "outlook_token.txt"
        tok_path.parent.mkdir(parents=True, exist_ok=True)
        with tok_path.open("a", encoding="utf-8") as f:
            f.write(f"{email}---{password}---{refresh}---{access}---{exp}\n")
        print(f"[reauth] OK: {line[:80]}... token_len={len(refresh or '')}", flush=True)
        print(f"[reauth] 已写入 {EXPORT_ALL}", flush=True)
        return True
    except Exception as e:
        print(f"[reauth] 异常: {type(e).__name__}: {e}", flush=True)
        return False
    finally:
        try:
            if ctrl is not None:
                ctrl.clean_up(page, "done_browser")
        except Exception:
            pass
        try:
            if ctrl is not None:
                ctrl.clean_up(type="all_browser")
        except Exception:
            pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", default="", help="只处理指定邮箱")
    ap.add_argument("--all", action="store_true", help="所有 Token 为空的账号")
    ap.add_argument("--password", default="", help="覆盖密码（不从导出文件读）")
    args = ap.parse_args()

    if args.email and args.password:
        targets = [(args.email, args.password)]
    else:
        targets = _load_targets(only_email=args.email, all_empty=args.all or not args.email)
        if args.email and args.password:
            targets = [(args.email, args.password)]
        if args.email and not targets:
            # 允许命令行密码
            if args.password:
                targets = [(args.email, args.password)]

    if not targets:
        print("[reauth] 没有待处理账号（导出文件里都已有 Token？）", flush=True)
        print(f"[reauth] 文件: {EXPORT_ALL}", flush=True)
        return 1

    print(f"[reauth] 待处理 {len(targets)} 个", flush=True)
    ok = 0
    for email, password in targets:
        if reauth_one(email, password):
            ok += 1
    print(f"[reauth] 完成 成功 {ok}/{len(targets)}", flush=True)
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
