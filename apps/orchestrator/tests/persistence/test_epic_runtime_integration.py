"""Upgrade the pre-runtime epic schema without rewriting retained run records."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic import command
from forge.domain.epic_brief import BriefContent
from forge.persistence.database import create_engine, create_session_factory
from forge.persistence.models.epic_brainstorm import BrainstormConversation
from forge.persistence.unit_of_work import PostgresUnitOfWork
from sqlalchemy import inspect, text


@pytest.fixture
def migrated_database_url(test_database_url, alembic_config_factory):
    command.upgrade(alembic_config_factory(test_database_url), "20261003_0032")
    return test_database_url


@pytest_asyncio.fixture
async def session_factory(migrated_database_url):
    engine = create_engine(migrated_database_url)
    try:
        yield create_session_factory(engine)
    finally:
        await engine.dispose()


@pytest.mark.integration
async def test_epic_runtime_upgrade_preserves_existing_project_task_policy_and_run(
    migrated_database_url, alembic_config_factory, session_factory, persisted_run
):
    async def retained_records():
        async with session_factory() as session:
            result = await session.execute(
                text(
                    "SELECT row_to_json(p)::text, row_to_json(v)::text, "
                    "row_to_json(t)::text, row_to_json(r)::text "
                    "FROM runs r JOIN tasks t ON t.id = r.task_id "
                    "JOIN projects p ON p.id = r.project_id "
                    "JOIN project_policy_versions v ON v.project_id = p.id "
                    "AND v.version = r.policy_version WHERE r.id = :id"
                ),
                {"id": persisted_run.id},
            )
            return tuple(result.one())

    before = await retained_records()
    config = alembic_config_factory(migrated_database_url)
    await asyncio.to_thread(command.upgrade, config, "head")
    assert await retained_records() == before
    engine = create_engine(migrated_database_url)
    try:
        async with engine.connect() as connection:
            tables = await connection.run_sync(lambda conn: set(inspect(conn).get_table_names()))
        assert {
            "epic_brainstorm_conversations",
            "epic_brainstorm_attempts",
            "epic_executions",
            "epic_item_attempts",
        } <= tables
    finally:
        await engine.dispose()
    await asyncio.to_thread(command.check, config)


@pytest.mark.integration
async def test_brainstorm_downgrade_retains_concurrently_committed_conversation(
    migrated_database_url, alembic_config_factory, session_factory, persisted_run
):
    config = alembic_config_factory(migrated_database_url)
    await asyncio.to_thread(command.upgrade, config, "head")
    epic_id, conversation_id = uuid4(), uuid4()
    async with PostgresUnitOfWork(session_factory) as work:
        await work.epics.create(
            epic_id=epic_id,
            project_id=persisted_run.project_id,
            title="Retained authoring",
            draft=BriefContent(problem="Preserve a committed conversation"),
        )
        await work.commit()
    async with session_factory() as writer:
        writer.add(
            BrainstormConversation(
                id=conversation_id,
                epic_id=epic_id,
                project_id=persisted_run.project_id,
            )
        )
        await writer.flush()
        downgrade = asyncio.create_task(
            asyncio.to_thread(command.downgrade, config, "20261003_0032")
        )
        try:
            async with asyncio.timeout(10):
                while True:
                    async with session_factory() as observer:
                        waiting = await observer.scalar(
                            text(
                                "SELECT EXISTS (SELECT 1 FROM pg_locks l "
                                "JOIN pg_class c ON c.oid = l.relation "
                                "WHERE NOT l.granted AND c.relname = 'epic_brainstorm_conversations')"
                            )
                        )
                    if waiting:
                        break
                    await asyncio.sleep(0.02)
            await writer.commit()
            with pytest.raises(
                RuntimeError, match="durable brainstorm evidence must not be discarded"
            ):
                await downgrade
        finally:
            await writer.rollback()
            if not downgrade.done():
                await asyncio.gather(downgrade, return_exceptions=True)
    async with session_factory() as session:
        assert await session.get(BrainstormConversation, conversation_id) is not None
        # PostgreSQL rolls the complete failed downgrade command back to head.
        assert (
            await session.scalar(text("SELECT version_num FROM alembic_version")) == "20261007_0037"
        )
