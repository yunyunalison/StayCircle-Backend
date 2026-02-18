import os
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool
from alembic import context

# Import application metadata so Alembic autogenerate can see models
from app.db import Base  # noqa: F401
from app import models  # noqa: F401

# Alembic Config object, provides access to the .ini file values
config = context.config

# Interpret the config file for Python logging.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Target metadata for 'autogenerate' support
target_metadata = Base.metadata


def get_url() -> str:
    """
    DATABASE_URL from environment, fallback to local SQLite (developer convenience).
    Examples:
      - sqlite:///./data.db
      - mysql+pymysql://staycircle:staycircle@mysql:3306/staycircle
    """
    return os.getenv("DATABASE_URL", "sqlite:///./data.db")


def run_migrations_offline() -> None:
    """
    Run migrations in 'offline' mode.
    """
    url = get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """
    Run migrations in 'online' mode.
    """
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = get_url()

    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        future=True,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            # Enable batch mode for SQLite to support ALTERs during dev
            render_as_batch=get_url().startswith("sqlite"),
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
