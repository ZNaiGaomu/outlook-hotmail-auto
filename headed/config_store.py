"""读写 config.json，解析住宅/动态 IP 开关对应的实际 proxy。"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

DIR = Path(__file__).resolve().parent
CONFIG_PATH = DIR / "config.json"
_lock = threading.RLock()

# 动态 IP：Chromium 走本地 HTTP 桥（带账密的 SOCKS5 不能直接交给 Chromium）
# 上游账号只允许写在本地 config.json，禁止把真实凭据写进源码。
DEFAULT_DYNAMIC_RAW = ""
DEFAULT_DYNAMIC_LOCAL = "http://127.0.0.1:17990"
DEFAULT_RESI_LOCAL = "http://127.0.0.1:17890"


def load_config() -> dict[str, Any]:
    with _lock:
        raw = CONFIG_PATH.read_text(encoding="utf-8")
        return json.loads(raw)


def save_config(data: dict[str, Any]) -> dict[str, Any]:
    """写入配置；按流量模式同步 proxy，供 main/controller 直接读取。

    优先级：
      use_dynamic=True  → proxy = dynamic_local_http (默认 127.0.0.1:17990)
      use_residential   → proxy = residential_proxy
      都关              → proxy = "" 本机直连
    动态与住宅互斥：开动态时关住宅。
    """
    with _lock:
        data = dict(data)

        # 规范化字段
        dyn_raw = (data.get("dynamic_proxy") or "").strip() or DEFAULT_DYNAMIC_RAW
        dyn_local = (data.get("dynamic_local_http") or "").strip() or DEFAULT_DYNAMIC_LOCAL
        data["dynamic_proxy"] = dyn_raw
        data["dynamic_local_http"] = dyn_local

        resi = (data.get("residential_proxy") or "").strip()
        if not resi:
            resi = DEFAULT_RESI_LOCAL
        data["residential_proxy"] = resi

        use_dyn = bool(data.get("use_dynamic", False))
        use_res = bool(data.get("use_residential", False))

        # 互斥：动态优先（用户说后面主要用动态）
        if use_dyn:
            use_res = False
            data["proxy"] = dyn_local
        elif use_res:
            data["proxy"] = resi
        else:
            data["proxy"] = ""

        data["use_dynamic"] = use_dyn
        data["use_residential"] = use_res

        CONFIG_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=4) + "\n",
            encoding="utf-8",
        )
        return data


def set_residential(enabled: bool) -> dict[str, Any]:
    cfg = load_config()
    enabled = bool(enabled)
    cfg["use_residential"] = enabled
    if enabled:
        cfg["use_dynamic"] = False
    return save_config(cfg)


def set_dynamic(enabled: bool) -> dict[str, Any]:
    cfg = load_config()
    enabled = bool(enabled)
    cfg["use_dynamic"] = enabled
    if enabled:
        cfg["use_residential"] = False
    return save_config(cfg)


def set_traffic_mode(mode: str) -> dict[str, Any]:
    """mode: dynamic | residential | direct"""
    cfg = load_config()
    m = (mode or "direct").strip().lower()
    if m == "dynamic":
        cfg["use_dynamic"] = True
        cfg["use_residential"] = False
    elif m in ("residential", "resi"):
        cfg["use_dynamic"] = False
        cfg["use_residential"] = True
    else:
        cfg["use_dynamic"] = False
        cfg["use_residential"] = False
    return save_config(cfg)


def effective_proxy(cfg: dict[str, Any] | None = None) -> str:
    cfg = cfg or load_config()
    if bool(cfg.get("use_dynamic", False)):
        return (cfg.get("dynamic_local_http") or DEFAULT_DYNAMIC_LOCAL).strip()
    if bool(cfg.get("use_residential", True)):
        return (cfg.get("residential_proxy") or cfg.get("proxy") or "").strip()
    return ""


def traffic_mode(cfg: dict[str, Any] | None = None) -> str:
    cfg = cfg or load_config()
    if bool(cfg.get("use_dynamic", False)):
        return "dynamic"
    if bool(cfg.get("use_residential", False)):
        return "residential"
    return "direct"


def public_view(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = cfg or load_config()
    mode = traffic_mode(cfg)
    return {
        "use_residential": bool(cfg.get("use_residential", False)),
        "use_dynamic": bool(cfg.get("use_dynamic", False)),
        "traffic_mode": mode,
        "residential_proxy": cfg.get("residential_proxy") or "",
        "dynamic_proxy": cfg.get("dynamic_proxy") or "",
        "dynamic_local_http": cfg.get("dynamic_local_http") or DEFAULT_DYNAMIC_LOCAL,
        "effective_proxy": effective_proxy(cfg) or "(本机直连 / 机房流量)",
        "incognito": bool(cfg.get("incognito", True)),
        "email_suffix": cfg.get("email_suffix"),
        "choose_browser": cfg.get("choose_browser"),
        "concurrent_flows": cfg.get("concurrent_flows"),
        "max_tasks": cfg.get("max_tasks"),
        "bot_protection_wait": cfg.get("bot_protection_wait"),
    }
