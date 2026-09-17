# Production Hardening and Verification Contract

Status: Phase 4 launch-safety specification. Every control is **unverified** until its evidence has been produced against the canonical implementation described in `TARGET_ARCHITECTURE.md`.

## Task contract

**Goal:** define the minimum controls and evidence required before real customers and real money use the store.

**In scope:** payments, inventory, wallet, delivery, refunds, security, privacy, reliability, observability, migrations, backups, deployment, rollback, and dangerous-path tests.

**Out of scope:** implementing these controls, claiming current tests satisfy them, supplier enablement, performance at hypothetical large-enterprise scale, and jurisdiction-specific legal advice.

**Required verification:** automated CI, PostgreSQL integration tests, staging recovery exercises, a clean-environment deployment, and a controlled live Telegram Stars purchase/refund.

**Stop condition:** each launch-critical risk has an owner, prevention/recovery behavior, a test level, and an evidence artifact. Later phases may mark a control verified only after executing that evidence.

## Verification levels

| Level | Meaning | Suitable evidence |
|---|---|---|
| Unit | Pure decision or validation behavior | Deterministic automated test |
| Integration | Real database constraints, transactions, migrations or adapter contract | Test against disposable PostgreSQL/object storage |
| End-to-end | Multiple real application boundaries | Running API, worker, database, fake boundary server or provider sandbox |
| Live smoke | Production credentials and provider behavior | Controlled low-value owner transaction with recorded identifiers |
| Operational drill | Recovery procedure works after an induced failure | Timestamped runbook record and resulting database/metric evidence |

Mocks are acceptable for unit tests around Telegram formatting and provider error classification. They are not evidence for database atomicity, SQL constraints, migration safety, webhook authentication, or restart recovery.

## Assets and trust boundaries

Protect:

- Customer Telegram identity and support content
- Money, Stars charges, wallet balances and refund state
- Unique inventory secrets and private download files
- Product pricing and promotion rules
- Admin sessions, roles and privileged actions
- Provider tokens, webhook secrets, encryption keys and service-role credentials
- Audit, payment and order evidence

Trust boundaries:

```text
Telegram -> public webhook -> API validation -> domain transaction -> PostgreSQL
Browser -> Supabase Auth -> admin API authorization -> domain transaction
Worker -> leased job -> Telegram/storage/supplier -> recorded outcome
CI/deployer -> secret store -> migration/deployment runtime
```

Data received from Telegram, browsers, uploaded files, storage callbacks, suppliers, environment variables and database legacy rows is untrusted until validated.

## Launch-critical control matrix

### Payments and checkout

| ID | Required control | Failure behavior | Verification |
|---|---|---|---|
| PAY-01 | Validate Telegram secret-token header and unguessable webhook path; production refuses to start without both | Reject before parsing or mutating state | Integration requests with missing, wrong and correct secrets |
| PAY-02 | Invoice payload is an opaque server-issued checkout id, not trusted price/product data | Unknown or expired checkout is rejected | Unit and API integration tests |
| PAY-03 | Pre-checkout verifies customer, product version, exact amount, currency, order state, terms and availability from server records | Reject pre-checkout with customer-safe reason | PostgreSQL integration matrix |
| PAY-04 | Provider event id and charge id have database uniqueness constraints | Duplicate returns the original result without a second mutation | Concurrent integration test using separate DB connections |
| PAY-05 | Payment confirmation, order confirmation, inventory finalization, job creation and audit insertion are one transaction | Roll back all local changes on any local failure | Fault-injection integration tests at every write boundary |
| PAY-06 | A confirmed payment without fulfillable stock becomes durable `refund_pending`/review work | Never acknowledge it as delivered or discard it | Integration and worker E2E test |
| PAY-07 | Reconciliation finds provider/local mismatches, paid orders without jobs and unresolved events | Create visible review work; never invent success | Scheduled-job integration test plus staging drill |
| PAY-08 | Amounts use integer minor units and currencies must match exactly | Mismatch is quarantined for review | Unit/property tests and DB checks |
| PAY-09 | Repeated buy callback resolves the existing active checkout/order | Return existing invoice/order | Bot E2E test with repeated callback id |

### Inventory and product fulfillment

| ID | Required control | Failure behavior | Verification |
|---|---|---|---|
| INV-01 | Unique stock allocation uses a guarded update or locked `SKIP LOCKED` claim in PostgreSQL | One buyer wins; the other gets unavailable/no-stock | Two-connection race test repeated enough to exercise scheduling |
| INV-02 | An inventory item can reference only one order item and `(product, fingerprint)` is unique | Constraint rejects double assignment/import | Migration/schema and integration tests |
| INV-03 | Reservations have an expiry and are released only when unpaid | Paid/sold reservations are never released | Clock-controlled integration tests |
| INV-04 | Late payment after reservation expiry either claims another item once or creates refund work | No oversell and no lost payment | Integration test for both remaining-stock and no-stock cases |
| INV-05 | Bulk upload validates before an atomic insert and deduplicates against existing and same-file values | Entire write rolls back on unexpected failure | Real-DB upload integration test with injected failure |
| INV-06 | Unused values are encrypted with versioned keys and HMAC fingerprints | Decryption failure moves affected work to review without logging the value | Encryption round-trip, wrong-key and rotation tests |
| INV-07 | Normal APIs, logs, analytics, errors and exports omit ciphertext and plaintext values | Request fails closed if a serializer would expose a protected field | Response-schema tests, log capture and security review |
| INV-08 | Delivery retries reuse the assigned artifact | Retry never allocates or decrements stock again | Worker restart/retry integration test |
| INV-09 | Fulfillment/inventory policy combinations are validated centrally | Product stays draft and cannot be sold | Product service and API tests for every supported strategy |

### Wallet, promotions and referrals

| ID | Required control | Failure behavior | Verification |
|---|---|---|---|
| WAL-01 | Wallet debit, order, stock, fulfillment job and ledger entry share one transaction | Complete rollback on any failure | Real-DB fault-injection integration test |
| WAL-02 | Wallet mutation idempotency key is unique and balance cannot become negative | Duplicate returns original entry; insufficient funds rejects | Concurrent double-spend integration test |
| WAL-03 | Every admin adjustment has actor, reason, immutable ledger entry and audit event | Missing reason/permission rejects entire action | Admin API integration tests |
| PRO-01 | Promotion limits and redemption are checked/recorded in the purchase transaction | Concurrent last use has one winner | Two-connection race test |
| REF-01 | Referred user is unique, cannot refer self, and circular attribution is rejected | Referral remains unattributed or held | Unit plus database constraint tests |
| REF-02 | Rewards remain pending until the purchase clears its fraud/refund hold and are released once | Duplicate job does not double credit | Job idempotency and refund-before-release tests |

### Delivery and refunds

| ID | Required control | Failure behavior | Verification |
|---|---|---|---|
| DEL-01 | Fulfillment job is committed with the purchase and claimed using lease plus fencing token | Stale worker cannot complete a newer lease | PostgreSQL lease-race integration test |
| DEL-02 | Worker restart after claim makes the expired job claimable | Same assigned content is retried | Kill/restart E2E drill |
| DEL-03 | Telegram timeout after possible send is treated as ambiguous, not proof of failure | Retry same artifact and record each attempt | Fake Telegram server E2E test |
| DEL-04 | Delivered is terminal except explicit refund/revocation transitions | Later failed resend cannot regress delivered state | Database transition tests |
| DEL-05 | Retry count, backoff and dead-letter threshold are bounded | Exhausted job becomes visible manual work | Clock-controlled worker test |
| REFUND-01 | Refund intent is committed before provider call and has a unique request key | Repeated click returns same refund record | API/integration test |
| REFUND-02 | Provider timeout/unknown response enters review and blocks blind retry | Owner sees unknown outcome and reconciliation action | Provider fault E2E test |
| REFUND-03 | Confirmed refund, financial event, order state, wallet credit where applicable, inventory quarantine and audit are atomic | Roll back local transition on local failure | Real-DB fault-injection test |
| REFUND-04 | Refund reconciliation can resolve success, confirmed failure and still-unknown outcomes | Only proven failure may be safely retried | Adapter contract and staging drill |

### Authentication, authorization and abuse

| ID | Required control | Verification |
|---|---|---|
| SEC-01 | Supabase JWT is validated server-side for issuer, audience, signature, expiry and subject | Invalid, expired, wrong-project and missing JWT integration tests |
| SEC-02 | Active admin membership and permission are checked on every endpoint | Role-by-endpoint deny matrix; hidden UI is not counted as security |
| SEC-03 | Customer identity comes only from the authenticated Telegram update context | Manipulated user/order callback tests |
| SEC-04 | Every mutation validates schema, length, enum, identifier, amount and state transition | Boundary/fuzz tests with realistic malformed payloads |
| SEC-05 | Database access uses parameters/query builders; dynamic sort/filter fields use allowlists | Static review plus malicious filter/search integration tests |
| SEC-06 | Browser API allows only configured origins and headers; service-role credentials never reach browser assets | Deployed header/config inspection |
| SEC-07 | State-changing requests use bearer authorization and unique request ids; browser tokens are not placed in URLs or logs | Browser/API integration test and log capture |
| SEC-08 | Rate limits cover user commands, callbacks, checkout creation, admin login/API and broadcast creation | Burst tests prove bounded behavior without breaking payment callbacks |
| SEC-09 | Uploads enforce size, encoding, line count/type and safe object metadata; uploaded files are never executed | File and inventory upload abuse tests |
| SEC-10 | Secrets come from the deployment secret store; production rejects defaults/placeholders | Startup configuration tests and deployed secret inventory |
| SEC-11 | Logs redact authorization, provider payloads, Telegram raw bodies, inventory values and supplier credentials | Automated log-capture assertions for success and failure paths |
| SEC-12 | Dependency lock, vulnerability scan, secret scan and license review run in CI | CI artifacts with approved or remediated findings |
| SEC-13 | Supplier purchasing defaults off and requires explicit resale authorization, spend cap and successful adapter verification | Production config assertion and supplier-disabled purchase test |

### Privacy and data governance

| ID | Required control | Verification |
|---|---|---|
| PRIV-01 | Data inventory classifies Telegram identity, commerce records, support content, secrets and telemetry | Reviewed data map linked from operations documentation |
| PRIV-02 | Only fields needed for fulfillment, support, fraud prevention and accounting are collected | API/schema review; no raw update body retained as a convenience copy |
| PRIV-03 | API read models minimize personal and secret data by role | Role response snapshots and field-deny assertions |
| PRIV-04 | Customer export returns the customer's applicable profile, order, wallet and support data without inventory secrets or other users | Integration test with two users |
| PRIV-05 | Deletion/pseudonymization behavior preserves required financial/audit integrity and removes optional profile/support data according to the approved policy | Staging exercise with post-operation queries |
| PRIV-06 | Retention periods for raw ingress failures, support content, audit, payment and order records are approved for the operating jurisdiction before launch | Configuration/runbook review; unresolved policy is a launch blocker |

Do not invent legal retention periods in code. The owner must approve a jurisdiction-appropriate policy before the implementation is marked launch-ready.

## Reliability and dependency behavior

| Dependency/failure | Required behavior | Required evidence |
|---|---|---|
| PostgreSQL unavailable | API readiness fails; webhook receives retryable server response; no false success | E2E outage test |
| Database connection lost mid-transaction | Transaction rolls back; duplicate Telegram delivery safely resumes from idempotency key | Fault-injection integration test |
| API process restarts | Committed updates/jobs remain; uncommitted work is retried | Restart drill |
| Worker process restarts | Expired leases are reclaimed with fencing; jobs are not lost | Restart drill with running job |
| Telegram send timeout | Delivery remains ambiguous/retryable with same artifact | Fake-server timeout test |
| Telegram permanent rejection | Bounded attempts, sanitized reason, owner work item | Adapter test |
| Object storage unavailable | File delivery retries; order remains paid and undelivered | E2E dependency outage test |
| Auth provider unavailable | Existing valid local verification policy is applied; unsafe admin mutation is denied | Admin API outage test |
| Supplier timeout | Purchase becomes uncertain/manual review; no blind second purchase | Adapter and job E2E test before supplier enablement |
| Notification failure | Commerce transaction stays committed; outbox retries | Integration test |
| Analytics query failure | Commerce and owner recovery actions remain usable; metrics show delayed state | Dashboard/API degraded-state test |

Retries use bounded exponential backoff with jitter. Permanent validation/authentication errors are not retried. External mutations are retried only with provider-supported idempotency or after reconciliation proves the prior attempt did not succeed.

## Observability contract

### Structured events

Every log line uses structured fields and a correlation id. Relevant safe identifiers include order reference, payment/refund record id, job id, product id, endpoint, outcome and duration. Do not use customer names, message bodies, raw update payloads, inventory values, tokens, or full provider responses as labels or log fields.

Required business events:

- checkout created/rejected
- pre-checkout accepted/rejected by reason class
- payment verified/duplicate/anomalous
- inventory reserved/sold/released/quarantined
- wallet entry committed/rejected
- fulfillment queued/attempted/delivered/dead-lettered
- refund requested/unknown/confirmed/failed
- reconciliation issue opened/resolved
- admin sensitive action allowed/denied

### Metrics

- HTTP requests, errors and latency by bounded route/status class
- Webhook accepted, rejected, duplicate and processing duration
- Payments confirmed, anomalous and awaiting reconciliation
- Orders paid but not delivered, with oldest age
- Job queue depth, oldest age, attempts, success, retry and dead-letter counts by bounded job type
- Available stock and low-stock product count
- Refunds pending/unknown and oldest age
- Dependency call latency/error rate by dependency and operation class
- Database pool saturation and migration version

Customer ids, order ids, error messages and SKUs with unbounded variety are not metric labels.

### Alerts

Alerts must be actionable and link to the owner/admin filtered view or operator runbook:

- Any paid order without a fulfillment job
- Paid delivery older than the agreed delivery target
- Any unknown refund outcome
- Payment anomaly queue older than the review target
- Dead-letter job count greater than zero
- Webhook authentication failures above abuse threshold
- Database readiness failure
- Backup failure or restore verification overdue
- Low stock according to per-product thresholds

Exact numeric thresholds are set after staging measurements and documented before launch. Missing thresholds are a launch blocker, not silently chosen by code.

## Database and migration gate

The canonical migration path is `add -> backfill -> switch readers/writers -> verify -> remove later`.

Required checks:

1. Apply all migrations to an empty PostgreSQL database.
2. Apply them to a fixture representing legacy Supabase data, including expired, failed, duplicate-like and partially populated rows.
3. Run backfills twice and prove idempotency.
4. Verify foreign keys, checks, unique constraints, partial indexes and transition guards.
5. Compare source and target counts and monetary/stock reconciliation totals.
6. Run application integration tests against the migrated schema.
7. Confirm the previous application remains compatible until the cutover point for additive migrations.
8. Record irreversible steps; destructive cleanup happens only after the rollback window.

Migration execution uses a dedicated least-privilege deployment credential, not the browser key and not an ad hoc owner session.

## Backup and recovery gate

Before launch:

- Enable encrypted daily database backups and point-in-time recovery if the provider supports it.
- Back up object-storage metadata and ensure private product files have a recovery source.
- Store encryption keys in a separate secret manager with version and recovery ownership documented.
- Export deployment configuration names without secret values.
- Restore the database into an isolated environment.
- Verify products, available/reserved/sold stock totals, orders, payments, wallet balances, refunds, jobs and audit counts after restore.
- Decrypt a controlled test inventory item with each active key version.
- Run a read-only purchase reconciliation against the restored data.

A backup that has never been restored is not accepted as launch evidence.

Recovery objectives and retention must be selected based on the owner's acceptable data-loss and downtime limits before hosting is finalized.

## Automated test plan

### Unit suite

Cover state-transition rules, money arithmetic, quote validation, promotion calculation, referral eligibility, retry classification, secret redaction, permission decisions, fulfillment-policy validation and provider response parsing.

### PostgreSQL integration suite

Run in CI against an isolated PostgreSQL service with independent connections. It must include:

1. Two customers buying the last unique item simultaneously
2. Two wallet purchases racing for the same balance
3. Duplicate payment events delivered concurrently
4. Duplicate provider charge under different event ids
5. Repeated Telegram callback creating one checkout/order
6. Promotion final use raced by two orders
7. Reservation expiry racing payment confirmation
8. Late successful payment with and without replacement stock
9. Failure after each local purchase write proving complete rollback
10. Delivery lease expiry and stale-worker fencing
11. Refund request duplication and local atomic completion
12. Referral reward job executed twice
13. Atomic bulk stock upload with duplicate and injected failure
14. Unauthorized role attempts for every sensitive admin endpoint
15. Migration from representative legacy fixtures

### End-to-end suite

Run the API, worker and PostgreSQL together. Use a controllable fake Telegram/storage server for deterministic faults:

- `/start -> browse -> product -> checkout -> payment -> delivery -> history`
- API restart between webhook receipt and commit
- Worker restart before and after provider send
- Telegram timeout after accepting a delivery
- Payment succeeds while delivery is unavailable
- Refund succeeds, fails and returns unknown
- Storage outage during file delivery
- Admin creates product, imports stock, publishes, finds order, retries delivery and requests refund
- Browser role restrictions and session expiry
- Broadcast pause/resume and rate limiting

### Live smoke

Before general sales, use an owner-controlled account and low-value product:

1. Verify webhook secret configuration.
2. Purchase with Telegram Stars.
3. Confirm exactly one charge, order, sold item, fulfillment job, delivery and audit event.
4. Restart the worker and confirm no repeat delivery is generated for a completed job.
5. Request a refund and wait for confirmed provider/local agreement.
6. Confirm the item is quarantined and cannot be resold.
7. Record identifiers and timestamps in a redacted launch evidence report.

## CI quality gates

Every pull request:

- Locked dependency installation
- Python lint and formatting check
- Type checking for the canonical application
- Unit tests
- PostgreSQL integration tests including migrations
- Dashboard lint/unit tests
- Browser tests for critical admin workflows
- Secret scanning
- Dependency vulnerability scanning
- Container build and non-root/runtime configuration checks

Main-branch deployment requires all gates and an immutable image digest. Production migration and deploy are separate explicit jobs with environment approval. The dashboard and API are deployed against compatible versioned contracts.

Checked-in test counts are not launch evidence. CI must publish machine-readable test results tied to the exact commit and image digest.

## Release and rollback gate

### Pre-deploy

- Current backup and successful restore evidence
- Migration dry run against representative data
- Required secrets/configuration validated without printing values
- Sales paused for cutover
- New image and migration identifiers recorded
- Old webhook target and rollback command recorded
- On-call owner and stop conditions agreed

### Staged release

1. Apply additive migrations.
2. Deploy API and worker with general sales disabled.
3. Verify liveness, readiness, migration version and queue processing.
4. Switch webhook and validate authenticated test update.
5. Enable owner/test-account purchase only.
6. Complete the live smoke purchase and refund.
7. Enable general sales and observe the defined metrics.

### Immediate rollback/stop conditions

- Payment recorded more than once
- Wallet debited more than once or below zero
- One inventory item assigned to multiple orders
- Confirmed payment missing from local records
- Paid order lost without delivery/refund work
- Inventory secret appears in logs or normal API responses
- Admin authorization bypass
- Migration reconciliation mismatch
- Persistent webhook or database readiness failure

Before general sales, rollback may restore the old webhook/application and pre-cutover snapshot. After canonical sales begin, do not reactivate an old writer against divergent state. Pause checkout and forward-fix or restore the canonical system according to the incident runbook.

## Required evidence bundle

The launch audit must receive:

- Commit and container image digest
- CI results for unit, integration, E2E, browser, security and migration jobs
- Schema/migration version and reconciliation report
- Redacted live Stars purchase/refund record
- Backup restore drill record
- Worker/API restart drill record
- Secret/configuration checklist without values
- Dependency and license scan results
- Dashboard role/permission test matrix
- Monitoring dashboard and alert test evidence
- Approved retention policy and incident contacts
- Known limitations and explicitly disabled integrations

## Current-repository evidence status

The existing local test suite provides useful behavioral examples for duplicate payments, same-item delivery retries, refund authorization and supplier uncertainty. The Worker tests largely mock database RPC behavior. The current CI does not start PostgreSQL, apply migrations, exercise concurrent SQL transactions, build the target container, run browser tests, scan dependencies/secrets, or perform restore/restart drills.

Therefore, none of the launch-critical controls in this document should be marked verified solely because the existing test suite passes.

## Phase 4 acceptance criteria

- Every dangerous path named in the project brief maps to a control and executable verification.
- Transactional invariants are proved against PostgreSQL rather than mocked RPC results.
- External partial-success and unknown outcomes have explicit recovery behavior.
- Security checks cover authentication, authorization, untrusted input, secrets, logs and dependencies.
- Privacy checks cover minimization, role-based access, export, deletion/pseudonymization and owner-approved retention.
- Operational evidence includes restore and restart drills, not only tests.
- Deployment has predefined stop and rollback conditions.
- No control is described as verified before its evidence is executed against the canonical implementation.

