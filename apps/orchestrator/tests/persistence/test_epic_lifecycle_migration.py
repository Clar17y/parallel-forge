"""Additive shared-ceiling migration retains prior epic rows and refuses lost edits."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from alembic import command
from forge.persistence.database import create_engine
from sqlalchemy import text

PREVIOUS_HEAD = "20261004_0034"
LIFECYCLE_HEAD = "20261005_0035"


async def _query(database_url: str, statement: str, values: dict | None = None):
    engine = create_engine(database_url)
    try:
        async with engine.begin() as connection:
            result = await connection.execute(text(statement), values or {})
            return result.scalar_one_or_none() if result.returns_rows else None
    finally:
        await engine.dispose()


@pytest.mark.integration
def test_lifecycle_upgrade_reupgrade_and_edited_ceiling_downgrade_refusal(
    test_database_url, alembic_config_factory
) -> None:
    config = alembic_config_factory(test_database_url)
    command.upgrade(config, PREVIOUS_HEAD)
    project_id, epic_id = uuid4(), uuid4()
    asyncio.run(
        _query(
            test_database_url,
            "INSERT INTO projects (id, name, canonical_path, canonical_path_key, "
            "github_repository, default_branch) VALUES "
            "(:project, 'Retained', :path, :path, :repo, 'main')",
            {"project": project_id, "path": f"/tmp/{project_id}", "repo": f"owner/{project_id}"},
        )
    )
    asyncio.run(
        _query(
            test_database_url,
            "INSERT INTO epics (id, project_id, title, draft) "
            "VALUES (:epic, :project, 'Retained epic', '{}')",
            {"epic": epic_id, "project": project_id},
        )
    )
    asyncio.run(
        _query(
            test_database_url,
            "INSERT INTO epic_brainstorm_budget_ledgers (epic_id, project_id, ceiling) "
            "VALUES (:epic, :project, '{}'::jsonb)",
            {"epic": epic_id, "project": project_id},
        )
    )
    command.upgrade(config, LIFECYCLE_HEAD)
    assert (
        asyncio.run(
            _query(
                test_database_url,
                "SELECT title FROM epics WHERE id = :epic",
                {"epic": epic_id},
            )
        )
        == "Retained epic"
    )
    assert (
        asyncio.run(
            _query(
                test_database_url,
                "SELECT version FROM epic_brainstorm_budget_ledgers WHERE epic_id = :epic",
                {"epic": epic_id},
            )
        )
        == 1
    )
    assert (
        asyncio.run(
            _query(
                test_database_url,
                "SELECT disabled_dimensions FROM epic_brainstorm_budget_ledgers WHERE epic_id = :epic",
                {"epic": epic_id},
            )
        )
        == []
    )
    command.upgrade(config, "head")  # Repeated upgrade keeps one head and prior rows.
    asyncio.run(
        _query(
            test_database_url,
            "UPDATE epic_brainstorm_budget_ledgers SET version = 2, "
            "disabled_dimensions = '[\"provider_attempts\"]'::jsonb WHERE epic_id = :epic",
            {"epic": epic_id},
        )
    )
    with pytest.raises(RuntimeError, match="cannot discard owner budget"):
        command.downgrade(config, PREVIOUS_HEAD)
    assert (
        asyncio.run(
            _query(
                test_database_url,
                "SELECT version FROM epic_brainstorm_budget_ledgers WHERE epic_id = :epic",
                {"epic": epic_id},
            )
        )
        == 2
    )
    asyncio.run(
        _query(
            test_database_url,
            "UPDATE epic_brainstorm_budget_ledgers SET version = 1, "
            "disabled_dimensions = '[]'::jsonb WHERE epic_id = :epic",
            {"epic": epic_id},
        )
    )
    command.downgrade(config, PREVIOUS_HEAD)
    command.upgrade(config, LIFECYCLE_HEAD)
    assert (
        asyncio.run(
            _query(
                test_database_url,
                "SELECT version FROM epic_brainstorm_budget_ledgers WHERE epic_id = :epic",
                {"epic": epic_id},
            )
        )
        == 1
    )
