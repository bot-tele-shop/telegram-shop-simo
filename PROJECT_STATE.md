# Project State

Last updated: 2026-09-18

## Current phase

Phase 5 planning is complete. **Slice 0: canonical application and development runtime** from `docs/IMPLEMENTATION_PLAN.md` is merged. Release 1 identity, settings, roles, feature controls, and audit APIs are implemented locally on `feature/canonical-settings-features`, but have not passed the real-PostgreSQL CI gate or been deployed.

Implemented and locally verified so far:

- locked Python dependency environment
- canonical FastAPI and worker entry points
- production configuration fail-closed checks
- structured secret-redacting logs
- async SQLAlchemy/asyncpg database readiness
- Alembic bootstrap migration against PostgreSQL
- 830 legacy-plus-canonical tests passed locally (1 external database test skipped)
- lint, strict type checking and dashboard JavaScript syntax
- canonical container built successfully in GitHub CI on `main`
- earlier merged `main` branch CI execution passed on GitHub Actions; the current feature branch has not run CI

Current unmerged branch `feature/canonical-settings-features` additionally contains:

- the actionable plan in `docs/NEXT_STORE_ACTION_PLAN.md`
- additive canonical identity, roles, settings/history, feature, and audit tables
- Supabase asymmetric JWT verification and server-side admin permission checks
- revision-guarded, audited feature and store-setting commands
- owner-only minimized audit read API
- local tests, lint, strict typing, and Alembic offline upgrade/downgrade SQL checks

Release 1 is **not gated complete**. Docker/PostgreSQL is unavailable locally, so the real database integration test is pending. Both saved GitHub CLI account tokens are invalid, preventing push/PR/CI. No canonical migration or API route has been deployed to production.

Current production UI work:

- The improved Worker storefront from PR #11 was merged and deployed on `main`.
- Live checkout was paused in the existing dashboard as Release 0 containment; it should remain paused until a controlled payment/delivery/refund smoke test is authorized.
- Production catalog content remains owner-managed; no products or stock are invented by code.

The canonical API has local admin foundations but is not deployed or cut over. The store must not be described as launch-ready.

## Implementation classification

| Path | Classification | Change policy |
|---|---|---|
| `src/digital_shelf/` | Target canonical implementation; foundation created, not routed to production | New work proceeds by verified implementation slice |
| `dashboard/` | Active legacy dashboard, planned in-place migration | Critical fixes only until canonical admin contracts exist |
| `worker/` | Legacy webhook commerce implementation | Critical security/data-loss fixes only; no new features |
| `shop/` | Legacy local/polling implementation and source of proven inventory concepts | Critical fixes and extraction support only |
| `supabase/migrations/0001..0004` | Legacy production-candidate schema history | Preserve; do not add competing business behavior |
| `reseller-backend/` | Experimental reference | Frozen; remove after canonical cutover |

## Authoritative design documents

- `docs/TARGET_ARCHITECTURE.md`
- `docs/ADMIN_EXPERIENCE.md`
- `docs/PRODUCTION_HARDENING.md`
- `docs/IMPLEMENTATION_PLAN.md`

When current code or older documentation conflicts with these target documents, current production behavior remains unchanged until the relevant migration slice is verified and cut over. Target documents define what to build; they do not claim that behavior already exists.

## Safety rules during migration

- Do not run local polling and a Telegram webhook for the same bot token simultaneously.
- Do not dual-write legacy and canonical commerce state.
- Do not enable supplier purchasing during the canonical migration.
- Do not delete legacy tables/code before the rollback window ends.
- Do not mark Phase 4 controls verified without their stated evidence.
- Do not expose unused inventory values in APIs, logs, tests, reports or dashboard lists.
