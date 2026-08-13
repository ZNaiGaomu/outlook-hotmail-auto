# Ubuntu 云服务器部署指南

**先读这段重要警告再决定要不要上云。**

## ⚠️ 云服务器的致命短板：IP

`outlook.live.com` 对**机房 IP（datacenter IP）风控极高**。阿里云 / 腾讯云 / AWS / GCP / Azure / Oracle 的公网出口 IP 都被 Microsoft 标记。
直接用云服务器 IP 访问注册页，多半在 `goto` 阶段就报：

```text
[Error: IP] - IP质量不佳，无法进入注册界面。
```

**唯一可行方案**：云服务器 → **住宅代理池**出站（在 `config.json` 的 `proxy` 字段配置）。
常见付费住宅代理服务：roxlabs、iproyal、brightdata、smartproxy、lumiproxy…

如果不接住宅代理，下面所有步骤都是白干，直接回到本地跑。

---

## 方案 A：原生部署（推荐，更灵活）

### 1. 系统与 Python

推荐 Ubuntu 22.04 LTS 或 24.04 LTS。

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git
python3 --version   # 需要 3.10+
```

### 2. 安装 Chromium 运行时依赖 + 虚拟显示 Xvfb

`patchright/playwright` 驱动的 chromium 在 Linux 上需要一堆共享库。作者要求**不能用无头模式**，所以要 Xvfb 提供虚拟显示。

```bash
sudo apt install -y \
    xvfb \
    ca-certificates fonts-liberation fonts-noto-cjk \
    libasound2 libatk-bridge2.0-0 libatk1.0-0 libatspi2.0-0 \
    libcairo2 libcups2 libdbus-1-3 libdrm2 libgbm1 libglib2.0-0 \
    libgtk-3-0 libnspr4 libnss3 libpango-1.0-0 \
    libx11-6 libx11-xcb1 libxcb1 libxcomposite1 libxdamage1 \
    libxext6 libxfixes3 libxkbcommon0 libxrandr2 libxshmfence1 \
    wget curl
```

### 3. 拉项目 + 装 Python 依赖

```bash
cd OutlookRegister

python3 -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt
patchright install chromium
```

### 4. 配置 `config.json`

关键是代理：必须是**住宅代理**，不要直连。

```json
{
    "choose_browser": "patchright",
    "email_suffix": "@outlook.com",
    "proxy": "http://USER:PASS@gate.residential.example.com:8000",
    "bot_protection_wait": 20,
    "max_captcha_retries": 2,
    "concurrent_flows": 1,
    "max_tasks": 1,
    ...
}
```

建议第一次部署先用 `concurrent_flows=1, max_tasks=1` 跑通一次，再逐步放量。

### 5. 用 Xvfb 启动（关键，不是直接 `python main.py`）

```bash
# 当前 shell 一次性运行：
xvfb-run -a -s "-screen 0 1280x800x24" python main.py
```

**常驻后台推荐用 tmux**：

```bash
sudo apt install -y tmux
tmux new -s outlook
# 进入 tmux 后：
cd ~/OutlookRegister
source .venv/bin/activate
xvfb-run -a -s "-screen 0 1280x800x24" python main.py
# 脱离：Ctrl+b 然后 d
# 回来看：tmux attach -t outlook
```

或者做成 systemd 服务（见最后「附录：systemd」）。

### 6. 看结果

成功/失败邮箱写在：

```text
Results/unlogged_email.txt   # 不需要 OAuth2 时
Results/logged_email.txt     # 开了 OAuth2 时
Results/outlook_token.txt    # OAuth2 token
```

---

## 方案 B：Docker

项目已经给你准备了 `Dockerfile`（`xvfb-run` + chromium + patchright 都装好了）。

```bash
# 在云服务器上
cd OutlookRegister

# 编辑 config.json，把 proxy 填成住宅代理
vim config.json

docker build -t outlook-register .

mkdir -p Results
docker run --rm -it \
    -v "$PWD/config.json:/app/config.json:ro" \
    -v "$PWD/Results:/app/Results" \
    --name outlook-register \
    outlook-register
```

想常驻：

```bash
docker run -d --restart unless-stopped \
    -v "$PWD/config.json:/app/config.json:ro" \
    -v "$PWD/Results:/app/Results" \
    --name outlook-register \
    outlook-register
docker logs -f outlook-register
```

---

## 排错

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `启动浏览器失败: ... Connection closed while reading from the driver` | chromium 没装好 | `patchright install chromium` |
| `[Error: IP] - IP质量不佳，无法进入注册界面` | 当前出口 IP 被风控 | 换代理 / 换住宅节点，不要直连云 IP |
| `[Error: IP or browser] - 当前IP注册频率过快` | 同 IP 短时间重复注册被限速 | 降并发、增大 `bot_protection_wait`、换 IP 节点 |
| `'bool' object has no attribute 'new_context'` | launch_browser 返回 False，通常是上一条的连锁 | 修复 launch 错误即可 |
| 容器里没东西显示/卡死 | 忘记用 `xvfb-run` | 入口命令必须是 `xvfb-run ... python main.py` |
| `Missing dependencies: libXXX.so.N` | 系统依赖没装齐 | 回到步骤 2 重装 |

---

## 附录：systemd 常驻服务（方案 A 可选）

`/etc/systemd/system/outlook-register.service`：

```ini
[Unit]
Description=OutlookRegister
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/OutlookRegister
Environment=DISPLAY=:99
ExecStart=/usr/bin/xvfb-run -a -s "-screen 0 1280x800x24" /home/ubuntu/OutlookRegister/.venv/bin/python /home/ubuntu/OutlookRegister/main.py
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
```

启用：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now outlook-register
sudo systemctl status outlook-register
journalctl -u outlook-register -f
```

---

## 最后再提醒一遍

- **住宅代理是硬性前提**。没有住宅代理，云服务器上这玩意跑不出来。
- 先 `concurrent_flows=1, max_tasks=1` 跑通单次再放量。
- 同一批住宅节点不要短时间内反复用，注意节点轮换。
