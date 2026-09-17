# Owner dashboard runbook

URL: https://digital-shelf-admin.pages.dev (Cloudflare Pages project
`digital-shelf-admin`, auto-deployed from `dashboard/` on every green main push).

## Sign in
Supabase Auth account whose email is listed in the `OWNER_EMAILS` Worker
secret (sourced from the `DASHBOARD_OWNER_EMAILS` GitHub variable). Every
admin request is re-authorized server-side; a logged-out or non-owner session
gets 401/403 and can do nothing.

## Daily flow
1. **Products** → create (stays inactive).
2. **Stock** → pick the SKU, paste codes/URLs (one per line, ≤500), upload.
   Lines are encrypted in the Worker before they touch the database; the
   dashboard only ever shows counts, never codes.
3. **Products** → Activate. The product appears in the Telegram catalog.
4. **Orders** → search by ID/title/user, watch states:
   `delivering → delivered`, or `delivery_failed` (auto-notified; use Resend).
5. **Resend** re-sends the SAME assigned code — never allocates new stock.
6. **Refund** asks for two confirmations, calls Telegram `refundStarPayment`,
   then records the refund and quarantines the code so it is never resold.
   If Telegram rejects the call, nothing is recorded — retry from the order.
7. **Settings** → store name, support contact, terms/privacy text, and the
   checkout pause switch. Pausing stops new invoices; already-paid orders
   still deliver. Changing terms/privacy forces re-acceptance at checkout.

## Alerts (Overview)
- `review` payment events: paid but unexpected (wrong amount, unknown order,
  replayed charge). Investigate, then refund from the order if appropriate.
- Failed webhook updates: permanent processing failures (see Supabase
  `update_inbox.last_error`).

## Operations
- Webhook registration: run "Register Telegram webhook" (keeps pending
  updates; sends the secret-token header when `WEBHOOK_HEADER_SECRET` exists).
- Secrets: run "Sync Worker secrets" after changing any GitHub secret/var.
- Migrations: files in `supabase/migrations/` are applied via the Supabase
  management query endpoint with a personal access token (service-role keys
  are not migration credentials). Apply in filename order.
- Rollback: revert the merge commit on main; CI re-deploys the previous code.
  Database migrations are additive — no rollback needed for 0003/0004.

## Deferred on purpose
Offers, Referrals and API pages in the bot say plainly they are not enabled;
there are no wallet balances or rewards. Supplier purchasing is rejected
before payment until the pipeline is implemented.
