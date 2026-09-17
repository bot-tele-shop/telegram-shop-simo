# Screenshot-inspired Telegram storefront

## What changed

The reference image is implemented as a native Telegram welcome message with an HTML quote card and green (`success`) inline buttons. The row layout is Products; Offers; Profile / Orders; Payments / Referrals; Support / API. `/start` and `/menu` open it; `/shop` still opens the existing product catalog directly.

This patch updates **both existing runtimes**, `shop/` and `worker/`. Now that the repository is available, reusing its existing catalog, payment and order handlers avoids another disconnected storefront. The experimental `reseller-backend/` PostgreSQL foundation remains separate and unchanged; its payment/storage logic has not been merged into either runtime.

## Honest feature status

- Products and Orders use the existing runtime's catalog and buyer-scoped order records.
- Profile displays the requesting user's Telegram ID and payment information, not a made-up wallet balance.
- Payments explains existing per-order Telegram Stars checkout. It does not implement USDT deposits or a custodial Stars wallet.
- Support uses your existing merchant support setting.
- Offers reports that no promotions are configured. Referrals and customer API access explicitly say they are not enabled. No commissions, API keys or invented products are generated.
- The screenshot's warranty claim is deliberately not copied: warranty promises require actual merchant/supplier terms.

The shared, dependency-free presentation module is mirrored in `shop/storefront.py` and `worker/src/storefront.py` because the runtimes are packaged separately. A test enforces byte-for-byte parity. Shop names are HTML-escaped. Telegram controls wallpaper, fonts and final button rendering; this is a layout match, not a promise of pixel-identical client styling.

## Configuration and verification

Keep your existing shop name and support settings: `shop_name` / `support_contact` locally, `SHOP_NAME` / `SUPPORT_CONTACT` for the Worker. No new credentials or database migrations are needed for this UI.

Run `python -m pytest tests -q` after installing `requirements-dev.txt`. Validation on 2026-09-16: **697 passed**, including **41 new storefront tests**. Tests use synthetic data and mocked network transports, not Telegram, Cloudflare, Supabase or Canboso accounts. Cloud UI tests do not validate a deployed Cloudflare runtime.

For a separate test bot, verify `/start`, each menu button, `/shop`, buyer-specific Orders and existing Stars checkout. Existing production/supplier launch prerequisites still apply. This change does not add automatic Canboso purchasing to the cloud runtime, nor constitute a payment or fulfillment security audit.

The development branch is not a deployment. The repository documents automatic deployment when `worker/` changes reach `main`; do not merge or deploy without approval and a separate launch review.
