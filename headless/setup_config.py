"""Create local config.json from the example. Never overwrites existing secrets."""

from __future__ import annotations

import shutil
from pathlib import Path

DIR = Path(__file__).resolve().parent


def main() -> int:
    src = DIR / "config.example.json"
    dst = DIR / "config.json"
    if not src.is_file():
        print("[setup] missing config.example.json")
        return 1
    if dst.exists():
        print("[setup] keep existing config.json")
    else:
        shutil.copyfile(src, dst)
        print("[setup] created config.json from config.example.json")
    (DIR / "Results").mkdir(exist_ok=True)
    print("[setup] next: edit config.json and fill in your own proxy / OAuth settings")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
