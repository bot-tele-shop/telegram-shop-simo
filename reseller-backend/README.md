# Velmora reseller — offline safety slice

A deliberately **simulation-only** Python/PostgreSQL vertical slice for one approved digital product. It accepts internal simulated payment events only. It has no Telegram transport, public payment endpoint, supplier credential, real supplier adapter, or live spending path. Non-`simulation` provider construction fails closed with `UnsafeModeError`.

## Implemented safety slice

- PostgreSQL/Alembic schema: products, exact price-snapshotted orders, distinct payment/charge evidence, one supplier purchase identity per order, fenced leased jobs, encrypted deliveries, and audit records. Telegram-sized buyer IDs are PostgreSQL `BIGINT`.
- Five-minute XTR integer quote and review retention for unknown, wrong, late, duplicate, and anomalous financial events. A conflicting duplicate charge moves the affected order to review and cancels only queued purchase work; an already-running network attempt is retained for review rather than assumed cancelable.
- Each claim has a persisted random token. Every purchase/delivery state-change checkpoint validates that token under row locks, and purchase creation additionally validates the currently selected eligible payment (order/buyer/amount/currency). Database locks are never held during mock supplier/messenger calls.
- Independent SQLite **mock** supplier ledger with serialized replay, calls separate from debits, and complete/no-debit/pending/timeout-after-debit/malformed outcomes. It is not the business database and its digital values are explicitly non-real.
- Fernet encrypted delivery storage; delivery retry sends the stored value only. Retrieval checks the saved buyer ID. Demo/test reports do not print digital values.

## Local setup (Compose or your own PostgreSQL)

Python 3.12 and PostgreSQL 15 are required. The commands activate the venv, so all later commands resolve to the installed project tools:

```sh
python3.12 -m venv .venv
. .venv/bin/activate
pip install -r requirements.lock
pip install -e .
cp .env.example .env  # edit values, then export them: there is no dotenv loader
set -a; . ./.env; set +a
```

For the provided localhost-only Compose PostgreSQL, start it (Docker is not validated in this environment), then create a separate disposable test database once:

```sh
docker compose up -d postgres
PGPASSWORD=change-me-local-only createdb -h 127.0.0.1 -p 54329 -U velmora velmora_test
```

The sample `DATABASE_URL` in `.env.example` matches `compose.yaml`. Use an isolated, valid schema name—not `public`, a `pg_*` name, or a name containing punctuation:

```sh
export DATABASE_SCHEMA=demo_velmora_example
alembic upgrade head
```

## Tests, lint, and migration proof

`TEST_DATABASE_URL` is deliberately required by the test fixture. It must point to a disposable PostgreSQL database; tests never fall back to `DATABASE_URL` and create/drop only random `test_velmora_*` schemas.

```sh
export TEST_DATABASE_URL='postgresql+psycopg://velmora:change-me-local-only@127.0.0.1:54329/velmora_test'
ruff check .
python -m compileall -q velmora
pytest -p no:cacheprovider
```

For an independent fresh migration check, use another empty schema/database and inspect its revision:

```sh
export DATABASE_URL='postgresql+psycopg://velmora:change-me-local-only@127.0.0.1:54329/velmora_test'
export DATABASE_SCHEMA=demo_migration_proof
alembic upgrade head
psql 'postgresql://velmora:change-me-local-only@127.0.0.1:54329/velmora_test' -c 'SELECT version_num FROM demo_migration_proof.alembic_version;'
```

The socket peer `root` URL used in this task's sandbox evidence is **not** a user/deployment instruction. If reproducing only that isolated sandbox validation, set `TEST_DATABASE_URL` explicitly to its supplied socket URL.

## Safe offline demo

Migrate a fresh, empty `demo_` schema, then run the demo. It refuses non-`demo_` schemas and nonempty demo state. By default it creates a new temporary ledger workspace; if `SIMULATED_LEDGER` is set, it must name a path that does not yet exist. The demo never deletes an existing ledger or schema.

```sh
export DATABASE_URL='postgresql+psycopg://velmora:change-me-local-only@127.0.0.1:54329/velmora_test'
export DATABASE_SCHEMA=demo_velmora_run_01
export FERNET_KEY="$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
export VELMORA_MODE=simulation
alembic upgrade head
python -m velmora.demo
```

## Deferred / not a production claim

Telegram/aiogram updates, Stars invoice/pre-checkout/authenticated intake/refunds/reconciliation, Canboso integration and its real idempotency contract, real catalog/balance checks, admin UI, rate limits, spending budgets, retention deletion, backup restoration, production deployment, and live acceptance tests are future work. Dockerfile/Compose are authored but untested here because Docker was unavailable. Lease fencing prevents stale database overwrites; an already-issued external call can remain uncertain and is held for review rather than automatically replayed.
