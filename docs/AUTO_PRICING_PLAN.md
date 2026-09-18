# Auto-Pricing Plan — Supplier-Linked Sell Prices

Goal: when a supplier's price moves, the shop's sell price follows a rule
(markup %, floor, jump clamp) instead of staying stale. Manual override always
wins. Existing preflight caps (`max_cost`) stay as the hard safety net.

## Current state (verified)

- `shop/supplier_worker.py::synchronize()` periodically pulls supplier
  products + balance and calls `supplier.cache_snapshot(products, balance)`.
- `shop/supplier_store.py::_check_quote()` blocks any purchase where the
  supplier's live price exceeds the product's approved `max_cost`.
- Sell prices live in the local products table, set by `Store.upsert_product()`
  — fully manual today.
- Admin surface: `python -m shop` CLI + `dashboard/` static app.

## Phase 1 — Pricing rules in the schema

1. Migration: add to products table (or side table `pricing_rules`):
   - `pricing_mode` TEXT: `manual` (default) | `auto`
   - `markup_pct` INTEGER (e.g. 50 = sell at 1.5x cost)
   - `min_profit` INTEGER (minor units) — never sell below cost + this
   - `max_jump_pct` INTEGER — if one sync moves cost more than this %,
     clamp the sell-price change and flag for review instead of applying fully
2. Backfill: all existing products `manual`, zero behavior change.

## Phase 2 — Repricer on sync

3. New module `shop/pricing.py`:
   - `reprice(snapshot_products) -> list[PriceChange]`
   - For each local product with `pricing_mode = auto` and a supplier mapping:
     - `new_price = max(cost * (1 + markup_pct/100), cost + min_profit)`
     - if `|new_price - old_price| / old_price > max_jump_pct`: apply clamped
       change, emit `PriceChange(flagged=True)`
     - update `max_cost` in the supplier mapping to
       `cost * (1 + max_jump_pct/100)` so preflight still guards between syncs
4. Hook into `supplier_worker.synchronize()` right after `cache_snapshot`,
   inside the same failure-isolation pattern (repricer failure must never
   kill the sync loop).
5. Every change (applied or flagged) appended to a `price_events` table:
   sku, old_cost, new_cost, old_price, new_price, flagged, timestamp.

## Phase 3 — Admin control + alerts

6. CLI: `python -m shop pricing list|set <sku> --mode auto --markup 50
   --min-profit 0.20 --max-jump 25|review`
7. Alert on flagged jumps: admin Telegram message (reuse existing admin
   notify path) + row in dashboard.
8. Dashboard: pricing panel per product — mode toggle, markup input, last
   cost/price, event history.

## Phase 4 — Safety + tests

9. Tests (repo standard, alongside existing 656):
   - markup math incl. rounding to minor units
   - min_profit floor beats raw markup
   - jump clamp triggers + flagged event written
   - repricer exception does not break `synchronize()`
   - `max_cost` re-derivation keeps preflight blocking intact
10. Dry-run mode: `python -m shop pricing preview` shows what the next sync
    would change without touching the DB.

## Rollout

- Ship with all products `manual`; opt products into `auto` one by one.
- First week: `max_jump_pct` tight (10–15%), review every flag.
- Later: multi-supplier cheapest-wins routing reuses `PriceChange` events
  and the same repricer entry point.

## Open questions for owner

- Markup in % only, or also fixed "+$0.50" mode?
- When supplier price DROPS: lower sell price too, or keep the extra margin?
  (Recommend: lower it — stays competitive, flags still fire.)
- Stars pricing: sell prices are in Telegram Stars, supplier cost in USD/VND —
  need the FX conversion point pinned (recommend: convert at sync time, store
  the rate used in `price_events`).
