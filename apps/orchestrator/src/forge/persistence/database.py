"""Explicit SQLAlchemy construction with no import-time connections."""

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def create_engine(database_url: str) -> AsyncEngine:
    """Construct an async engine without opening a connection."""

    return create_async_engine(database_url, pool_pre_ping=True)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Construct the shared async-session factory for an explicit engine."""

    return async_sessionmaker(engine, expire_on_commit=False)


async_session_factory = create_session_factory
