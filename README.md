# Digital Shelf — Telegram Stars Shop

A Telegram shop bot selling digital goods for Telegram Stars.

Two runtimes, one codebase:

| Path | What it is | Where it runs |
|---|---|---|
| `shop/` | Full bot: polling mode, admin CLI, encrypted SQLite stock, Canboso supplier pipeline, 656 tests | Any server / locally |
| `worker/` | Webhook buyer flow backed by Supabase Postgres | Cloudflare Workers |
| `supabase/` | Database migrations (schema + atomic fulfillment RPCs) | Supabase |
| `.github/workflows/` | CI + automatic Cloudflare deploy + secret sync | GitHub Actions |

## Quick start (local bot)

```bash
pip install -r requirements.txt
python -m shop init        # creates config.local.json with a fresh encryption key
python -m shop doctor      # validates local setup
python -m shop demo        # offline checkout/delivery/refund simulation
python -m shop run         # long-polling bot (needs bot_token etc. in config)
```

See `LAUNCH.md` for the full go-live checklist.

## Cloud deployment (Cloudflare + Supabase + GitHub Actions)

Every push to `main` that touches `worker/` deploys automatically to
Cloudflare Workers. See `docs/DEPLOYMENT.md` for the one-time setup
(Cloudflare API token in GitHub secrets, Worker secrets sync, webhook
registration) and `tools/push_to_supabase.py` to seed Supabase with your
local catalog and encrypted stock.

## Rules that keep this project safe

- Never commit `config.local.json`, `.env`, or anything under `data/`
- The bot token, supplier API key, and Fernet key live in secrets only
- Stars refunds never auto-cancel supplier orders — resolve both sides
