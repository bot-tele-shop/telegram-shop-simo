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

Merges to `main` deploy automatically — but only after lint and the full test
suite pass for that exact commit (see the `deploy` jobs in
`.github/workflows/ci.yml`). Pull requests never deploy. See
`docs/DEPLOYMENT.md` for the one-time setup and `tools/push_to_supabase.py`
to seed Supabase with your local catalog and encrypted stock.

## Owner dashboard

A private dashboard lives at https://digital-shelf-admin.pages.dev
(Cloudflare Pages, source in `dashboard/`). Sign in with the owner Supabase
account (email must be in the `OWNER_EMAILS` Worker secret). From it you can:

1. Create a product (always starts inactive)
2. Bulk-upload codes/download URLs on the Stock tab — encrypted server-side
3. Activate the product once stock is ready; it appears in the Telegram shop
4. Watch orders, resend a buyer's code (always the same code), or issue a
   double-confirmed Stars refund (the code is quarantined, never resold)
5. Pause checkout, edit store name/support/terms on the Settings tab
   (changing terms makes buyers re-accept at next checkout)

The dashboard API runs inside the same Worker under `/admin/api/*` and
validates the Supabase session and owner allowlist on every request.

## Rules that keep this project safe

- Never commit `config.local.json`, `.env`, or anything under `data/`
- The bot token, supplier API key, and Fernet key live in secrets only
- Stars refunds never auto-cancel supplier orders — resolve both sides
