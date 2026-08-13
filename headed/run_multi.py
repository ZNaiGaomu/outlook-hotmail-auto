"""连续多轮注册尝试，带间隔，避免同 IP 连撞过猛。"""
from __future__ import annotations

import json
import time
from pathlib import Path

from config_store import load_config, save_config
from main import run_from_config

ROUNDS = 3
GAP_SEC = 25


def main() -> None:
    cfg = load_config()
    cfg["use_residential"] = True
    cfg["max_tasks"] = 1
    cfg["concurrent_flows"] = 1
    cfg["email_suffix"] = "@outlook.com"
    cfg["max_captcha_retries"] = 6
    save_config(cfg)

    print(f"将连续尝试 {ROUNDS} 轮，每轮 1 个号，间隔 {GAP_SEC}s")
    print("住宅代理:", cfg.get("proxy"))
    results = []
    for i in range(1, ROUNDS + 1):
        print("\n" + "=" * 60)
        print(f" ROUND {i}/{ROUNDS}")
        print("=" * 60)
        t0 = time.time()
        try:
            out = run_from_config(str(Path("config.json").resolve()))
        except Exception as e:
            out = {"error": f"{type(e).__name__}: {e}"}
        took = round(time.time() - t0, 1)
        results.append({"round": i, "took": took, "result": out})
        print(f"[round {i}] done in {took}s -> {out}")
        # 读 results 文件
        unlogged = Path("Results/unlogged_email.txt")
        if unlogged.exists():
            lines = [ln for ln in unlogged.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]
            print(f"[round {i}] unlogged_email count={len(lines)}")
            if lines:
                print("  last:", lines[-1])
        if i < ROUNDS:
            print(f"冷却 {GAP_SEC}s …")
            time.sleep(GAP_SEC)

    print("\n==== SUMMARY ====")
    print(json.dumps(results, ensure_ascii=False, indent=2))
    unlogged = Path("Results/unlogged_email.txt")
    if unlogged.exists():
        lines = [ln for ln in unlogged.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]
        print(f"成功账号总数: {len(lines)}")
        for ln in lines[-5:]:
            print(" ", ln)
    else:
        print("成功账号总数: 0")


if __name__ == "__main__":
    main()
