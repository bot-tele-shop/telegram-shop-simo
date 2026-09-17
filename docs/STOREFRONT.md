# Native Telegram storefront

## What changed

The storefront is a native Telegram welcome message with an HTML quote card and colored inline buttons. The menu is Products; Offers; Profile / Orders; Payments / Referrals; Support / API. `/start` and `/menu` also install a persistent quick-access keyboard for Products, Support, Payments and API. `/shop` opens the product catalog directly.

The Cloudflare Worker catalog renders eight real, in-stock products per page with Previous / Next navigation. Products still enter the existing terms gate, Telegram Stars invoice, atomic stock allocation and delivery flow. Empty catalog, profile and order screens now remain useful instead of looking broken.

Orders have In progress, Completed, Needs attention and All filters. Profile totals are computed from that buyer's own order history; no balance or purchase is fabricated.

This patch updates **both existing runtimes**, `shop/` and `worker/`. Now that the repository is available, reusing its existing catalog, payment and order handlers avoids another disconnected storefront. The experimental `reseller-backend/` PostgreSQL foundation remains separate and unchanged; its payment/storage logic has not been merged into either runtime.

## Honest feature status

- Products and Orders use the existing runtime's catalog and buyer-scoped order records.
- Profile displays the requesting user's Telegram identity and real delivered-order totals, not a made-up wallet balance.
- Payments explains existing per-order Telegram Stars checkout. It does not implement USDT deposits or a custodial Stars wallet.
- Support uses your existing merchant support setting.
- Offers reports that no promotions are configured. Referrals and customer API access explicitly say they are not enabled. No commissions, API keys or invented products are generated.
- The screenshot's warranty claim is deliberately not copied: warranty promises require actual merchant/supplier terms.

The shared, dependency-free presentation module is mirrored in `shop/storefront.py` and `worker/src/storefront.py` because the runtimes are packaged separately. A test enforces byte-for-byte parity. Shop names are HTML-escaped. Telegram controls wallpaper, fonts and final button rendering; this is a layout match, not a promise of pixel-identical client styling.

## Configuration and verification

Keep your existing shop name and support settings: `shop_name` / `support_contact` locally, `SHOP_NAME` / `SUPPORT_CONTACT` for the Worker. No new credentials or database migrations are needed for this UI.

Run `python -m pytest tests -q` after installing the locked development environment. Validation on 2026-09-18: **767 passed, 1 skipped**, including **47 focused storefront tests**. The Worker also passes a Wrangler 4.132.0 deployment dry run. Tests use synthetic data and mocked network transports, not Telegram, Cloudflare, Supabase or Canboso accounts. Cloud UI tests do not prove the production catalog contains products.

For a separate test bot, verify `/start`, each menu button, `/shop`, buyer-specific Orders and existing Stars checkout. Existing production/supplier launch prerequisites still apply. This change does not add automatic Canboso purchasing to the cloud runtime, nor constitute a payment or fulfillment security audit.

The development branch is not a deployment. The repository documents automatic deployment when `worker/` changes reach `main`; do not merge or deploy without approval and a separate launch review.
