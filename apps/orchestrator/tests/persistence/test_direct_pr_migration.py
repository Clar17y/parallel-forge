"""Slice D migration 0047: pr fields + PUBLISHING/PR_OPEN."""

import asyncio

import pytest
from alembic import command
from sqlalchemy import text

PREVIOUS = "20261022_0046"
HEAD_REVISION = "20261022_0047"


async def _scalar(database_url: str, sql: str):
    from forge.persistence.database import create_engine
    engine = create_engine(database_url)
    try:
        async with engine.connect() as c:
            return await c.scalar(text(sql))
    finally:
        await engine.dispose()


@pytest.mark.integration
def test_direct_pr_migration_up_down(test_database_url, alembic_config_factory):
    config = alembic_config_factory(test_database_url)
    command.upgrade(config, HEAD_REVISION)
    cols = asyncio.run(_scalar(test_database_url, "SELECT count(*) FROM information_schema.columns WHERE table_name='direct_runs' AND column_name IN ('pr_number','pr_url','pr_rounds')"))
    assert cols == 3
    states = asyncio.run(_scalar(test_database_url, "SELECT string_agg(pg_get_constraintdef(oid), ' ') FROM pg_constraint WHERE conrelid='direct_runs'::regclass AND contype='c'"))
    assert "PUBLISHING" in states and "PR_OPEN" in states
    command.downgrade(config, PREVIOUS)
    cols = asyncio.run(_scalar(test_database_url, "SELECT count(*) FROM information_schema.columns WHERE table_name='direct_runs' AND column_name IN ('pr_number','pr_url','pr_rounds')"))
    assert cols == 0
    command.upgrade(config, HEAD_REVISION)
