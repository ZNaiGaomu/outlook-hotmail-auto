# OutlookRegister - 云服务器/容器镜像
#
# 注意：
# 1. outlook.live.com 对机房 IP 风控极高，容器/云主机的出口 IP 基本不能直接用。
#    你必须通过住宅代理池把出口改成住宅 IP（在 config.json 的 proxy 字段填写）。
# 2. 作者明确要求"关闭无头模式"。本镜像使用 Xvfb 提供虚拟显示，让浏览器在非 headless 模式运行。
# 3. 默认 Python 3.11，基于 Debian slim。需要 root 在构建时安装系统依赖。
#
# 构建：
#   docker build -t outlook-register .
# 运行（挂载 config 与 Results）：
#   docker run --rm -it \
#     -v "$PWD/config.json:/app/config.json:ro" \
#     -v "$PWD/Results:/app/Results" \
#     outlook-register

FROM python:3.11-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DISPLAY=:99

WORKDIR /app

# patchright/playwright 运行 chromium 需要的一堆系统库 + Xvfb 虚拟显示
RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb \
        ca-certificates \
        fonts-liberation \
        fonts-noto-cjk \
        libasound2 \
        libatk-bridge2.0-0 \
        libatk1.0-0 \
        libatspi2.0-0 \
        libcairo2 \
        libcups2 \
        libdbus-1-3 \
        libdrm2 \
        libgbm1 \
        libglib2.0-0 \
        libgtk-3-0 \
        libnspr4 \
        libnss3 \
        libpango-1.0-0 \
        libx11-6 \
        libx11-xcb1 \
        libxcb1 \
        libxcomposite1 \
        libxdamage1 \
        libxext6 \
        libxfixes3 \
        libxkbcommon0 \
        libxrandr2 \
        libxshmfence1 \
        wget \
        curl \
    && rm -rf /var/lib/apt/lists/*

# 先复制 requirements 以利用 Docker 层缓存
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 下载 chromium（patchright 会单独管理自己的 chromium）
RUN patchright install chromium

# 复制源码
COPY . .

# 创建 Results 目录（也可从宿主机挂载覆盖）
RUN mkdir -p /app/Results

# 入口：启动 Xvfb 后执行 main.py
# 如需更换入口脚本，可通过 docker run ... <其他命令> 覆盖
CMD ["xvfb-run", "-a", "-s", "-screen 0 1280x800x24", "python", "main.py"]
