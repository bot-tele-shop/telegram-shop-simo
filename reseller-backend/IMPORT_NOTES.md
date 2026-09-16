# Import notes

This directory is an isolated, simulation-only offline reseller foundation imported under `reseller-backend/`. It provides the tested Python/PostgreSQL/Alembic safety slice and an independent mock supplier ledger; it has no real Telegram transport, Stars payment intake, or Canboso adapter yet. The source `.github/` workflow directory was intentionally not imported. Prior local test/demo evidence is historical context only; no CI has been run on the target branch.

Run setup, migration, tests, and the offline demo from this subdirectory. Read `README.md` for the explicit simulation-only setup and limitations.
