from __future__ import with_statement

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool, text

from velmora.db import validate_schema
from velmora.models import Base

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)
target_metadata = Base.metadata
schema = validate_schema(os.getenv("DATABASE_SCHEMA", "velmora"))


def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = os.getenv("DATABASE_URL", configuration["sqlalchemy.url"])
    connectable = engine_from_config(configuration, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
        connection.execute(text(f'SET search_path TO "{schema}"'))
        context.configure(connection=connection, target_metadata=target_metadata, version_table_schema=schema,
                          include_schemas=False, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
