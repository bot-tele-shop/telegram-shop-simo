# Target Architecture

Status: Phase 2 design decision. This document defines the system to implement; it does not claim that the current repository already behaves this way.

## Task contract

**Goal:** replace the competing local, Cloudflare, and reseller commerce implementations with one launchable system whose payment, inventory, order, and delivery behavior has a single source of truth.

**In scope:** runtime topology, module ownership, database model, state machines, transaction boundaries, retries, permissions, migration direction, and acceptance criteria.

**Out of scope:** dashboard visual design, provider credentials, production hosting vendor selection, supplier enablement, and implementation in this phase.

**Stop condition:** every required subsystem has one owner, dangerous flows have explicit transaction and idempotency rules, and the later implementation can be ordered without another architecture decision.

## Architecture decision

Build a Python modular monolith backed by PostgreSQL.

The application is one codebase and one Docker image with two process roles:

- `api`: FastAPI HTTP application containing the Telegram webhook and admin API. Aiogram remains the Telegram framework.
- `worker`: background job runner for delivery, refunds, notifications, reservation expiry, reconciliation, and approved supplier work.

Both roles use the same domain modules and PostgreSQL database. They may be deployed independently for restart and scaling purposes, but they are not separate services and must not duplicate business rules.

Use:

- Python 3.12
- Aiogram 3
- FastAPI
- PostgreSQL 15 or newer, initially Supabase Postgres
- SQLAlchemy 2 and Alembic, with explicit SQL where row locking or guarded updates are clearer
- `cryptography`/Fernet-compatible envelope encryption for sensitive inventory
- Supabase Auth for owner login, validated by the API
- A private object-storage bucket for downloadable files
- The existing static dashboard, migrated to the canonical API
- Cloudflare only as DNS, TLS/WAF, and static-site hosting; no commerce state or business logic in a Worker

Do not add Redis, a message broker, microservices, or distributed transactions for the initial launch. PostgreSQL is sufficient for locking, the outbox, and the durable job queue at the expected scale.

## Deployment topology

```text
Telegram ---- signed webhook ----> API process ----+
                                                    |
Owner dashboard ---- authenticated HTTPS --------> |---- PostgreSQL
                                                    |       |
                                                    |       +-- jobs/outbox
                                                    |
                                                    +<--- Worker process
                                                            |
                                                            +-- Telegram delivery/refunds
                                                            +-- private object storage
                                                            +-- approved supplier adapters
```

The API and worker deploy from the same immutable image. A migration job runs once before the new image receives traffic. Only one Telegram delivery mode is active: webhook. The polling runtime is development-only after cutover.

## Module boundaries

| Module | Owns | Must not own |
|---|---|---|
| `users` | Telegram identity, locale, status, terms acceptance | balances or admin roles |
| `catalog` | categories, product descriptions, prices, status, fulfillment configuration | inventory secrets |
| `inventory` | encrypted stock, reservations, allocation, quarantine | payments or Telegram messages |
| `checkout` | checkout sessions, quote validation, expiry, idempotent purchase orchestration | provider-specific callback parsing |
| `orders` | order and order-item snapshots, customer-visible status | provider credentials |
| `payments` | payment attempts, verified events, refunds, reconciliation and anomalies | product mutation |
| `wallet` | accounts, immutable ledger entries, guarded balance changes | external payment callbacks |
| `fulfillment` | fulfillment plans, delivery jobs, attempts and outcomes | charging money |
| `promotions` | eligibility, discount calculation, usage limits and redemptions | balance mutation outside purchase transactions |
| `referrals` | referral relationships, reward eligibility, fraud holds and rewards | direct unlogged balance edits |
| `notifications` | outbox messages, retries and templates | order-state decisions |
| `support` | support tickets and operator notes | privileged commerce mutations |
| `admin` | authenticated application commands and read models | direct table editing |
| `audit` | append-only actor/action/result records | application control flow |
| `configuration` | validated environment and database-backed store settings | secrets returned to clients |
| `jobs` | durable leases, attempts, scheduling and dead-letter state | domain-specific decisions |
| `adapters` | Telegram, storage, auth and supplier integrations | domain state ownership |

Modules call application services, not each other's tables. The database may enforce cross-module foreign keys and invariants.

## Product and fulfillment model

A product selects behavior; no product name or SKU selects code paths.

### Fulfillment types

- `unique_code`: one encrypted value assigned to one order item
- `unique_url`: one encrypted URL assigned to one order item
- `download_file`: a signed, expiring link to a private stored object
- `reusable_content`: reusable text, file, or link; access is still recorded per order
- `manual`: creates an operator task and clearly tells the customer fulfillment is pending
- `subscription_access`: creates or extends a time-bounded entitlement through an approved adapter

### Inventory policies

- `finite_unique`: one `inventory_items` row per sellable secret
- `finite_quantity`: guarded numeric capacity for products without unique secrets
- `unlimited`: no stock decrement; used only for reusable content
- `manual`: availability is controlled by product status and operator capacity

The API validates allowed pairs. For example, `unique_code` and `unique_url` require `finite_unique`; `reusable_content` normally requires `unlimited`.

Supplier-backed products are not a distinct product type. A supplier is an optional inventory-acquisition adapter. Supplier stock must enter the same inventory or entitlement model before customer delivery. Uncertain supplier outcomes go to manual review and must never trigger blind repurchasing.

## Database model

All identifiers are UUIDs internally. Public order references are separate non-secret identifiers. Monetary amounts use signed 64-bit integer minor units; Telegram Stars use whole-star integer units. Floating-point money is forbidden.

### Identity and catalog

- `users`: `id`, unique `telegram_user_id`, username/display snapshots, locale, status, created/updated timestamps
- `terms_acceptances`: user, terms version, accepted timestamp; unique per user/version
- `categories`: parent category, unique slug, name, description, sort order, active flag
- `products`: unique SKU, category, name, description, price amount/currency, status, fulfillment type, inventory policy, metadata, version, timestamps
- `product_assets`: product, private storage object reference, asset role, checksum, active flag

Indexes cover active category ordering, active product listing, and case-insensitive SKU/name search. Product deletion is soft archival when any order references it.

### Checkout, orders, and payments

- `checkout_sessions`: user, product/version, quantity, quoted amounts, currency, promotion result, status, expiry, unique idempotency key
- `orders`: unique public reference, user, unique checkout session, order state, payment state, fulfillment state, amount snapshots, currency, timestamps
- `order_items`: order, product reference, name/SKU/price/fulfillment snapshots, quantity, fulfillment state
- `payment_attempts`: order, provider, expected amount/currency, state, provider payment identifier, timestamps
- `payment_events`: payment attempt, provider, unique provider event key, payload hash, verified timestamp, result
- `payment_anomalies`: payment/event reference, reason, review state, resolution and operator
- `refunds`: order/payment, amount, state, unique idempotency key, provider refund identifier, attempts and timestamps

Constraints enforce non-negative totals, matching currencies, one order per checkout, and uniqueness of provider charge/event identifiers. Provider payload storage is minimized and redacted; secrets and full customer payloads are not logged.

### Inventory and fulfillment

- `inventory_items`: product, encrypted payload, encryption key version, HMAC fingerprint, state, reservation expiry, order-item reference, reserved/sold/quarantined timestamps
- `inventory_counters`: product, available quantity, reserved quantity, sold quantity, version; only for `finite_quantity`
- `fulfillment_jobs`: order item, strategy, state, next attempt time, attempt count, lease owner/expiry, unique logical job key
- `delivery_attempts`: fulfillment job, attempt number, channel, Telegram message id when known, outcome, sanitized error and timestamps
- `entitlements`: user, product/order item, state, starts/expires timestamps, external reference

Unique `(product_id, fingerprint)` prevents duplicate stock import. A partial index supports rapid claims of available stock by product. An inventory item may be bound to only one order item. Ciphertext and decrypted values are excluded from normal list/search/admin read models.

### Wallet, promotions, and referrals

- `wallet_accounts`: unique user/currency, current balance, version
- `wallet_entries`: account, signed amount, entry type, reference type/id, unique idempotency key, actor and timestamp
- `promotions`: code, discount rule, validity window, global/per-user limits, status and eligibility metadata
- `promotion_redemptions`: promotion, user, order, amount; unique promotion/order
- `referrals`: unique referred user, referrer, attribution time, status and risk reason
- `referral_rewards`: referral/order, beneficiary, amount, state, available timestamp, unique reward rule key

Every wallet balance mutation has exactly one immutable ledger entry in the same transaction. Referral rewards are pending until the qualifying order passes its refund/fraud hold. Self-referrals and circular relationships are rejected.

### Operations

- `notification_outbox`: recipient/channel/template, redacted payload, state, attempts and next attempt
- `jobs`: job type, redacted payload, state, run time, lease/fencing token, attempts and last sanitized error
- `support_tickets` and `support_messages`
- `admin_users`, `roles`, `role_permissions`
- `audit_events`: actor, action, target, request correlation id, redacted before/after metadata, result and timestamp
- `store_settings`: typed operational settings with revision history

Jobs are claimed with `FOR UPDATE SKIP LOCKED`. Lease completion requires the current fencing token so an expired worker cannot overwrite a newer attempt.

## State ownership

Use independent state axes rather than one overloaded order status.

- Order: `pending`, `confirmed`, `cancelled`, `completed`, `refunded`, `review_required`
- Payment: `unpaid`, `pending`, `paid`, `refund_pending`, `refunded`, `failed`, `anomalous`
- Fulfillment: `not_ready`, `queued`, `processing`, `delivered`, `manual_action`, `failed`, `revoked`
- Inventory item: `available`, `reserved`, `sold`, `quarantined`, `retired`
- Job: `queued`, `running`, `retry_wait`, `succeeded`, `dead_letter`

Transitions occur only through domain services. Database checks reject impossible terminal-state regressions, including `delivered -> failed` and `refunded -> paid`.

## Critical transaction boundaries

### Idempotent checkout creation

The callback query id or a server-issued request token is the idempotency key. In one transaction:

1. Lock or create the checkout session by idempotency key.
2. Validate the active product version, price, terms, quantity, promotion, and customer status.
3. Create or return the single pending order and payment attempt.

A repeated Telegram callback returns the existing invoice/order result.

### Telegram pre-checkout and reservation

In one short transaction:

1. Lock the checkout and order.
2. Verify buyer, product version, expected currency and exact amount from server-owned records.
3. For finite inventory, atomically claim stock and set a reservation expiry.
4. Mark the checkout ready for provider confirmation.

If no item can be reserved, reject pre-checkout. Reservation expiry is handled by a job using guarded updates; it never releases inventory belonging to a paid order.

### External payment confirmation

After authenticating and validating the Telegram update, one database transaction:

1. Insert the provider event using its unique key. A conflict returns the previously recorded result.
2. Lock payment attempt, checkout, order, and reservation.
3. Verify payer, provider charge id, currency and exact amount.
4. Mark payment paid once.
5. Convert the reservation to sold, or make one guarded replacement allocation if a late payment arrived after expiry.
6. Confirm the order and order item once.
7. Insert the fulfillment job and notification outbox entries using unique logical keys.
8. Insert the purchase audit event.

If a paid finite-stock order cannot be allocated, commit `refund_pending` plus a durable refund job. Never discard or merely log a paid-but-unfulfilled event.

### Wallet purchase

One serializable or explicitly locked transaction:

1. Resolve the request idempotency key.
2. Lock product/inventory, promotion usage, and wallet account in a consistent order.
3. Revalidate price and stock.
4. Create the order once.
5. Insert one debit ledger entry and guard against a negative balance.
6. Allocate/sell inventory once.
7. Insert fulfillment, notification, referral-pending, and audit records.

Any failure rolls back the entire purchase. Repeating the request returns the existing order and never debits again.

### Delivery

Delivery is at-least-once because a Telegram timeout can occur after Telegram accepted the message. Exactly-once external delivery cannot be promised.

The worker claims a fulfillment job with a lease, loads the already assigned artifact, decrypts it only in memory, and sends it. Every retry sends the same artifact; it never allocates another item. Success stores the Telegram message id and marks fulfillment delivered. Ambiguous or repeated failures move to retry and then manual review. No secret appears in logs, job payloads, analytics, or normal admin list responses.

### Refund

The admin request first commits a refund intent with a unique idempotency key. A worker then calls the provider.

- Confirmed provider success atomically marks payment/order refund state, inserts the financial event, credits a wallet exactly once when applicable, quarantines unique inventory, revokes an entitlement where supported, and writes an audit event.
- Timeout or unknown provider outcome becomes `review_required`; it is reconciled before another provider call.
- Provider failure is recorded and safely retryable only when the provider result proves no refund occurred.

The system never performs a provider refund first and attempts to create the local record afterward.

## API and permission boundaries

- Telegram identity comes only from authenticated Telegram webhook data, never from a request body field supplied by a user.
- The webhook requires both an unguessable path token and Telegram's secret-token header. Production startup fails if either is absent.
- Admin endpoints validate the Supabase JWT server-side and then require an active `admin_users` record and permission.
- The Supabase service-role key exists only in server configuration and is never sent to the dashboard.
- All modifying admin calls require an idempotency/request id and create an audit event transactionally.
- Stock list endpoints return masked metadata, never ciphertext or plaintext. Revealing or resending an assigned secret is a separate permissioned, audited action.
- Rate limits apply per Telegram user, update type, IP where meaningful, and admin identity. Payment callbacks rely on signature verification and idempotency, not rate limiting alone.
- Logs contain correlation ids and identifiers, not tokens, invoice payloads, inventory values, authorization headers, or raw webhook bodies.

## Failure recovery

- Database commits are the source of truth; API responses and Telegram messages are consequences.
- The outbox guarantees committed notifications are retried after restart.
- Reservation expiry, delivery, refunds, reconciliation, referral release, and supplier actions are durable jobs.
- Jobs use bounded exponential backoff and end in a visible dead-letter/manual-review state.
- A scheduled reconciliation job finds paid orders without fulfillment jobs, stale reservations, uncertain refunds, expired leases, and provider events without resolved payments.
- Health endpoints distinguish process liveness from database readiness. Worker health reports oldest queued job age without exposing data.
- Daily database backups and periodic restore drills are required before launch.

## Migration and cutover

Use an additive migration followed by a short maintenance cutover. Do not dual-write two commerce cores.

1. Back up SQLite, Supabase, encryption keys, and deployment configuration; verify a restore in a non-production database.
2. Add the canonical schema without deleting existing tables.
3. Implement canonical domain services and tests while sales remain on the old path.
4. Import users, products, encrypted inventory, orders, payment identifiers, refunds, and terms acceptances with reconciliation reports. Preserve ciphertext/key versions where possible.
5. Deploy the API and worker with sales disabled and validate Telegram webhook authentication, admin auth, job recovery, and read-only migrated data.
6. Enter a maintenance window, disable both old sales paths, run the final delta import, reconcile stock/payment counts, and register the new webhook.
7. Enable canonical sales for owner/test accounts, run a real low-value purchase/refund/delivery check, then enable general sales.
8. Migrate the dashboard to canonical endpoints.
9. After the rollback window, archive `reseller-backend`, remove Cloudflare Worker commerce logic, and make the local polling runtime development-only.

Rollback before general sales restores the prior webhook and database snapshot. After canonical orders are accepted, rollback is forward recovery from the canonical database; old runtimes must not resume writes against divergent state.

## Phase 2 acceptance criteria

- One application service owns each business rule and one PostgreSQL database is authoritative.
- A repeated checkout, payment event, wallet request, refund request, or job completion has a defined idempotent result.
- Unique inventory can be assigned to no more than one order item.
- Amounts and currencies are verified against server records and never use floating point.
- Payment, order, inventory, wallet, fulfillment job, and audit changes share explicit transaction boundaries.
- Failed or ambiguous external effects enter a recoverable state instead of being treated as success.
- Unused inventory secrets are absent from ordinary APIs, logs, analytics, and list views.
- Product behavior is selected through validated fulfillment type and inventory policy fields.
- Admin authorization and audit ownership are server-side.
- The design requires no microservice, Redis, or broker for initial launch.

## Deferred until later phases

- Dashboard information architecture and visual design
- Exact hosting vendor and production sizing
- Supplier-specific enablement and legal approval
- Advanced analytics and accounting exports
- Multi-store or multi-tenant support
- Native cryptocurrency or card providers beyond explicitly approved payment integrations

