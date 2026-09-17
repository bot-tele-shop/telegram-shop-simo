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
            assert revision == "0001_bootstrap"
            assert schema == "digital_shelf"
        finally:
            await engine.dispose()

    asyncio.run(verify())
