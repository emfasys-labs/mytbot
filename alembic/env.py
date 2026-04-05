"""
Alembic environment — uses ``storage.models.Base.metadata`` for autogenerate.
Database URL is built from the same ``POSTGRES_*`` variables as the app (sync psycopg2 URL).
"""

from __future__ import annotations

import os
from logging.config import fileConfig
from urllib.parse import quote_plus

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import engine_from_config, pool

from storage.models import Base

load_dotenv()

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _sync_database_url() -> str:
    host = os.getenv("POSTGRES_HOST", "localhost")
    port = os.getenv("POSTGRES_PORT", "5432")
    db = os.getenv("POSTGRES_DB", "mytbot")
    user = quote_plus(os.getenv("POSTGRES_USER", "mytbot"))
    password = quote_plus(os.getenv("POSTGRES_PASSWORD", ""))
    return f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{db}"


def run_migrations_offline() -> None:
    url = _sync_database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    config.set_main_option("sqlalchemy.url", _sync_database_url())
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
