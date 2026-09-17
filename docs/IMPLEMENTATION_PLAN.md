# Dependency-Ordered Implementation Plan

Status: Phase 5 execution plan. Work proceeds in thin verified slices. A later slice does not begin when its dependency gate is red.

## Delivery contract

**Goal:** replace the three competing commerce implementations with the canonical modular monolith described in `TARGET_ARCHITECTURE.md`, without disrupting the existing system before cutover.

**In scope:** canonical backend, database, worker, bot/webhook, admin API, dashboard migration, legacy-data migration, production controls, and launch audit.

**Out of scope:** unapproved supplier sales, multi-store support, speculative scale work, and providers beyond Telegram Stars and the internal wallet.

**Implementation rule:** each slice follows `failing executable criterion -> smallest implementation -> targeted verification -> diff review`. Destructive cleanup is last.

**Stop rule:** stop a slice when its stated acceptance criteria pass. New improvements that do not block those criteria are recorded for a later slice rather than expanding the active change.

## Source-of-truth transition

The migration temporarily contains old and new code, but their status is explicit:

- `src/digital_shelf/`: target canonical application; introduced in Slice 0 and not exposed to production traffic until cutover.
- `shop/`: legacy local/polling implementation; frozen except for critical security/data-loss fixes.
- `worker/`: legacy production-candidate webhook implementation; frozen except for critical security/data-loss fixes.
- `supabase/migrations/0001..0004`: legacy schema history; retained for existing environments.
- `reseller-backend/`: experimental reference; no new features.
- `dashboard/`: active owner UI, migrated screen by screen to the canonical API after backend contracts are stable.

There is no dual-writing between legacy and canonical commerce tables. Cutover uses a maintenance window and final delta migration.

## Milestone A — Reproducible foundation

### Slice 0: canonical application and development runtime

**Depends on:** approved architecture.

**Build:**

- Create `src/digital_shelf/` with configuration, API and worker entry points but no sales route.
- Add FastAPI, SQLAlchemy 2, Alembic and a PostgreSQL driver using compatible pinned versions.
- Add one validated settings model; production startup rejects placeholder secrets and missing webhook security values.
- Add a local PostgreSQL container and commands for migration, API, worker and tests.
- Add CI jobs for unit tests, a real PostgreSQL service, migration application and container build.
- Establish structured logging with redaction and correlation ids.

**Verify:** clean dependency install; container build; `/live` works without the database; `/ready` fails and succeeds with database availability; empty-database migration job is executable.

**Gate:** no business feature is added until a fresh machine can run the same database-backed test environment as CI.

### Slice 1: additive canonical schema

**Depends on:** Slice 0.

**Build:**

- Create canonical identity, catalog, checkout, order, payment, inventory, wallet, fulfillment, job/outbox, promotion, referral, support, admin, audit and configuration tables.
- Add money, currency, state, foreign-key, uniqueness and transition constraints.
- Add partial indexes for active catalog, available inventory, due jobs and attention queues.
- Preserve legacy tables; canonical table names or schema namespace must not collide ambiguously.
- Add representative legacy fixtures and a migration reconciliation skeleton.

**Verify:** apply from empty; upgrade a legacy-shaped fixture; apply/backfill twice; inspect constraints/indexes; run negative tests proving duplicate provider ids, inventory assignment and wallet idempotency are rejected.

**Gate:** Phase 4 controls INV-02, PAY-04 and the migration gate have executable PostgreSQL evidence.

### Slice 2: encryption and inventory service

**Depends on:** Slice 1.

**Build:**

- Port the existing Fernet/HMAC strengths into the canonical inventory module.
- Support `finite_unique`, `finite_quantity`, `unlimited` and `manual` policies.
- Implement atomic bulk validation/import, reservation, expiry, allocation, sale, quarantine and retirement.
- Keep decryption behind one internal fulfillment interface.

**Verify:** wrong-key/rotation behavior; duplicate import; atomic failed import; two-connection last-item race; expiry racing payment; response/log snapshots contain no protected values.

**Gate:** all INV controls not dependent on delivery are green.

## Milestone B — Commerce transaction core

### Slice 3: users, catalog and idempotent checkout

**Depends on:** Slices 1–2.

**Build:**

- User upsert from trusted Telegram identity and terms acceptance.
- Categories and product configuration with validated fulfillment/policy combinations.
- Server-owned quotes and checkout sessions.
- One active result per callback/request idempotency key.
- Pre-checkout validation and finite-stock reservation.

**Verify:** manipulated user/product/price/currency callbacks; expired terms; inactive product; repeated callback; product version change; no-stock rejection.

**Gate:** PAY-02, PAY-03, PAY-08, PAY-09 and INV-09 are green.

### Slice 4: Telegram Stars payment finalization

**Depends on:** Slice 3.

**Build:**

- Payment attempts and append-only verified events.
- One transaction for payment confirmation, order confirmation, inventory finalization, fulfillment job/outbox and audit.
- Duplicate and anomalous event handling.
- Paid-without-stock path that creates durable refund/review work.

**Verify:** concurrent duplicate event; same charge with different event ids; wrong payer/amount/currency; fault after every local write; late payment with and without replacement stock.

**Gate:** PAY-04 through PAY-08 are green against PostgreSQL.

### Slice 5: wallet purchase

**Depends on:** Slices 2–3.

**Build:**

- Wallet accounts and immutable entries.
- Atomic wallet purchase with guarded non-negative balance.
- Audited owner adjustments through an application command, not direct balance editing.

**Verify:** two concurrent purchases against one balance; repeated request id; failure after debit attempt; unauthorized/invalid adjustment; reconciliation of account balance to entries.

**Gate:** WAL-01 through WAL-03 are green.

## Milestone C — Durable effects and recovery

### Slice 6: jobs, outbox and delivery

**Depends on:** Slice 4.

**Build:**

- PostgreSQL job/outbox claim with lease and fencing token.
- Bounded retry/backoff and dead-letter state.
- Delivery strategies for unique code/link, reusable content, private file and manual fulfillment.
- Attempt records and same-artifact retry.
- Reconciliation for paid orders without fulfillment work.

**Verify:** stale worker fencing; API/worker restart; timeout after possible Telegram send; permanent Telegram error; object-storage outage; delivered-state regression rejection.

**Gate:** DEL-01 through DEL-05 and PAY-07 are green.

### Slice 7: durable refunds and payment reconciliation

**Depends on:** Slices 4 and 6.

**Build:**

- Refund intent before provider mutation.
- Worker execution, unknown-outcome review and safe reconciliation.
- Atomic confirmed-refund state, financial event, wallet credit where applicable, inventory quarantine/entitlement revocation and audit.
- Admin review commands constrained by the state machine.

**Verify:** repeated request; confirmed success; proven failure; timeout/unknown; local database failure after provider response; refund racing delivery; item cannot return to available.

**Gate:** REFUND-01 through REFUND-04 are green.

### Slice 8: promotions and referrals

**Depends on:** Slices 4–5.

**Build:**

- Promotion eligibility, limits and transactional redemption.
- Referral attribution with self/cycle protection.
- Pending reward, fraud/refund hold, one-time release and reversal behavior.

**Verify:** concurrent final promotion use; repeated reward job; self/circular referral; refund before and after reward release; owner review permissions.

**Gate:** PRO-01 and REF-01 through REF-02 are green.

## Milestone D — Canonical interfaces

### Slice 9: authenticated Telegram webhook and bot journey

**Depends on:** Slices 3–8.

**Build:**

- Mandatory path and header webhook secrets.
- Durable ingress idempotency.
- `/start`, catalog, product, terms, checkout, successful payment, orders and support flows backed only by canonical services.
- Rate limits and customer-safe errors.

**Verify:** missing/wrong secrets; duplicate/out-of-order updates; callback manipulation; restart during checkout/payment; complete fake-provider customer journey.

**Gate:** PAY-01, SEC-03, SEC-04, SEC-08 and the customer E2E path are green.

### Slice 10: admin authentication, permissions and core API

**Depends on:** Slices 1–9.

**Build:**

- Supabase JWT verification and canonical admin membership/roles.
- Catalog, inventory, order, payment, refund, customer, balance and attention-queue endpoints.
- Transactional audit for every sensitive mutation.
- Idempotency keys, field-minimized read models and safe pagination/filter allowlists.

**Verify:** endpoint/role deny matrix; expired/wrong-project JWT; malicious filters; duplicate submissions; audit failure rolls back mutation; stock/payment secrets absent from responses.

**Gate:** SEC-01 through SEC-07 and WAL-03 are green.

### Slice 11: notifications, support and broadcasts

**Depends on:** Slice 6 and Slice 10.

**Build:**

- Notification preferences and outbox templates.
- Support tickets/messages and internal notes.
- Broadcast audience snapshot, test send, scheduling, throttling, pause/resume and outcome counts.

**Verify:** notification dependency failure does not roll back commerce; broadcast duplicate job does not duplicate a recipient send; permission tests; no support/message content in logs.

**Gate:** durable recovery and privacy response tests pass.

## Milestone E — Owner dashboard

### Slice 12: shared dashboard shell and Home

**Depends on:** Slice 10.

**Build:** canonical navigation, role-aware routes, loading/error/permission states, checkout-status control, setup path, attention queue and operational metrics.

**Verify:** browser tests for first run, returning owner, paused checkout, attention actions, session expiry and denied role.

### Slice 13: Catalog and inventory dashboard

**Depends on:** Slices 10 and 12.

**Build:** guided product form, fulfillment selection, preview/publish, category management, stock summaries and two-stage atomic import.

**Verify:** browser flows for every fulfillment type, invalid combination, duplicate stock, failed atomic import, low-stock state and protected-value absence.

### Slice 14: Sales and customer operations dashboard

**Depends on:** Slices 7, 10 and 12.

**Build:** orders, payments, refunds, customer detail, wallet history/adjustment, timelines and safe resend/manual fulfillment.

**Verify:** plain-language state mapping; refund unknown state; same-item resend; audited balance adjustment; filters/pagination and responsive priority views.

### Slice 15: Marketing, support and settings dashboard

**Depends on:** Slices 8, 11 and 12.

**Build:** promotions, referrals, broadcasts, support inbox, notification/legal/team/integration settings and activity log.

**Verify:** recipient preview/test send; promotion impact preview; referral holds; legal-version warning; secret write-only controls; role restrictions.

## Milestone F — Migration, hardening and cutover

### Slice 16: legacy-data migration tooling

**Depends on:** stable canonical schemas and services.

**Build:** repeatable importers for SQLite and legacy Supabase data; dry-run mode; count, money, charge-id and stock-state reconciliation; encrypted-value/key-version preservation.

**Verify:** realistic snapshots; repeated dry run/import; malformed legacy rows become explicit review records; zero unexplained reconciliation differences.

### Slice 17: operations and release controls

**Depends on:** all canonical runtime slices.

**Build:** production Docker image, migration job, readiness/liveness, metrics, alerts, backup/restore runbooks, secret/config validation, dependency/secret scanning and environment approvals.

**Verify:** clean-environment deploy; database/storage outage; worker/API restart; backup restore; alert delivery; image/commit evidence bundle.

### Slice 18: controlled cutover

**Depends on:** Slices 16–17 and all Phase 4 launch-critical automated gates.

**Execute:** backup, pause sales, final delta import, reconcile, deploy canonical API/worker, switch webhook, enable owner-only purchase, complete live Stars purchase/refund, observe, then enable general sales.

**Stop immediately on:** duplicate money movement, oversell, missing paid order, secret exposure, authorization bypass, migration mismatch or persistent readiness failure.

### Slice 19: legacy removal

**Depends on:** completed rollback window and verified canonical production behavior.

**Build:** archive/remove `reseller-backend`, Worker commerce code and obsolete schema paths; make polling explicitly development-only; remove stale deployment/testing claims; retain required migration history and operational evidence.

**Verify:** repository search finds no active imports/routes/deploy jobs for legacy commerce; clean build/test/deploy uses only canonical components.

## Completion reporting

For each slice, report:

- Files and contracts changed
- Acceptance tests executed and their exact outcomes
- Phase 4 control ids newly verified
- Data migration or rollback implications
- Known limitations and deferred work

Passing unit tests alone never marks a transaction, deployment, backup, provider, or browser control verified.

