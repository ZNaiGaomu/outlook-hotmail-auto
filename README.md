# Outlook / Hotmail Auto

[![Release](https://img.shields.io/github/v/release/ZNaiGaomu/outlook-hotmail-auto)](https://github.com/ZNaiGaomu/outlook-hotmail-auto/releases)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

A self-hosted Outlook / Hotmail provisioning toolkit. It drives a real Chromium session, fills the Microsoft signup flow, optionally completes recovery-mail verification, and can request OAuth2 tokens for IMAP / SMTP after the mailbox is created.

This repository ships **source only**. Proxy accounts, Azure client IDs, mailbox admin passwords, and generated credentials stay on your machine.

[English](#english) · [中文](#中文)

---

## English

### What you get

| Edition | Path | When to use |
| --- | --- | --- |
| **Headed browser** (recommended) | [`headed/`](headed/) | Local Windows / macOS, or a Linux host with Xvfb. Includes the dashboard, residential / dynamic proxy bridges, Cloudflare temp-mail recovery, and OAuth2 export. |
| **Headless / protocol** | [`headless/`](headless/) | Leaner server footprint. Same signup core, fewer operator tools. |

Shared capabilities:

- Patchright (recommended) or Playwright Chromium
- Residential / sticky-session HTTP or SOCKS proxies
- Optional OAuth2 authorization-code + PKCE (`refresh_token` / `access_token`)
- IP and signup-page diagnostics
- Native Ubuntu installer, `tmux` runner, and Docker image

### Repository layout

```text
.
├── headed/                 # full operator edition
│   ├── config.example.json
│   ├── dashboard.py        # http://127.0.0.1:8765
│   ├── main.py
│   ├── setup_config.py
│   └── docs/cf-temp-mail.md
├── headless/               # compact edition
│   ├── config.example.json
│   └── main.py
├── CHANGELOG.md
└── LICENSE
```

Runtime files such as `config.json`, `Results/`, and exported mailboxes are git-ignored.

### Requirements

- Python 3.10+
- A **residential** egress. Datacenter IPs from typical cloud VPS providers are almost always rejected by `outlook.live.com`.
- Optional: Clash / V2 on `127.0.0.1:7890` if your proxy vendor only accepts a specific front hop
- Optional: your own Cloudflare temp-mail deployment for recovery-email challenges

### Quick start (headed)

```bash
git clone https://github.com/ZNaiGaomu/outlook-hotmail-auto.git
cd outlook-hotmail-auto/headed

python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux / macOS: source .venv/bin/activate

pip install -r requirements.txt
patchright install chromium

python setup_config.py
# then edit config.json — proxy, OAuth, temp-mail
```

Windows:

```bat
python dashboard.py
```

or `start_dashboard.bat`. Open http://127.0.0.1:8765

CLI:

```bash
python main.py
```

Linux server:

```bash
bash install.sh
# edit config.json
bash run.sh          # foreground via Xvfb
bash run.sh --tmux   # detach into session "outlook"
```

Docker:

```bash
docker build -t outlook-hotmail-auto .
docker run --rm -it \
  -v "$PWD/config.json:/app/config.json:ro" \
  -v "$PWD/Results:/app/Results" \
  outlook-hotmail-auto
```

See [`headed/DEPLOY.md`](headed/DEPLOY.md) for systemd and troubleshooting.

### Configuration

Copy the examples, never commit the filled files.

| File | Purpose |
| --- | --- |
| `config.json` | Browser engine, mailbox suffix, concurrency, OAuth2, temp-mail |
| `resi_proxy_config.json` | Upstream residential node for the `17890` local HTTP bridge |
| `dyn_proxy_config.json` | Dynamic / whitelist node for the `17990` local HTTP bridge |

`config.json` essentials:

| Key | Notes |
| --- | --- |
| `choose_browser` | `patchright` (recommended) or `playwright` |
| `email_suffix` | `@outlook.com` or `@hotmail.com` |
| `proxy` / `residential_proxy` / `dynamic_proxy` | Your own vendor URL. Format `http://user:pass@host:port` or `socks5://user:pass@host:port` |
| `concurrent_flows` / `max_tasks` | Start with `1` / `1` |
| `oauth2.client_id` | Your Azure app ID. Leave empty if you do not need tokens |
| `cf_mail.*` | Your temp-mail base URL and domains. See [`headed/docs/cf-temp-mail.md`](headed/docs/cf-temp-mail.md) |

OAuth2 setup, if needed:

1. Register an application in [Azure Portal](https://portal.azure.com/)
2. Use redirect URI `https://login.microsoftonline.com/common/oauth2/nativeclient`
3. Request `offline_access` plus the Outlook IMAP / SMTP scopes you actually need
4. Put the client ID in `config.json` and set `enable_oauth2` to `true`

### Outputs

Created only on the machine that runs the tool:

| Path | Content |
| --- | --- |
| `Results/unlogged_email.txt` | mailbox + password |
| `Results/logged_email.txt` | mailbox + password after OAuth2 |
| `Results/outlook_token.txt` | refresh / access tokens |
| `导出/` (headed) | normalized `email----password----client_id----token` export |

### Security

- This release contains **no** live proxies, Azure secrets, mailbox passwords, or refresh tokens.
- Keep `config.json`, `*proxy_config.json`, `Results/`, and `导出/` local.
- Rotate any credential that ever lived in an older private copy of this project.

### Disclaimer

Automating Microsoft account creation may violate Microsoft service terms and local law. You are responsible for using this software only on systems and identities you are authorized to operate. The authors are not liable for account bans, ToS enforcement, or misuse.

---

## 中文

面向 Outlook / Hotmail 的自托管注册工具包：用真实 Chromium 走完注册页，可选处理备用邮箱验证，并在建号后按需申请 OAuth2 Token。

仓库只公开**可再次部署的源码**。代理账密、Azure Client ID、临时邮箱后台密码、以及跑出来的账号 / Token **都不会随仓库分发**。下载后把示例配置复制成本地文件，填入你自己的信息即可运行。

### 两个版本

| 版本 | 目录 | 适用场景 |
| --- | --- | --- |
| **有头浏览器（推荐）** | [`headed/`](headed/) | 本地 Windows / macOS，或带 Xvfb 的 Linux。含管理面板、住宅 / 动态代理桥、Cloudflare 临时邮箱、OAuth2 导出。 |
| **无头 / 协议版** | [`headless/`](headless/) | 更轻的服务器部署，核心注册流程相同。 |

### 快速开始（有头版）

```bash
git clone https://github.com/ZNaiGaomu/outlook-hotmail-auto.git
cd outlook-hotmail-auto/headed
python -m venv .venv
pip install -r requirements.txt
patchright install chromium
python setup_config.py
```

然后编辑 `config.json`，填入**你自己的**住宅代理、OAuth 与临时邮箱信息。

```bash
python dashboard.py    # 面板 http://127.0.0.1:8765
python main.py         # 命令行
```

Linux 服务器：`bash install.sh` 后执行 `bash run.sh` 或 `bash run.sh --tmux`。详细步骤见 [`headed/DEPLOY.md`](headed/DEPLOY.md)。

### 配置原则

1. 用 `setup_config.py` 从 `*.example.json` 生成本地文件。
2. 只改本地 `config.json` / `resi_proxy_config.json` / `dyn_proxy_config.json`。
3. 不要把填好的配置、`Results/`、`导出/` 提交回 Git。
4. 机房 IP 基本无法进入注册页，必须使用住宅出口。
5. 第一次请用 `concurrent_flows=1`、`max_tasks=1` 跑通再放量。

### 合规说明

批量或自动化创建 Microsoft 账户可能违反服务条款或当地法规。请仅在你有权操作的环境中使用，并自行承担合规责任。

---

## Version

Current release: **v1.0.0** — first public source drop. See [CHANGELOG.md](CHANGELOG.md) and [Releases](https://github.com/ZNaiGaomu/outlook-hotmail-auto/releases).
