"""Database engine and readiness primitives."""

from sqlalchemy import MetaData, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    create_async_engine,
)

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(schema="digital_shelf", naming_convention=NAMING_CONVENTION)


def create_engine(database_url: str) -> AsyncEngine:
    """Create the process-level async engine without opening a connection."""

    return create_async_engine(database_url, pool_pre_ping=True)


async def database_ready(engine: AsyncEngine) -> bool:
    """Return whether a short database round trip succeeds."""

    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception:
        return False
    return True
