# Canonical Application Development

The canonical application is being built additively under `src/digital_shelf`. It is not connected to production traffic yet. See `PROJECT_STATE.md` and `IMPLEMENTATION_PLAN.md` before changing runtime behavior.

## Requirements

- Python 3.12
- uv 0.12.3
- PostgreSQL 15 or newer, or Docker with Compose
- Node.js for the existing WebCrypto regression tests

## Install

```bash
uv sync --frozen --group dev
```

Copy `.env.canonical.example` to `.env` for local canonical development. Never commit `.env` or real credentials.

## Database

Start the local database and apply migrations:

```bash
docker compose up -d postgres
uv run alembic upgrade head
```

The default local URL is `postgresql+asyncpg://telegram_shop:telegram_shop@localhost:5432/telegram_shop`.

## Checks

```bash
uv run ruff check shop tests worker/src tools scripts src migrations
uv run mypy src/digital_shelf
uv run python -m pytest -q
node --check dashboard/app.js
docker build -t digital-shelf:local .
```

Set `SHOP_TEST_DATABASE_URL` to a migrated disposable PostgreSQL database to execute the real-database integration test. CI always sets it.

## Run the scaffold

```bash
uv run uvicorn digital_shelf.api:app --host 127.0.0.1 --port 8000
uv run digital-shelf-worker
```

- `GET /live` proves only that the API process is running.
- `GET /ready` proves that the process can reach PostgreSQL.
- The worker intentionally has no commerce handlers in Slice 0.

Production startup requires independent webhook path and header secrets of at least 32 characters and a non-default database URL. The canonical application must not receive the Telegram webhook until the controlled cutover slice.
