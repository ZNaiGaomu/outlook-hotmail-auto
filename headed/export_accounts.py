"""
账号导出：统一写成

  邮箱----密码----ClientID----Token

固定目录：
  导出/
    export_accounts.txt   # 累计全部（导入用）
    latest.txt            # 最近一次成功
    by_date/YYYY-MM-DD.txt

也兼容旧 Results/unlogged_email.txt / logged_email.txt / outlook_token.txt。
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Iterable

PROJECT_DIR = Path(__file__).resolve().parent
EXPORT_DIR = PROJECT_DIR / "导出"
EXPORT_ALL = EXPORT_DIR / "export_accounts.txt"
EXPORT_LATEST = EXPORT_DIR / "latest.txt"
EXPORT_BY_DATE = EXPORT_DIR / "by_date"
RESULTS_DIR = PROJECT_DIR / "Results"

# 从本地 config.json 读取；未配置时留空，避免把他人的 Client ID 写进导出文件
DEFAULT_CLIENT_ID = ""


def _load_client_id() -> str:
    try:
        raw = json.loads((PROJECT_DIR / "config.json").read_text(encoding="utf-8"))
        cid = (raw.get("oauth2") or {}).get("client_id") or ""
        if str(cid).strip():
            return str(cid).strip()
    except Exception:
        pass
    return DEFAULT_CLIENT_ID


def ensure_export_dirs() -> None:
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    EXPORT_BY_DATE.mkdir(parents=True, exist_ok=True)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)


def format_line(email: str, password: str, client_id: str = "", token: str = "") -> str:
    email = (email or "").strip()
    password = (password or "").strip()
    client_id = (client_id or "").strip() or _load_client_id()
    token = (token or "").strip()
    return f"{email}----{password}----{client_id}----{token}"


def parse_any_line(line: str) -> tuple[str, str, str, str] | None:
    """解析多种历史格式 -> email, password, client_id, token。"""
    s = (line or "").strip()
    if not s or s.startswith("#"):
        return None

    # 新格式
    if "----" in s:
        parts = s.split("----")
        while len(parts) < 4:
            parts.append("")
        return parts[0].strip(), parts[1].strip(), parts[2].strip(), parts[3].strip()

    # 旧 token: email---password---refresh---access---expire
    if "---" in s:
        parts = s.split("---")
        email = parts[0].strip()
        password = parts[1].strip() if len(parts) > 1 else ""
        refresh = parts[2].strip() if len(parts) > 2 else ""
        return email, password, _load_client_id(), refresh

    # 旧 unlogged: email: password  或 email:password
    m = re.match(r"^(\S+@\S+)\s*:\s*(.+)$", s)
    if m:
        return m.group(1).strip(), m.group(2).strip(), _load_client_id(), ""

    return None


def append_export(
    email: str,
    password: str,
    client_id: str = "",
    token: str = "",
) -> str:
    """追加一条成功账号到固定导出目录，返回格式化行。"""
    ensure_export_dirs()
    line = format_line(email, password, client_id, token)

    # 去重：同邮箱保留最新一行
    existing: list[str] = []
    if EXPORT_ALL.exists():
        existing = [
            ln.rstrip("\n")
            for ln in EXPORT_ALL.read_text(encoding="utf-8", errors="replace").splitlines()
            if ln.strip()
        ]
    email_l = email.strip().lower()
    kept = []
    for ln in existing:
        parsed = parse_any_line(ln)
        if parsed and parsed[0].lower() == email_l:
            continue
        kept.append(ln)
    kept.append(line)
    EXPORT_ALL.write_text("\n".join(kept) + "\n", encoding="utf-8")

    EXPORT_LATEST.write_text(line + "\n", encoding="utf-8")
    day = datetime.now().strftime("%Y-%m-%d")
    day_file = EXPORT_BY_DATE / f"{day}.txt"
    with day_file.open("a", encoding="utf-8") as f:
        f.write(line + "\n")

    # 兼容旧路径
    with (RESULTS_DIR / "unlogged_email.txt").open("a", encoding="utf-8") as f:
        # 若已存在同邮箱则仍追加一行旧格式，便于面板读取
        f.write(f"{email}: {password}\n")

    return line


def migrate_results_to_export() -> list[str]:
    """把 Results 里旧账号迁到 导出/export_accounts.txt。"""
    ensure_export_dirs()
    lines_out: list[str] = []
    sources = [
        RESULTS_DIR / "unlogged_email.txt",
        RESULTS_DIR / "logged_email.txt",
        RESULTS_DIR / "outlook_token.txt",
        EXPORT_ALL,
    ]
    seen: set[str] = set()
    for path in sources:
        if not path.exists():
            continue
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            parsed = parse_any_line(raw)
            if not parsed:
                continue
            email, password, client_id, token = parsed
            key = email.lower()
            # token 文件优先覆盖无 token 的
            if key in seen and not token:
                continue
            line = format_line(email, password, client_id, token)
            # 若已有同邮箱且新行有 token，替换
            if key in seen and token:
                lines_out = [ln for ln in lines_out if not ln.lower().startswith(key + "----")]
            if key not in seen or token:
                if key not in seen:
                    lines_out.append(line)
                    seen.add(key)
                else:
                    lines_out.append(line)
    # 去重保序（最后出现优先）
    final: dict[str, str] = {}
    order: list[str] = []
    for ln in lines_out:
        p = parse_any_line(ln)
        if not p:
            continue
        k = p[0].lower()
        if k not in final:
            order.append(k)
        # 有 token 的覆盖无 token
        old = final.get(k)
        if old:
            op = parse_any_line(old)
            if op and op[3] and not p[3]:
                continue
        final[k] = format_line(*p)
    merged = [final[k] for k in order]
    EXPORT_ALL.write_text(("\n".join(merged) + "\n") if merged else "", encoding="utf-8")
    if merged:
        EXPORT_LATEST.write_text(merged[-1] + "\n", encoding="utf-8")
    return merged


def read_export_lines() -> list[str]:
    ensure_export_dirs()
    if not EXPORT_ALL.exists():
        return migrate_results_to_export()
    return [
        ln.strip()
        for ln in EXPORT_ALL.read_text(encoding="utf-8", errors="replace").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]


if __name__ == "__main__":
    rows = migrate_results_to_export()
    print(f"导出目录: {EXPORT_DIR}")
    print(f"主文件:   {EXPORT_ALL}")
    print(f"共 {len(rows)} 条")
    for ln in rows:
        print(ln)
