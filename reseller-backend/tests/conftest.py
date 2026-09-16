from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, text

from velmora.db import make_engine, session_factory, validate_schema

ROOT = Path(__file__).parents[1]


@pytest.fixture
def runtime(tmp_path):
    # Deliberately separate from DATABASE_URL: tests must never silently target a user's database.
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.fail("TEST_DATABASE_URL is required for PostgreSQL integration tests; it must name a disposable test database")
    schema = "test_velmora_" + uuid4().hex[:10]
    env = {**os.environ, "DATABASE_URL": url, "DATABASE_SCHEMA": schema}
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT, env=env, check=True,
                   capture_output=True, text=True)
    engine = make_engine(url, schema)
    yield {"engine": engine, "sessions": session_factory(engine), "key": Fernet.generate_key().decode(),
           "ledger": tmp_path / "supplier.sqlite", "url": url, "schema": schema}
    engine.dispose()
    # This is a generated, validated test_ schema only; no arbitrary schema/database is dropped.
    assert validate_schema(schema).startswith("test_")
    cleanup = create_engine(url)
    with cleanup.begin() as c:
        c.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
    cleanup.dispose()
