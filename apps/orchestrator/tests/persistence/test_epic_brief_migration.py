"""Additive epic foundation migration and retained-data rollback contracts."""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from forge.persistence.database import create_engine
from sqlalchemy import text

PREVIOUS_HEAD = "20260929_0030"


async def _seed_task(database_url: str) -> tuple[UUID, str]:
    project_id, task_id = uuid4(), uuid4()
    engine = create_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO projects "
                    "(id, name, canonical_path, canonical_path_key, github_repository, "
                    "default_branch) VALUES (:id, 'Existing', :path, :path, :repo, 'main')"
                ),
                {"id": project_id, "path": f"/tmp/{project_id}", "repo": f"owner/{project_id}"},
            )
            await connection.execute(
                text(
                    "INSERT INTO tasks (id, project_id, title, body, normalized_text, "
                    "task_digest, untrusted_external_content) "
                    "VALUES (:id, :project_id, 'Existing title', 'Original body', "
                    "'Existing title\\n\\nOriginal body', :digest, false)"
                ),
                {"id": task_id, "project_id": project_id, "digest": "a" * 64},
            )
            original = await connection.scalar(
                text("SELECT row_to_json(t)::text FROM tasks t WHERE id = :id"), {"id": task_id}
            )
            assert isinstance(original, str)
            return task_id, original
    finally:
        await engine.dispose()


async def _inspect_upgrade(database_url: str, task_id: UUID, original: str) -> None:
    engine = create_engine(database_url)
    try:
        async with engine.connect() as connection:
            tables = set(
                (
                    await connection.execute(
                        text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
                    )
                ).scalars()
            )
            assert {"epics", "epic_brief_revisions"} <= tables
            retained = await connection.scalar(
                text("SELECT row_to_json(t)::text FROM tasks t WHERE id = :id"), {"id": task_id}
            )
            assert retained == original
            assert await connection.scalar(text("SELECT count(*) FROM epics")) == 0
            assert await connection.scalar(text("SELECT count(*) FROM epic_brief_revisions")) == 0
    finally:
        await engine.dispose()


@pytest.mark.integration
def test_epic_upgrade_is_additive_and_repeatable(
    test_database_url,
    alembic_config_factory,
) -> None:
    config = alembic_config_factory(test_database_url)
    command.upgrade(config, PREVIOUS_HEAD)
    task_id, original = asyncio.run(_seed_task(test_database_url))
    command.upgrade(config, "head")
    asyncio.run(_inspect_upgrade(test_database_url, task_id, original))
    command.upgrade(config, "head")
    asyncio.run(_inspect_upgrade(test_database_url, task_id, original))
    command.downgrade(config, PREVIOUS_HEAD)
    command.upgrade(config, "head")
    asyncio.run(_inspect_upgrade(test_database_url, task_id, original))


async def _seed_epic(database_url: str) -> tuple[UUID, UUID, str]:
    import json

    from forge.domain.operation import canonical_digest

    task_id, _original = await _seed_task(database_url)
    epic_id, revision_id = uuid4(), uuid4()
    content = {
        "schema_version": 1,
        "problem": "",
        "outcomes": [],
        "scope": [],
        "exclusions": [],
        "requirements": [],
        "decisions": [],
        "assumptions": [],
        "open_questions": [],
    }
    digest = canonical_digest(content)
    engine = create_engine(database_url)
    try:
        async with engine.begin() as connection:
            project_id = await connection.scalar(
                text("SELECT project_id FROM tasks WHERE id = :id"), {"id": task_id}
            )
            await connection.execute(
                text(
                    "INSERT INTO epics (id, project_id, title, draft) "
                    "VALUES (:id, :project_id, 'Saved epic', CAST(:content AS jsonb))"
                ),
                {"id": epic_id, "project_id": project_id, "content": json.dumps(content)},
            )
            await connection.execute(
                text(
                    "INSERT INTO epic_brief_revisions "
                    "(id, epic_id, revision_number, epic_version, content, content_digest) "
                    "VALUES (:id, :epic_id, 1, 1, CAST(:content AS jsonb), :digest)"
                ),
                {
                    "id": revision_id,
                    "epic_id": epic_id,
                    "content": json.dumps(content),
                    "digest": digest,
                },
            )
            await connection.execute(
                text(
                    "UPDATE epics SET accepted_brief_revision_id = :revision, "
                    "accepted_brief_digest = :digest WHERE id = :id"
                ),
                {"id": epic_id, "revision": revision_id, "digest": digest},
            )
        return epic_id, revision_id, digest
    finally:
        await engine.dispose()


async def _execute_write(database_url: str, statement: str, parameters: dict) -> None:
    engine = create_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(text(statement), parameters)
    finally:
        await engine.dispose()


@pytest.mark.integration
def test_brief_revisions_are_immutable_and_cross_epic_selection_fails(
    test_database_url,
    alembic_config_factory,
) -> None:
    from sqlalchemy.exc import DBAPIError

    command.upgrade(alembic_config_factory(test_database_url), "head")
    epic_id, revision_id, digest = asyncio.run(_seed_epic(test_database_url))
    other_epic, other_revision, other_digest = asyncio.run(_seed_epic(test_database_url))
    for statement in (
        "UPDATE epic_brief_revisions SET content = '{}'::jsonb WHERE id = :id",
        "DELETE FROM epic_brief_revisions WHERE id = :id",
    ):
        with pytest.raises(DBAPIError, match="epic brief revisions are immutable"):
            asyncio.run(_execute_write(test_database_url, statement, {"id": revision_id}))
    assert other_epic != epic_id and digest == other_digest
    with pytest.raises(DBAPIError, match="fk_epics_accepted_brief"):
        asyncio.run(
            _execute_write(
                test_database_url,
                "UPDATE epics SET accepted_brief_revision_id = :revision, "
                "accepted_brief_digest = :digest WHERE id = :id",
                {"id": epic_id, "revision": other_revision, "digest": other_digest},
            )
        )


@pytest.mark.integration
def test_downgrade_refuses_to_discard_saved_epic_records(
    test_database_url,
    alembic_config_factory,
) -> None:
    from sqlalchemy.exc import DBAPIError

    config = alembic_config_factory(test_database_url)
    command.upgrade(config, "head")
    epic_id, revision_id, digest = asyncio.run(_seed_epic(test_database_url))
    with pytest.raises(DBAPIError, match="cannot downgrade retained epic data"):
        command.downgrade(config, PREVIOUS_HEAD)
    command.upgrade(config, "head")

    async def inspect_retained() -> None:
        engine = create_engine(test_database_url)
        try:
            async with engine.connect() as connection:
                assert (
                    await connection.scalar(
                        text("SELECT accepted_brief_revision_id FROM epics WHERE id = :id"),
                        {"id": epic_id},
                    )
                    == revision_id
                )
                assert (
                    await connection.scalar(
                        text("SELECT content_digest FROM epic_brief_revisions WHERE id = :id"),
                        {"id": revision_id},
                    )
                    == digest
                )
                assert (
                    await connection.scalar(text("SELECT version_num FROM alembic_version"))
                    == ScriptDirectory.from_config(config).get_current_head()
                )
        finally:
            await engine.dispose()

    asyncio.run(inspect_retained())
