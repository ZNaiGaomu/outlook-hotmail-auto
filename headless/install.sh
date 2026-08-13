#!/usr/bin/env bash
# OutlookRegister - Ubuntu 一键装依赖脚本
# 用法（在项目根目录，服务器上执行一次即可）：
#   bash install.sh
#
# 做了什么：
# 1. apt 装 chromium 运行时依赖 + Xvfb + tmux + python3-venv
# 2. 创建 .venv 虚拟环境
# 3. pip 装 Python 依赖
# 4. patchright install chromium
#
# 不做什么：
# - 不修改 config.json（代理你自己填）
# - 不启动程序

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

echo "[install] 项目目录: $PROJECT_DIR"

if [[ $EUID -ne 0 ]]; then
  SUDO="sudo"
else
  SUDO=""
fi

echo "[install] 1/4 更新 apt 并安装系统依赖..."
$SUDO apt-get update

# Ubuntu 24.04 把 libasound2 改名为 libasound2t64，这里兼容处理
ASOUND_PKG="libasound2"
if apt-cache show libasound2t64 >/dev/null 2>&1; then
  ASOUND_PKG="libasound2t64"
fi

$SUDO apt-get install -y --no-install-recommends \
    python3 python3-venv python3-pip git tmux curl wget \
    xvfb ca-certificates fonts-liberation fonts-noto-cjk \
    "$ASOUND_PKG" libatk-bridge2.0-0 libatk1.0-0 libatspi2.0-0 \
    libcairo2 libcups2 libdbus-1-3 libdrm2 libgbm1 libglib2.0-0 \
    libgtk-3-0 libnspr4 libnss3 libpango-1.0-0 \
    libx11-6 libx11-xcb1 libxcb1 libxcomposite1 libxdamage1 \
    libxext6 libxfixes3 libxkbcommon0 libxrandr2 libxshmfence1

echo "[install] 2/4 创建 Python 虚拟环境 .venv..."
if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip

echo "[install] 3/4 安装 Python 依赖..."
pip install -r requirements.txt

echo "[install] 4/4 下载 chromium..."
patchright install chromium

mkdir -p Results

echo ""
echo "[install] ✅ 完成。"
echo ""
echo "下一步："
echo "  1. python setup_config.py"
echo "  2. 编辑 config.json，把 proxy 改成你自己的【住宅代理】地址"
echo "  3. 运行: bash run.sh"
