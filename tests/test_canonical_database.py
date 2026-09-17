"""Real PostgreSQL evidence for the canonical Slice 0 foundation."""

import asyncio
import os

import pytest
from sqlalchemy import text

from digital_shelf.db import create_engine, database_ready


@pytest.mark.integration
def test_bootstrap_migration_and_readiness_against_postgres() -> None:
    database_url = os.environ.get("SHOP_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("SHOP_TEST_DATABASE_URL is not configured")

    async def verify() -> None:
        engine = create_engine(database_url)
        try:
            assert await database_ready(engine)
            async with engine.connect() as connection:
                revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
                schema = await connection.scalar(
                    text(
                        "SELECT schema_name FROM information_schema.schemata "
                        "WHERE schema_name = 'digital_shelf'"
                    )
                )
                table_names = set(
                    (
                        await connection.execute(
                            text(
                                "SELECT table_name FROM information_schema.tables "
                                "WHERE table_schema = 'digital_shelf'"
                            )
                        )
                    ).scalars()
                )
                feature_rows = (
                    (
                        await connection.execute(
                            text(
                                "SELECT feature_key, requested_enabled, state "
                                "FROM digital_shelf.feature_flags ORDER BY feature_key"
                            )
                        )
                    )
                    .mappings()
                    .all()
                )
            assert revision == "0002_identity_settings_features"
            assert schema == "digital_shelf"
            assert {
                "users",
                "admin_users",
                "roles",
                "role_permissions",
                "admin_user_roles",
                "store_settings",
                "store_setting_revisions",
                "feature_flags",
                "audit_events",
            } <= table_names
            by_key = {row["feature_key"]: row for row in feature_rows}
            assert by_key["offers"]["state"] == "disabled"
            assert dict(by_key["low_stock_alerts"]) == {
                "feature_key": "low_stock_alerts",
                "requested_enabled": True,
                "state": "setup_required",
            }
        finally:
            await engine.dispose()

    asyncio.run(verify())
