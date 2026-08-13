# Cloudflare Temporary Mail

The headed edition can create a recovery mailbox when Microsoft asks for a secondary email. All endpoints and credentials come from your local `config.json`.

## Required fields

```json
{
  "cf_mail": {
    "enabled": true,
    "base_url": "https://your-temp-mail.example.com",
    "domain": "mail.example.com",
    "fallback_domain": "mail.example.com",
    "site_password": "",
    "admin_password": ""
  }
}
```

Environment variable overrides:

| Variable | Maps to |
| --- | --- |
| `CF_MAIL_BASE` | `base_url` |
| `CF_EMAIL_DOMAIN` | `domain` |
| `CF_MAIL_FALLBACK_DOMAIN` | `fallback_domain` |
| `CF_MAIL_SITE_PASSWORD` | `site_password` |
| `CF_MAIL_ADMIN_PASSWORD` | `admin_password` |

## Operator checklist

1. Deploy or reuse a [cloudflare_temp_email](https://github.com/dreamhunter2333/cloudflare_temp_email) instance.
2. Add your receiving domain in the admin panel.
3. Point Email Routing catch-all to that worker.
4. Keep site / admin passwords in `config.json` only. Do not commit them.

The API strips `+` from local parts, so the client creates random alphanumeric addresses instead of plus-addressing.
