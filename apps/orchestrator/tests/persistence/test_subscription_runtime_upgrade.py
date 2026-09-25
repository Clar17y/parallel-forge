"""Diagnostic-only migration can roll back without dropping runtime evidence tables."""

import asyncio
from uuid import uuid4

import pytest
from alembic import command
from forge.persistence.database import create_engine, create_session_factory
from forge.persistence.repositories.subscription_runtime_status import (
    SubscriptionRuntimeStatusStore,
)
from sqlalchemy import inspect


@pytest.mark.integration
def test_runtime_status_upgrade_and_downgrade_preserve_other_tables(
    test_database_url, alembic_config_factory
):
    config = alembic_config_factory(test_database_url)

    async def tables():
        engine = create_engine(test_database_url)
        try:
            async with engine.connect() as connection:
                return await connection.run_sync(lambda sync: set(inspect(sync).get_table_names()))
        finally:
            await engine.dispose()

    async def retain_report():
        engine = create_engine(test_database_url)
        try:
            store = SubscriptionRuntimeStatusStore(create_session_factory(engine))
            assert await store.report(uuid4(), ())
            assert len((await store.status())["workers"]) == 1
        finally:
            await engine.dispose()

    command.upgrade(config, "20260912_0020")
    before = asyncio.run(tables())
    command.upgrade(config, "20260912_0021")
    assert asyncio.run(tables()) == before | {"subscription_worker_status"}
    asyncio.run(retain_report())
    command.downgrade(config, "20260912_0020")
    assert asyncio.run(tables()) == before
    command.upgrade(config, "20260912_0021")
    asyncio.run(retain_report())
    command.downgrade(config, "base")
