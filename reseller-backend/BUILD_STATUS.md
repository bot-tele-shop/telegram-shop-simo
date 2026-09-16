# Build status — offline safety slice

**Status:** locally validated, simulation-only core. This is not production-ready and does not claim completion of the 25 release acceptance tests or a remote CI run.

## Fresh local evidence

All checks used a real local PostgreSQL 15 test database through an explicitly supplied `TEST_DATABASE_URL`; every pytest case migrated and removed its own generated `test_velmora_*` schema.

- `pip install --force-reinstall -e '.[test]'`: passed.
- `ruff check .`: **All checks passed**.
- `python -m compileall -q velmora`: **passed** (exit 0).
- `pytest -p no:cacheprovider`: **12 passed in 43.96s**.
- A fresh `demo_velmora_validation_20260917` schema migrated to `0001_initial`, then the redacted offline demo completed with exactly **one mock request, one mock debit, and one delivered stored result**. That schema was explicitly removed afterwards. Details: `test-evidence/`.

## Corrected, tested controls

- Tests require `TEST_DATABASE_URL`, never silently use `DATABASE_URL`, and only clean generated test schemas. README/.env/CI now show a portable localhost Compose URL, explicit environment export requirement, project install, exposed CI PostgreSQL port, lint/compile/test stages. CI is authored only—not remotely executed.
- A content-conflicting duplicate charge changes its recorded payment and order to review and cancels queued purchase work atomically; an in-flight attempt is deliberately not claimed cancelled. Worker purchase checkpoints lock and revalidate the selected eligible payment, so an injected unpaid job cannot call the supplier.
- Job claims now use persisted random claim tokens. Purchase identity creation, hold, delivery, and outcome state transitions fence on the current token under row locks, while supplier/messenger calls occur without a PostgreSQL transaction. The overlap test blocks an actual mock call, expires/reclaims its lease, and proves one identity/debit plus encrypted stale response evidence without a stale state overwrite. A subprocess test recreates engine/worker state around the durable checkpoint.
- Buyer IDs are `BIGINT`; supplier price is `Decimal`; Stars price validation rejects floats/bools/non-positive values. Invalid financial event amounts remain stored as review evidence rather than becoming eligible. Fresh migration includes declared runtime indexes, validated schema identifiers, claim token, and encrypted response-evidence fields.
- Demo refuses a non-`demo_` schema or nonempty state and never unlinks an existing supplied ledger. The SQLite mock takes an early `BEGIN IMMEDIATE` writer lock, with a concurrent replay test proving one debit.

## Included simulated coverage

Fresh migration/index/type agreement; large buyer ID through delivery/retrieval; duplicate/conflicting/distinct/later/wrong/currency/amount payment treatment; PostgreSQL concurrent intake/claims and lock ordering; unpaid-job fencing; timeout-after-debit/pending/malformed outcomes; durable subprocess checkpoint recovery; controlled stale-lease overlap; encrypted delivery retry/ownership/wrong-key behavior; mock idempotency/concurrency; and live-mode construction rejection.

## Important limitations

Telegram/aiogram updates, invoice/pre-checkout timing, authenticated Stars intake/refunds/reconciliation, real Canboso integration/idempotency, real catalog/balance checks, admin/support UI, rate limiting, budgets, retention deletion, backup restoration, deployment, and all live acceptance tests remain deferred. Supplier interruption/lease loss remains conservative: it preserves encrypted evidence and holds the order for review rather than automatically replaying or assuming an in-flight call was cancelled. Docker/Compose configuration remains untested because Docker was unavailable.
