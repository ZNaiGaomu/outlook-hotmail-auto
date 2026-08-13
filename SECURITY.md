# Security

This repository is intended to ship **redeployable source** only.

Do not open issues or pull requests that include:

- proxy usernames, passwords, or vendor session IDs
- Azure / Microsoft client secrets or refresh tokens
- generated Outlook / Hotmail passwords
- temp-mail admin passwords or private domains you do not want public

If a secret was committed by mistake, rotate it at the vendor immediately and treat the Git history as compromised.

Local files that must stay untracked:

- `config.json`
- `dyn_proxy_config.json`
- `resi_proxy_config.json`
- `Results/`
- `导出/`
