from __future__ import annotations

import re

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

_SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
_RESERVED_SCHEMAS = {"public", "information_schema", "pg_catalog", "pg_toast"}


def validate_schema(schema: str) -> str:
    """Return a safe PostgreSQL schema identifier or fail before SQL interpolation."""
    if not isinstance(schema, str) or not _SCHEMA_RE.fullmatch(schema):
        raise ValueError("DATABASE_SCHEMA must be a nonempty PostgreSQL identifier (letters, digits, underscores)")
    lowered = schema.lower()
    if lowered in _RESERVED_SCHEMAS or lowered.startswith("pg_"):
        raise ValueError("DATABASE_SCHEMA may not name a reserved PostgreSQL schema")
    return schema


def make_engine(url: str, schema: str) -> Engine:
    schema = validate_schema(schema)
    # search_path scopes runtime statements to a validated, explicit schema.
    return create_engine(url, connect_args={"options": f"-csearch_path={schema}"}, pool_pre_ping=True)


def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)


def create_schema(engine: Engine, schema: str) -> None:
    schema = validate_schema(schema)
    with engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
