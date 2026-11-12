
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import settings


NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def get_database_url() -> str:
    url = settings.database_url.strip()

    if not url:
        raise RuntimeError("DATABASE_URL has not been configured.")

    if url.startswith("postgres://"):
        url = url.replace(
            "postgres://",
            "postgresql+asyncpg://",
            1,
        )
    elif url.startswith("postgresql://"):
        url = url.replace(
            "postgresql://",
            "postgresql+asyncpg://",
            1,
        )

    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)

    query = [
        (key, value)
        for key, value in query
        if key not in ("channel_binding", "sslmode")
    ]

    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            parts.path,
            urlencode(query),
            parts.fragment,
        )
    )


_engine = None
_session_factory = None


def get_engine():
    global _engine

    if _engine is None:
        _engine = create_async_engine(
            get_database_url(),
            pool_pre_ping=True,
            pool_size=2,
            max_overflow=1,
            pool_timeout=15,
            pool_recycle=300,
            connect_args={
                "timeout": 15,
                "command_timeout": 30,
                "server_settings": {
                    "application_name": "remique-zero",
                },
            },
        )

    return _engine


def get_session_factory():
    global _session_factory

    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )

    return _session_factory


async def get_db_session():
    async with get_session_factory()() as session:
        yield session


async def close_database():
    global _engine, _session_factory

    if _engine is not None:
        await _engine.dispose()

    _engine = None
    _session_factory = None
