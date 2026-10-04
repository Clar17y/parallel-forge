"""Migration test for durable epic brainstorm tables (20261004_0033)."""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest
from alembic import command
from forge.persistence.database import create_engine
from sqlalchemy import text

PREVIOUS_HEAD = "20261003_0032"
BRAINSTORM_HEAD = "20261004_0033"

EXPECTED_TABLES = (
    "epic_brainstorm_audit",
    "epic_brainstorm_budget_ledgers",
    "epic_brainstorm_receipts",
    "epic_brainstorm_conversations",
    "epic_brainstorm_jobs",
    "epic_brainstorm_turns",
    "epic_brainstorm_attempts",
    "epic_brainstorm_quota_admissions",
)


async def _execute(database_url: str, statement: str, parameters: dict | None = None) -> None:
    engine = create_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(text(statement), parameters or {})
    finally:
        await engine.dispose()


async def _seed_project_and_epic(database_url: str) -> tuple[UUID, UUID]:
    project_id, epic_id = uuid4(), uuid4()
    engine = create_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO projects (id, name, canonical_path, canonical_path_key, "
                    "github_repository, default_branch) "
                    "VALUES (:id, 'Existing', :path, :path, :repo, 'main')"
                ),
                {"id": project_id, "path": f"/tmp/{project_id}", "repo": f"owner/{project_id}"},
            )
            await connection.execute(
                text(
                    "INSERT INTO epics (id, project_id, title, draft) "
                    "VALUES (:id, :project_id, 'Retained epic', '{}')"
                ),
                {"id": epic_id, "project_id": project_id},
            )
        return project_id, epic_id
    finally:
        await engine.dispose()


async def _inspect_tables(database_url: str) -> set[str]:
    engine = create_engine(database_url)
    try:
        async with engine.connect() as connection:
            result = await connection.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            )
            return set(result.scalars())
    finally:
        await engine.dispose()


@pytest.mark.integration
def test_brainstorm_migration_upgrade_and_empty_downgrade(
    test_database_url, alembic_config_factory
) -> None:
    config = alembic_config_factory(test_database_url)
    command.upgrade(config, PREVIOUS_HEAD)

    _project_id, epic_id = asyncio.run(_seed_project_and_epic(test_database_url))

    tables_before = asyncio.run(_inspect_tables(test_database_url))
    for t in EXPECTED_TABLES:
        assert t not in tables_before

    # Upgrade to brainstorm migration
    command.upgrade(config, BRAINSTORM_HEAD)

    tables_after = asyncio.run(_inspect_tables(test_database_url))
    for t in EXPECTED_TABLES:
        assert t in tables_after

    # Verify existing project and epic are retained
    async def _verify_retained():
        engine = create_engine(test_database_url)
        try:
            async with engine.connect() as conn:
                epic_title = await conn.scalar(
                    text("SELECT title FROM epics WHERE id = :id"), {"id": epic_id}
                )
                assert epic_title == "Retained epic"
        finally:
            await engine.dispose()

    asyncio.run(_verify_retained())

    # Empty downgrade should succeed cleanly
    command.downgrade(config, PREVIOUS_HEAD)

    tables_downgraded = asyncio.run(_inspect_tables(test_database_url))
    for t in EXPECTED_TABLES:
        assert t not in tables_downgraded

    # Upgrade back to head
    command.upgrade(config, BRAINSTORM_HEAD)
    tables_reupgraded = asyncio.run(_inspect_tables(test_database_url))
    for t in EXPECTED_TABLES:
        assert t in tables_reupgraded


@pytest.mark.integration
def test_brainstorm_migration_refuses_downgrade_with_records(
    test_database_url, alembic_config_factory
) -> None:
    config = alembic_config_factory(test_database_url)
    command.upgrade(config, BRAINSTORM_HEAD)

    project_id, epic_id = asyncio.run(_seed_project_and_epic(test_database_url))

    # Insert a conversation record
    conv_id = uuid4()
    asyncio.run(
        _execute(
            test_database_url,
            "INSERT INTO epic_brainstorm_conversations (id, epic_id, project_id, version) "
            "VALUES (:id, :epic_id, :project_id, 1)",
            {"id": conv_id, "epic_id": epic_id, "project_id": project_id},
        )
    )

    # Downgrade must fail closed
    with pytest.raises(RuntimeError, match="durable brainstorm evidence must not be discarded"):
        command.downgrade(config, PREVIOUS_HEAD)

    # Clean up the record and verify downgrade then succeeds
    asyncio.run(
        _execute(
            test_database_url,
            "DELETE FROM epic_brainstorm_conversations WHERE id = :id",
            {"id": conv_id},
        )
    )
    command.downgrade(config, PREVIOUS_HEAD)
