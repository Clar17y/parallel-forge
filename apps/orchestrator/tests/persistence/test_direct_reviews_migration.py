"""Slice C migration 0046: direct_reviews + REVIEWING."""

import asyncio

import pytest
from alembic import command
from sqlalchemy import text

PREVIOUS = "20261022_0045"
HEAD_REVISION = "20261022_0046"


async def _scalar(database_url: str, sql: str):
    from forge.persistence.database import create_engine
    engine = create_engine(database_url)
    try:
        async with engine.connect() as c:
            return await c.scalar(text(sql))
    finally:
        await engine.dispose()


@pytest.mark.integration
def test_direct_reviews_migration_up_down(test_database_url, alembic_config_factory):
    config = alembic_config_factory(test_database_url)
    command.upgrade(config, HEAD_REVISION)
    tables = asyncio.run(_scalar(test_database_url, "SELECT count(*) FROM pg_tables WHERE schemaname='public' AND tablename='direct_reviews'"))
    assert tables == 1
    states = asyncio.run(_scalar(test_database_url, "SELECT string_agg(pg_get_constraintdef(oid), ' ') FROM pg_constraint WHERE conrelid='direct_runs'::regclass AND contype='c'"))
    assert "REVIEWING" in states
    kinds = asyncio.run(_scalar(test_database_url, "SELECT string_agg(pg_get_constraintdef(oid), ' ') FROM pg_constraint WHERE conrelid='direct_sessions'::regclass AND contype='c'"))
    assert "review" in kinds
    command.downgrade(config, PREVIOUS)
    tables = asyncio.run(_scalar(test_database_url, "SELECT count(*) FROM pg_tables WHERE schemaname='public' AND tablename='direct_reviews'"))
    assert tables == 0
    command.upgrade(config, HEAD_REVISION)
