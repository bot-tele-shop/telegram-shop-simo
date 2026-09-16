# Deployment — Cloudflare Workers + Supabase + GitHub Actions

One-time setup. After this, every push to `main` deploys automatically.

## 1. Repository

The code lives at `github.com/bot-tele-shop/telegram-shop` (private).
The Tasklet GitHub app must be installed on the `bot-tele-shop` org with
access to this repo so the agent can push updates.

## 2. Supabase

Project: **telegram bot shop** (`lnaszistlucibbrcjewi`, eu-west-1).
Migrations in `supabase/migrations/` are already applied:

- `0001_init.sql` — tables (products, orders, stock, payments, refunds,
  webhook inbox, supplier pipeline) + atomic RPCs (`fulfill_order`,
  `record_refund`, `claim_update`) + RLS on everything (service-role only)
- `0002_catalog.sql` — `catalog_with_stock()` RPC

Seed catalog and encrypted stock from your local database:

```bash
export SUPABASE_URL="https://lnaszistlucibbrcjewi.supabase.co"
export SUPABASE_SERVICE_ROLE_KEY="..."   # Supabase dashboard -> Settings -> API -> service_role
python tools/push_to_supabase.py
```

## 3. GitHub secrets & variables

Repo -> Settings -> Secrets and variables -> Actions -> **Secrets** tab
(these are real credentials):

| Secret | Value |
|---|---|
| `CLOUDFLARE_API_TOKEN` | Cloudflare -> My Profile -> API Tokens -> "Edit Cloudflare Workers" template |
| `CLOUDFLARE_ACCOUNT_ID` | Cloudflare dashboard -> Workers -> right sidebar |
| `TELEGRAM_BOT_TOKEN` | @BotFather |
| `SUPABASE_SERVICE_ROLE_KEY` | Supabase dashboard -> Settings -> API |
| `STOCK_FERNET_KEY` | `stock_encryption_key` from `config.local.json` (must match, or codes won't decrypt) |
| `WEBHOOK_SECRET` | Random string, e.g. `openssl rand -hex 24` |
| `ADMIN_IDS` | Comma-separated numeric Telegram IDs |

Repo -> Settings -> Secrets and variables -> Actions -> **Variables** tab
(public legal copy, not credentials — same texts as `config.local.json`):

| Variable | Value |
|---|---|
| `TERMS_TEXT` | Terms of service text |
| `PRIVACY_TEXT` | Privacy policy text |

(The sync workflow also accepts these two as Secrets for backward
compatibility; Variables are preferred since the texts are public anyway.)

## 4. Workflows

- **CI** (`ci.yml`) — ruff + 656 tests + worker syntax check on every push/PR
- **Deploy to Cloudflare** (`deploy.yml`) — `wrangler deploy` on pushes to
  `main` that touch `worker/`, plus manual dispatch
- **Sync Worker secrets** (`secrets.yml`) — manual; copies the GitHub secrets
  and variables above into Cloudflare Worker secrets (run after
  adding/rotating any of them)

## 5. First deploy

1. Add all GitHub secrets (section 3)
2. Actions -> **Sync Worker secrets** -> Run workflow
3. Actions -> **Deploy to Cloudflare** -> Run workflow (or push to `main`)
4. Verify: `curl https://digital-shelf-bot.<account-subdomain>.workers.dev/health` -> `ok`
5. Register the Telegram webhook (once):

```bash
curl -X POST "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/setWebhook" \
  -d "url=https://digital-shelf-bot.<account-subdomain>.workers.dev/webhook/<WEBHOOK_SECRET>"
```

The bot then answers `/shop`, sells for Stars, delivers encrypted codes from
Supabase, and records refunds — no server to run.

## Notes

- The Worker path must be the ONLY live path: stop the local polling bot
  (`python -m shop run`) when the webhook is active — Telegram delivers each
  update to one destination only.
- Admin operations (import catalog/stock, supplier review) stay in the local
  CLI; push new stock to Supabase afterwards with `tools/push_to_supabase.py`.
- Editing `TERMS_TEXT`/`PRIVACY_TEXT` changes the terms version: buyers
  re-accept on next purchase, in both runtimes.
