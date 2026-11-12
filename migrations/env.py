from logging.config import fileConfig
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.config import settings
from app.database import Base
from app import models  # noqa: F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def database_url():
    url = (settings.database_url_unpooled or settings.database_url).strip()
    if not url:
        return None

    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+asyncpg://", 1)
    elif url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)

    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)

    # Remove parameters unsupported by asyncpg.
    query = [(key, value) for key, value in query if key != "channel_binding"]

    # Translate PostgreSQL SSL parameter to asyncpg's parameter.
    query = [
        ("ssl" if key == "sslmode" else key, value)
        for key, value in query
    ]

    return urlunsplit((
        parts.scheme,
        parts.netloc,
        parts.path,
        urlencode(query),
        parts.fragment,
    ))


def run_migrations_offline():
    url = database_url() or config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection):
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online():
    url = database_url()
    if not url:
        raise RuntimeError("Set DATABASE_URL in .env before running migrations.")

    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = url

    connectable = async_engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    import asyncio
    asyncio.run(run_migrations_online())
