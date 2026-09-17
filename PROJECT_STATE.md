# Project State

Last updated: 2026-09-18

## Current phase

Phase 5 planning is complete. **Slice 0: canonical application and development runtime** from `docs/IMPLEMENTATION_PLAN.md` is complete. The next canonical slice is Slice 1 (identity, users, roles and settings).

Implemented and locally verified so far:

- locked Python dependency environment
- canonical FastAPI and worker entry points
- production configuration fail-closed checks
- structured secret-redacting logs
- async SQLAlchemy/asyncpg database readiness
- Alembic bootstrap migration against PostgreSQL
- 767 legacy-plus-canonical tests (1 external database test skipped locally)
- lint, strict type checking and dashboard JavaScript syntax
- canonical container built successfully in GitHub CI on `main`
- committed branch CI execution passed on GitHub Actions

Current production UI work:

- `feature/complete-telegram-storefront` improves the active Cloudflare Worker menu,
  paginated real-stock catalog, persistent quick keyboard, buyer profile totals and order filters.
- This storefront branch is locally verified and committed. Push, PR, merge and deployment are
  blocked because the currently authenticated GitHub account has pull access but no push access.
- Production catalog content remains owner-managed; no products or stock are invented by code.

No canonical runtime has been implemented or cut over yet. The store must not be described as launch-ready.

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
