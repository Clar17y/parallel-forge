"""Primary feedback migration upgrade from 0042 and downgrade (issue #137)."""

import asyncio

import pytest
from alembic import command
from sqlalchemy import text

PREVIOUS = "20261020_0042"
HEAD_REVISION = "20261021_0043"


async def _tables(database_url: str) -> set[str]:
    from forge.persistence.database import create_engine
    engine = create_engine(database_url)
    try:
        async with engine.connect() as connection:
            return set((await connection.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname='public'")
            )).scalars())
    finally:
        await engine.dispose()


async def _revision(database_url: str) -> str | None:
    from forge.persistence.database import create_engine
    engine = create_engine(database_url)
    try:
        async with engine.connect() as connection:
            return await connection.scalar(text("SELECT version_num FROM alembic_version"))
    finally:
        await engine.dispose()


@pytest.mark.integration
def test_primary_feedback_migration_upgrade_and_downgrade(test_database_url, alembic_config_factory):
    config = alembic_config_factory(test_database_url)
    command.upgrade(config, PREVIOUS)
    assert asyncio.run(_revision(test_database_url)) == PREVIOUS
    command.upgrade(config, HEAD_REVISION)
    assert "subscription_primary_feedback" in asyncio.run(_tables(test_database_url))
    assert asyncio.run(_revision(test_database_url)) == HEAD_REVISION
    command.downgrade(config, PREVIOUS)
    assert "subscription_primary_feedback" not in asyncio.run(_tables(test_database_url))
    command.upgrade(config, HEAD_REVISION)
    assert "subscription_primary_feedback" in asyncio.run(_tables(test_database_url))
