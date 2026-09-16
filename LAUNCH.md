# Digital Shelf — Go-Live Checklist

Status: code complete, 656/656 tests green. Three values are missing before the bot can start.

## A. Connect the live bot

Missing values in `config.local.json`:

| Key | Where to get it |
|---|---|
| `bot_token` | @BotFather → `/mybots` → your bot → API Token (or `/newbot`) |
| `admin_ids` | Your numeric Telegram ID — message @userinfobot |
| `support_contact` | A handle buyers can reach, e.g. `@yourusername` |

Launch sequence:

1. Fill the three values above (test mode is already configured — safe to start).
2. `python -m shop doctor` — confirm zero `NEEDS SETUP` lines.
3. `python -m shop seed-demo` — load demo catalog to click through as a buyer.
4. `python -m shop run` — bot starts polling. Use `/whoami`, `/shop`, `/admin` in chat.
5. When ready for real money: set `"environment": "production"`,
   `"production_acknowledged": true`, `"enable_sales": true`, then restart.
   Keep the test-mode demo stock out of the production database.

Notes:
- Never commit `config.local.json`; it holds the stock encryption key.
- One polling process per database — the process lock enforces this.
- If the bot ever had a webhook configured, disable it via @BotFather first
  (the bot refuses to poll while a webhook is active, by design).

## B. Terms & privacy — DONE

Drafted and written into `config.local.json` (`terms_text`, `privacy_text`).
Buyers see them at `/terms`, before first checkout, and must tap "I accept".
Editing either text changes the recorded `terms_version`, so existing buyers
re-accept on their next purchase — update the texts before launch, not after.

## C. Canboso auto-fulfillment

The pipeline is built and tested: read-only product/balance sync, purchase with
persisted idempotency body (never auto-retried), encrypted evidence storage,
supplier-review CLI, and admin-controlled retry/fulfill/refund resolution.
It is deliberately gated. Enable in stages:

**Stage 1 — read-only sync (safe, no purchases):**
1. Set `"canboso": { "enabled": true }` in `config.local.json`.
2. Provide the buyer API key via environment: `export CANBOSO_API_KEY=...`
   (never in chat, logs, or the catalog).
3. Stop the bot, then: `python -m shop supplier-sync --output reports/sync-1.json`
   to cache products and wallet balance locally.

**Stage 2 — live purchases (only after stage 1 looks right):**
Set all of the following, then restart the bot:
- `"environment": "production"` (purchases are blocked in test mode)
- `"canboso.resale_authorized": true` — you are authorized to resell the products
- `"canboso.allow_purchases": true`
- `"canboso.acknowledge_price_race": true` — the API has no server-side max-price
- `"canboso.budget_currency"`: `"VND"` or `"USD"` (as reported by your wallet)
- `"canboso.spend_budget"`: positive cumulative cap, e.g. `"500"`

Ongoing operations:
- `/supplierreview` in chat, or `python -m shop supplier-review` — interrupted purchases
- `python -m shop supplier-inspect ORDER --output FILE --confirm-sensitive-export`
- `python -m shop supplier-resolve ORDER retry_same_request|stop_for_refund|fulfill ...`
- A Stars refund never cancels the supplier order — resolve both sides deliberately.
