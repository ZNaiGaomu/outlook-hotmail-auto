"""Create local config files from examples. Never overwrites existing secrets."""

from __future__ import annotations

import shutil
from pathlib import Path

DIR = Path(__file__).resolve().parent
PAIRS = (
    ("config.example.json", "config.json"),
    ("dyn_proxy_config.example.json", "dyn_proxy_config.json"),
    ("resi_proxy_config.example.json", "resi_proxy_config.json"),
)


def main() -> int:
    created = 0
    for src_name, dst_name in PAIRS:
        src = DIR / src_name
        dst = DIR / dst_name
        if not src.is_file():
            print(f"[setup] missing {src_name}")
            continue
        if dst.exists():
            print(f"[setup] keep existing {dst_name}")
            continue
        shutil.copyfile(src, dst)
        print(f"[setup] created {dst_name} from {src_name}")
        created += 1
    (DIR / "Results").mkdir(exist_ok=True)
    print("[setup] next: edit config.json and fill in your own proxy / OAuth / mailbox settings")
    return 0 if created or all((DIR / d).exists() for _, d in PAIRS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
