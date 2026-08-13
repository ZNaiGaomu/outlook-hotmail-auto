#!/usr/bin/env bash
# OutlookRegister 启动脚本
# 在 tmux 会话里用 xvfb-run 启动 main.py
#
# 用法：
#   bash run.sh           # 前台运行（Ctrl+C 退出）
#   bash run.sh --tmux    # 放到 tmux 会话 outlook 里常驻
#
# 需要先执行过 install.sh 并配置好 config.json。

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

if [[ ! -d .venv ]]; then
  echo "[run] 未找到 .venv，请先执行: bash install.sh" >&2
  exit 1
fi

if ! command -v xvfb-run >/dev/null 2>&1; then
  echo "[run] 未找到 xvfb-run，请先执行: bash install.sh" >&2
  exit 1
fi

if ! command -v patchright >/dev/null 2>&1 && [[ ! -f .venv/bin/patchright ]]; then
  echo "[run] 未找到 patchright 命令，请先执行: bash install.sh" >&2
  exit 1
fi

# shellcheck disable=SC1091
source .venv/bin/activate

PROXY=$(python - <<'PY'
import json
try:
    with open("config.json", "r", encoding="utf-8") as f:
        print((json.load(f).get("proxy") or "").strip())
except Exception:
    print("")
PY
)

if [[ -z "${PROXY}" ]]; then
  echo "[run] ⚠️ config.json 的 proxy 为空。云服务器直连注册成功率极低，建议先配置住宅代理。"
fi

CMD=(xvfb-run -a -s "-screen 0 1280x800x24" python main.py)

if [[ "${1:-}" == "--tmux" ]]; then
  if ! command -v tmux >/dev/null 2>&1; then
    echo "[run] 未安装 tmux，请: sudo apt install -y tmux" >&2
    exit 1
  fi
  SESSION="outlook"
  if tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "[run] tmux 会话 $SESSION 已存在，attach 查看："
    echo "      tmux attach -t $SESSION"
    exit 0
  fi
  tmux new -d -s "$SESSION" "cd '$PROJECT_DIR' && source .venv/bin/activate && ${CMD[*]}"
  echo "[run] ✅ 已在 tmux 会话 '$SESSION' 中启动。"
  echo "      查看:  tmux attach -t $SESSION"
  echo "      脱离:  Ctrl+b 然后 d"
else
  echo "[run] 前台启动: ${CMD[*]}"
  exec "${CMD[@]}"
fi
