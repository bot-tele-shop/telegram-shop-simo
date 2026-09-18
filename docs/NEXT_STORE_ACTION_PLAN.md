# Store Completion Action Plan

Status: owner-review draft  
Date: 2026-09-18  
Scope: complete the Telegram storefront and owner dashboard without weakening payment, inventory, or refund safety.

## 1. Decision and relationship to existing plans

This plan turns the observed gaps between the live store and the Zoom Store reference into executable work. It supplements, and does not replace:

- `TARGET_ARCHITECTURE.md`
- `IMPLEMENTATION_PLAN.md`
- `PRODUCTION_HARDENING.md`
- `ADMIN_EXPERIENCE.md`

The current Cloudflare Worker, Supabase schema, and static dashboard remain the live system until controlled cutover. They receive only launch-blocking fixes. New catalog, promotion, referral, customer, automation, and reseller-API behavior is implemented in the canonical application under `src/digital_shelf/` with PostgreSQL/Alembic migrations. The dashboard is then migrated in place to the canonical API.

This prevents new business rules from being duplicated across `worker/`, `shop/`, and the canonical application.

## 2. Task contract

### Goal

Deliver one coherent Telegram digital-goods store with:

- consistent branding and valid legal/support content;
- category-based product discovery;
- Telegram Stars checkout and safe automatic fulfillment;
- an owner dashboard for catalog, inventory, orders, promotions, customers, automations, feature controls, and settings;
- optional referrals and reseller API that remain hidden and unavailable until safely configured;
- durable monitoring, migration, deployment, and rollback evidence.

### In scope

- live-store containment and cleanup;
- canonical database and application services;
- feature enablement/readiness states;
- categories, products, media, inventory, checkout, orders, delivery, refunds;
- scheduled Stars promotions;
- customer/order operations;
- low-stock alerts and opt-in Telegram announcements;
- localization foundation;
- referrals after a reward policy is approved;
- reseller API after the commerce core is stable;
- dashboard migration and production cutover.

### Out of scope

- USDT, Binance, Bybit, BEP20, TRC20, or other crypto top-ups inside the Telegram bot;
- a custodial customer cash/crypto wallet;
- card payments for Telegram digital goods;
- automatic supplier purchasing before supplier legality, authorization, budget, and uncertainty controls are approved;
- multiple stores/tenants;
- cloning Zoom Store's branding, content, or proprietary implementation.

### Stop condition

Core launch work is complete when the Definition of Done in section 13 passes. Referrals and reseller API are independent optional releases and do not delay the safe core store launch.

## 3. Verified current baseline

### Working today

- deployed Telegram bot and Cloudflare Pages owner dashboard;
- owner authentication through Supabase Auth and server-side owner allowlist;
- product creation/editing, encrypted stock upload, activation/hiding;
- paginated live-stock product list;
- Telegram Stars invoices;
- terms acceptance;
- server-side payer, amount, currency, stock, and charge validation;
- automatic unique-code delivery;
- buyer profile and order status views;
- dashboard order search, same-code resend, Stars refund, checkout pause;
- refund quarantine and admin audit records.

### Observed launch blockers

- three inconsistent identities: Telegram bot name, store name, and dashboard brand;
- empty support contact;
- placeholder terms and privacy content;
- one test product with test copy;
- Offers, Referrals, and API buttons lead to placeholder pages;
- pending latency/payment-boundary commit is local because GitHub account `rabavadev` lacks repository write permission;
- the live Worker is legacy and the canonical application has only its foundation slice.

## 4. Required owner decisions and inputs

Implementation can prepare forms and validation, but production enablement waits for these inputs.

| Input | Required before | Owner supplies |
|---|---|---|
| Final store name and logo | Release 0 | Name, logo, short description |
| Support identity | Release 0 | Telegram handle and optional email |
| Terms/privacy/refund policy | Release 0 | Approved customer-facing text |
| Warranty/delivery language | First real product | Promise, exclusions, expected delivery |
| Initial categories/products | Catalog release | Names, descriptions, Stars prices, stock |
| Announcement destination | Automations | Telegram channel/chat and bot permissions |
| Languages | Localization | Ordered locale list and reviewed translations |
| Referral reward policy | Referrals | Discount/credit/other, hold time, limits |
| Reseller audience and contract | API | Who may use it and permitted operations |

Default decisions until the owner chooses otherwise:

- payment currency is Telegram Stars (`XTR`) only;
- Offers, Referrals, Reseller API, announcements, and extra languages are disabled;
- disabled modules are absent from Telegram menus and reject direct requests;
- no supplier-backed item is sold;
- checkout remains paused whenever required legal/support configuration is incomplete.

## 5. Change-impact map

| Area | Current owner | Target owner | Main consumers |
|---|---|---|---|
| Telegram webhook/menu | `worker/src/flow.py`, `worker/src/storefront.py` | canonical Telegram interface/application services | buyers |
| Dashboard | `dashboard/` | same UI, migrated to canonical admin API | owners/operators |
| Live legacy data | `supabase/migrations/0001..0004` | preserved until cutover/import | Worker/dashboard |
| Canonical data | Alembic under `migrations/` | PostgreSQL canonical schema | API and worker roles |
| Catalog/inventory | legacy Supabase RPCs | canonical catalog/inventory modules | bot, dashboard, checkout |
| Payments/refunds | legacy Worker RPCs | canonical payment/refund services | Telegram and admin API |
| Settings/features | legacy `metadata` | typed canonical store settings and feature states | bot, dashboard, jobs |
| Background effects | inline Worker actions | canonical PostgreSQL jobs/outbox worker | delivery/alerts/broadcasts |

Legacy paths deliberately remain until the rollback window ends. There is no dual-writing between legacy and canonical commerce tables.

## 6. Feature-control specification

Feature controls are readiness states, not cosmetic booleans.

### States

- `disabled`: hidden from menus and rejected by backend.
- `setup_required`: owner selected the feature but required configuration is incomplete; still hidden and rejected.
- `enabled`: readiness checks pass and backend behavior is available.

### Initial feature keys

| Key | Default | Readiness checks |
|---|---|---|
| `offers` | disabled | at least one valid current/future promotion |
| `referrals` | disabled | approved reward rule, limits, hold period, abuse controls |
| `reseller_api` | disabled | API terms, scopes, rate limits, at least one authorized client |
| `restock_announcements` | disabled | destination configured and bot permission verified |
| `low_stock_alerts` | enabled | owner notification destination and threshold |
| `localization` | disabled | at least one fully reviewed non-default locale |

### Invariants

1. Menu visibility and backend authorization use the same server-owned feature state.
2. Old callback data cannot bypass a disabled feature.
3. Secrets are write-only and are never returned to the dashboard.
4. Enable/disable attempts create audit records with actor, result, and readiness failures.
5. Checkout/payment/refund/inventory safety cannot be disabled by a feature flag.
6. Product activation remains a product-level state, not a global feature.
7. Categories appear automatically when active products span configured categories; no redundant category toggle is required.

## 7. Dependency-ordered releases

Each release is a thin, independently verifiable change. A release does not begin while its dependency gate is red.

### Release 0 — Contain and clean the live store

Depends on: none.

Tasks:

1. Grant GitHub write access to the deployment identity or authenticate with an authorized owner account.
2. Push the existing latency/payment-boundary branch and open a focused PR.
3. Pause live checkout while public content is incomplete.
4. Set one final store name in BotFather, application settings, and dashboard branding.
5. Add logo, short description, real support contact, approved terms, privacy, refund, warranty, and delivery text.
6. Hide/delete only disposable test catalog data after confirming it has no paid order relationship; otherwise archive it.
7. Add one legitimate stock-backed product and test inventory item.
8. Remove dead menu buttons from the live legacy menu as a temporary critical UX fix, or leave them hidden by default if the feature-control slice can ship immediately.

Verification:

- repository branch and PR are visible to the owner;
- CI test/deploy jobs pass;
- Telegram welcome, bot profile, and dashboard show one brand;
- `/terms`, `/privacy`, `/paysupport`, Products, Orders, and Profile show production copy;
- checkout is visibly paused until the owner authorizes the controlled smoke test;
- one owner-only low-value Stars purchase, delivery, order view, and full refund is recorded and reconciled.

Gate: no unexplained payment, stock, delivery, or refund difference.

### Release 1 — Canonical identity, settings, roles, and feature registry

Depends on: canonical foundation Slice 0 and Release 0 containment.

Tasks:

1. Add canonical users, admin users/roles/permissions, store settings, setting revisions, feature states, and audit tables through Alembic.
2. Define typed configuration commands; prohibit direct client table writes.
3. Validate Supabase JWT issuer, audience, signature, expiry, and subject in the canonical API.
4. Implement owner/admin permission checks.
5. Implement feature readiness evaluation and transition commands.
6. Expose minimized admin read models:
   - `GET /admin/v1/settings`
   - `PATCH /admin/v1/settings`
   - `GET /admin/v1/features`
   - `PATCH /admin/v1/features/{feature_key}`
   - `GET /admin/v1/audit`
7. Add correlation IDs and secret-redacting structured logs.

Verification:

- migrations apply to empty PostgreSQL and twice to a representative fixture;
- invalid/expired/wrong-project JWTs return 401;
- authenticated non-owner returns 403;
- unknown setting/feature keys return 400;
- incomplete feature enablement returns `setup_required` with safe reasons;
- audit failure rolls back sensitive configuration mutation;
- secrets and legal content are absent from logs.

Gate: identity and configuration contracts are stable before dashboard consumers migrate.

### Release 2 — Canonical categories, products, assets, and inventory

Depends on: Release 1.

Tasks:

1. Add categories with slug, display name, emoji, description, position, and active/archive state.
2. Add products with immutable SKU, category, title, description, Stars price, status, fulfillment type, inventory policy, warranty/delivery text, version, and timestamps.
3. Add product assets using private storage references or validated Telegram file identifiers.
4. Implement fulfillment policies: `unique_code`, `unique_url`, `reusable_content`, `download_file`, and `manual`.
5. Implement inventory policies: `finite_unique`, `finite_quantity`, `unlimited`, and `manual` with allowed-pair validation.
6. Port encrypted stock import, duplicate fingerprint protection, reservation, allocation, sale, quarantine, and retirement.
7. Implement admin endpoints for category/product/asset/inventory CRUD and preview/publish.
8. Return masked stock metadata only; decryption remains inside fulfillment.

Dashboard tasks:

- add Categories page;
- expand Product form for fulfillment, inventory policy, media, warranty, delivery, and preview;
- change product deletion to archive when history exists;
- add two-stage inventory import: validate preview, then commit atomically;
- add low-stock thresholds and clear empty/error/loading/success states.

Verification:

- manipulated SKU/category/product fields are rejected;
- invalid fulfillment/inventory pair is rejected;
- duplicate stock and mixed valid/invalid uploads roll back atomically;
- two concurrent claims cannot allocate one unique item twice;
- archived or inactive items never appear in buyer catalog;
- APIs, dashboard, logs, and audit never reveal unused plaintext inventory.

Gate: catalog and inventory invariants pass against real PostgreSQL.

### Release 3 — Idempotent checkout, Stars payment, delivery, and refunds

Depends on: Release 2.

Tasks:

1. Add checkout sessions keyed by Telegram callback/update idempotency key.
2. Snapshot product version, title, Stars price, terms version, promotion result, and expiry.
3. Reserve finite stock during pre-checkout with guarded expiry.
4. Validate payer, exact `XTR` amount, order state, terms, product state, and stock in one server transaction.
5. Persist unique Telegram charge events and anomalies.
6. Finalize payment, inventory, order, fulfillment job, notification outbox, and audit atomically.
7. Deliver the already-assigned item through a durable job; retries reuse the same item.
8. Add refund intent, provider execution, unknown-result review, reconciliation, and atomic confirmed-refund quarantine.
9. Add attention states for paid-without-stock, delivery failure, payment anomaly, and uncertain refund.

Verification:

- repeated Buy callback returns one checkout/order;
- concurrent duplicate payment event fulfills once;
- wrong user, amount, currency, product version, or terms is rejected;
- payment with no fulfillable stock creates durable refund work;
- timeout after possible Telegram send never allocates a second code;
- repeated refund request never refunds twice;
- refunded inventory cannot become available;
- restart and stale-job lease tests pass.

Gate: all money/inventory integration tests pass before the canonical bot is exposed.

### Release 4 — Canonical Telegram storefront and dynamic menus

Depends on: Releases 1–3.

Tasks:

1. Add mandatory secret webhook path and Telegram secret-token header validation.
2. Add durable ingress claim before processing.
3. Implement `/start`, Home, Products, category list, product list, product detail, terms, Stars checkout, Profile, Orders, Support, and payment support.
4. Build both inline and persistent keyboards from active feature state.
5. Hide disabled/setup-required modules and reject direct stale callbacks.
6. Add category/product pagination with Telegram's 64-byte callback limit.
7. Add customer-safe loading, empty, expired, sold-out, maintenance, error, and success messages.
8. Preserve immediate acknowledgement only for read-only navigation; payment/consent mutations remain durable first.
9. Add per-user rate limits that never drop payment callbacks.

Verification:

- complete fake-provider journey from `/start` through delivery and order history;
- every generated callback fits Telegram limits;
- disabled feature is absent and cannot be invoked directly;
- inactive/sold-out product cannot invoice;
- duplicate/out-of-order updates are idempotent;
- private-chat ownership is enforced;
- measured navigation acknowledgement is immediate while durable processing continues.

Gate: owner allowlisted staging bot passes the full journey.

### Release 5 — Dashboard shell, setup, and core operations

Depends on: Releases 1–4.

Tasks:

1. Migrate the existing dashboard in place to canonical admin endpoints.
2. Use this navigation:
   - Overview
   - Products
   - Categories
   - Inventory
   - Orders
   - Customers
   - Promotions
   - Automations
   - Integrations/API
   - Settings
3. Add first-run setup checklist for brand, support, legal text, catalog, stock, webhook, and checkout state.
4. Add Features section showing `disabled`, `setup_required`, or `enabled`, readiness reasons, and impact preview.
5. Add role-aware route visibility and server enforcement.
6. Add attention queue for failed delivery, payment review, refunds, low stock, and failed jobs.
7. Add order timeline, same-item resend, refund workflow, and customer purchase history.
8. Add responsive loading, empty, error, denied, confirmation, pending, and success states.

Verification:

- browser tests for first-run and returning owner;
- session expiry and denied role;
- feature enablement blocked until ready;
- dashboard hide does not replace backend authorization;
- dangerous actions require explicit confirmation with exact consequence;
- mobile viewport retains critical order/attention actions;
- no stock secret, service key, or token appears in DOM/network/log snapshots.

Gate: owner can operate the store without direct Supabase table edits.

### Release 6 — Scheduled Stars promotions

Depends on: Releases 2–5.

Tasks:

1. Add promotions with Stars discount rule, eligibility, start/end, global/per-user limits, status, and priority.
2. Add transactional promotion redemption and immutable order discount snapshot.
3. Add dashboard promotion list, create/edit, impact preview, schedule, pause, and end actions.
4. Show original price, sale price, percentage, remaining stock, and server-derived countdown in Telegram.
5. Show Offers only when at least one eligible active promotion exists.

Verification:

- price never comes from callback data;
- boundary tests for not-started, active, and expired offer;
- concurrent final-use redemption succeeds once;
- expiry racing pre-checkout uses the committed order quote rules;
- refund does not incorrectly restore a limited promotion use unless policy says so;
- fake/empty discounts cannot be published.

Gate: offer price and Stars invoice always reconcile.

### Release 7 — Alerts, restock announcements, and customer support

Depends on: durable jobs/outbox from Release 3 and dashboard from Release 5.

Tasks:

1. Add notification preferences and typed templates.
2. Add low-stock and out-of-stock owner alerts with per-product thresholds.
3. Add optional new-product, restock, and promotion announcements to one configured Telegram destination.
4. Verify destination permissions before enabling.
5. Add preview/test-send, deduplication key, throttling, pause/resume, and delivery outcome counts.
6. Add support tickets linked to user/order, customer messages, and internal notes.
7. Keep support content and inventory secrets out of logs and analytics.

Verification:

- repeated stock upload event does not duplicate an announcement;
- notification failure never rolls back stock or payment;
- unauthorized operator cannot broadcast or read restricted tickets;
- preview and actual message use the same template revision;
- support button includes order context without exposing delivery secret.

Gate: failed external sends are recoverable and visible.

### Release 8 — Localization

Depends on: stable storefront/dashboard copy from Releases 4–7.

Tasks:

1. Define message keys and default English catalog-independent copy.
2. Persist supported locales and user locale preference.
3. Add dashboard translation completeness report and enable control.
4. Add bot language selector only when more than one locale is complete.
5. Keep product translation fallback explicit.

Verification:

- every enabled locale passes key-completeness tests;
- missing product translation falls back predictably;
- terms acceptance records the exact localized terms version;
- callback identifiers remain language-neutral.

Gate: no enabled language exposes mixed placeholder copy.

### Release 9 — Referrals (optional independent release)

Depends on: stable orders/refunds/promotions and approved reward policy.

Tasks:

1. Add referral attribution with one referred-user relationship.
2. Reject self-referral and circular attribution.
3. Define qualifying order, reward amount/type, limits, hold period, and expiry.
4. Create pending reward only after a qualifying paid order.
5. Release once after refund/fraud hold; reverse safely on refund according to policy.
6. Add buyer referral page and dashboard configuration/review.

Verification:

- duplicate updates create one relationship/reward;
- self/circular referrals fail;
- refund before and after release follows approved policy;
- concurrent release runs credit once;
- disabled referrals are absent and rejected.

Gate: accounting reconciliation equals released minus reversed rewards.

### Release 10 — Reseller API (optional independent release)

Depends on: stable canonical commerce, approved reseller contract, and security review.

Tasks:

1. Add hashed API credentials with prefix, scopes, owner, status, created/last-used/expiry timestamps.
2. Add generate-once, rotate, and revoke operations; plaintext key is shown only at creation.
3. Add per-key/IP rate limits, quotas, correlation IDs, and audit.
4. Publish versioned product availability, checkout/create-order, order-status, and support endpoints.
5. Require idempotency key for every mutation.
6. Minimize stock data; never expose inventory values or supplier credentials.
7. Publish OpenAPI documentation and examples.

Verification:

- missing, malformed, expired, revoked, wrong-scope keys fail;
- duplicate mutation key returns the original result;
- quota/rate-limit tests do not affect Telegram payment callbacks;
- logs/docs/responses contain no secret key or inventory payload;
- API-created orders use the same canonical pricing, payment, inventory, and refund services.

Gate: independent security review has no unresolved high-severity issue.

### Release 11 — Migration, cutover, and legacy removal

Depends on: core Releases 1–7; optional Releases 8–10 do not block core cutover.

Tasks:

1. Build repeatable dry-run importers for settings, users, terms, categories/products, encrypted stock, orders, charges, refunds, and audit evidence.
2. Reconcile counts, stock states, Stars amounts, charge IDs, and encryption compatibility.
3. Back up legacy database/configuration and prove a non-production restore.
4. Deploy canonical API/worker dark with sales disabled.
5. Verify authentication, jobs, read-only migrated data, alerts, and readiness.
6. Schedule maintenance; pause legacy checkout; apply final delta import; reconcile again.
7. Register canonical webhook and ensure legacy polling/webhook writer is disabled.
8. Enable owner-only purchase; run low-value Stars purchase, delivery, order view, refund, and inventory quarantine.
9. Observe agreed window, then enable general checkout.
10. After rollback window, remove active routes/imports/deploy jobs for legacy commerce while retaining migration history and evidence.

Stop immediately on duplicate money movement, oversell, missing paid order, secret exposure, authorization bypass, migration mismatch, or persistent readiness failure.

Rollback:

- before canonical sales: restore previous webhook/runtime and verified snapshot;
- after canonical sales begin: pause checkout and forward-fix canonical state; never resume an old writer against divergent data.

## 8. Dashboard behavior rules

1. A toggle always states its customer impact.
2. `setup_required` controls are disabled and list exact missing configuration.
3. Saving a toggle is not described as successful until the backend confirms it.
4. Checkout pause requires confirmation and states that already-paid orders still deliver.
5. Product activation is refused when required stock/media/configuration is missing.
6. Refund UI distinguishes requested, processing, confirmed, failed, and review-required.
7. API keys and secrets are write-only; the UI can show only prefix/fingerprint/last-used state.
8. Empty pages explain the next valid action instead of showing fake data.
9. Destructive archive/revoke actions identify the exact product/key/destination.
10. Dashboard navigation visibility never substitutes for backend permission checks.

## 9. Required test matrix

Every release runs its targeted tests plus the non-regression suite.

### Unit

- validators, state transitions, readiness evaluators, menu composition, price calculation, message rendering.

### PostgreSQL integration

- real migrations, constraints, transaction rollback, concurrent stock/payment/promotion/referral races, job leases, idempotency.

### API/auth

- 401/403 matrix, role permissions, malicious filters, unknown fields, idempotency, pagination, redacted responses.

### Telegram adapter/E2E

- duplicate/out-of-order updates, callback manipulation, 64-byte limit, terms, Stars pre-checkout/payment, delivery retry, refund, disabled feature callbacks.

### Browser

- first-run setup, product publish, inventory import, feature readiness, promotion schedule, attention resolution, session expiry, responsive navigation.

### Security/privacy

- secret scanning, log snapshots, stock-value absence, API-key lifecycle, webhook secret rejection, authorization-deny tests.

### Release

- clean dependency install, lint, type check, complete tests, migration on empty and legacy fixture, container build, dashboard JS/build, deploy dry run, backup restore drill before cutover.

Unit mocks alone do not verify payment atomicity, database concurrency, provider outcomes, browser behavior, deployment, or restore readiness.

## 10. CI/CD and environments

Required environments:

- local/test: fake Telegram adapter and disposable PostgreSQL;
- staging: separate bot, database, dashboard, secrets, and owner allowlist;
- production: protected environment with explicit migration/deploy approval.

Pipeline order:

1. dependency lock verification;
2. lint/type/static secret checks;
3. unit tests;
4. PostgreSQL migration/integration tests;
5. API and Telegram journey tests;
6. dashboard build/browser tests;
7. immutable application image build;
8. staging deploy and smoke checks;
9. approved additive production migration;
10. production deploy;
11. readiness and owner-only smoke checks;
12. general enablement only after evidence review.

Production deployment must use the commit that passed the checks. A dashboard-only green result is not proof that the Worker/API, database migration, or webhook changed successfully.

## 11. Work tracking and completion report

Every task/PR records:

- release/task identifier;
- user-visible outcome;
- dependencies satisfied;
- schema/API compatibility impact;
- files/contracts changed;
- tests run and exact results;
- migration/rollback implications;
- screenshots or runtime evidence when UI changed;
- known limitations and explicitly deferred work.

New findings are added to the active release only if they block acceptance, were introduced by the change, make it unsafe, or invalidate verification. Everything else is recorded as later work.

## 12. Immediate execution queue

This is the exact starting order after owner approval of this plan.

1. **Access:** grant repository write permission and verify `git push --dry-run` equivalent access without changing production.
2. **Pending fix:** push the latency/payment-boundary commit, open PR, run CI, review, merge, verify Worker deploy.
3. **Containment:** pause checkout and replace public test content/branding/support/legal settings.
4. **Smoke:** create one legitimate test product, run controlled Stars purchase/delivery/refund, then pause again until canonical work is ready.
5. **Canonical Slice 1A:** identity/admin/settings/feature registry migration and real-PostgreSQL tests.
6. **Canonical Slice 1B:** category/product schema and API contracts.
7. **Canonical Slice 2:** encrypted inventory services and concurrency tests.
8. **Canonical Slices 3–4:** idempotent checkout and Stars payment finalization.
9. **Canonical Slices 6–7:** durable delivery and refunds.
10. **Canonical bot:** dynamic compliant storefront with feature states.
11. **Canonical dashboard:** setup, Features, catalog/inventory, orders/customers.
12. **Promotions and automations:** enable only after their gates pass.
13. **Migration/cutover:** dry run, reconciliation, controlled owner smoke, production enablement.
14. **Optional releases:** localization, referrals, then reseller API.

Do not start referrals or reseller API while core payment, delivery, refund, migration, or recovery gates remain red.

## 13. Definition of Done for the core store

The core store is complete only when all statements are true:

- one canonical runtime and PostgreSQL database own commerce behavior;
- one consistent brand, valid support contact, terms, privacy, delivery, warranty, and refund copy are live;
- owner can manage categories, products, media, inventory, orders, customers, promotions, automations, feature states, and settings without direct database edits;
- buyer can browse category/product details, accept current terms, pay exact Stars amount, receive the assigned item, view order status, and contact payment support;
- repeated checkout/payment/delivery/refund events are idempotent;
- unique stock cannot be oversold and unused secrets never leave the fulfillment boundary;
- disabled features are hidden and rejected server-side;
- paid-without-stock, failed delivery, anomalous payment, low stock, failed job, and uncertain refund are visible and recoverable;
- migration reconciliation has zero unexplained differences;
- backup restore and rollback procedures have been exercised;
- CI, staging journey, controlled live Stars purchase/delivery/refund, and post-deploy health evidence are recorded;
- legacy commerce writers are disabled, and later removed after the rollback window.

Optional referrals, reseller API, and extra languages have their own Definition of Done and do not weaken or delay this core finish line.
