"""Additive graph storage and immutable history migration contracts."""

from __future__ import annotations

import asyncio
import json
from uuid import UUID, uuid4

import pytest
from alembic import command
from forge.persistence.database import create_engine
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

PREVIOUS_HEAD = "20261003_0031"


async def _execute(database_url: str, statement: str, parameters: dict) -> None:
    engine = create_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(text(statement), parameters)
    finally:
        await engine.dispose()


async def _seed_brief(database_url: str) -> tuple[UUID, UUID, str]:
    project_id, epic_id, brief_id = uuid4(), uuid4(), uuid4()
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
                    "VALUES (:id, :project_id, 'Retained epic', :content)"
                ),
                {"id": epic_id, "project_id": project_id, "content": "{}"},
            )
            await connection.execute(
                text(
                    "INSERT INTO epic_brief_revisions "
                    "(id, epic_id, revision_number, epic_version, content, content_digest) "
                    "VALUES (:id, :epic_id, 1, 1, :content, :digest)"
                ),
                {"id": brief_id, "epic_id": epic_id, "content": "{}", "digest": "b" * 64},
            )
            await connection.execute(
                text(
                    "UPDATE epics SET accepted_brief_revision_id = :brief_id, "
                    "accepted_brief_digest = :digest WHERE id = :epic_id"
                ),
                {"epic_id": epic_id, "brief_id": brief_id, "digest": "b" * 64},
            )
        return epic_id, brief_id, "b" * 64
    finally:
        await engine.dispose()


async def _brief_bytes(database_url: str, epic_id: UUID) -> str:
    engine = create_engine(database_url)
    try:
        async with engine.connect() as connection:
            result = await connection.scalar(
                text(
                    "SELECT json_build_object('epic', row_to_json(e), 'brief', "
                    "(SELECT row_to_json(b) FROM epic_brief_revisions b "
                    "WHERE b.id = e.accepted_brief_revision_id))::text "
                    "FROM epics e WHERE e.id = :id"
                ),
                {"id": epic_id},
            )
            assert isinstance(result, str)
            return result
    finally:
        await engine.dispose()


async def _inspect_graph_storage(database_url: str) -> None:
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
            assert "epic_graph_revisions" in tables
            assert await connection.scalar(text("SELECT count(*) FROM epic_graph_revisions")) == 0
    finally:
        await engine.dispose()


@pytest.mark.integration
def test_graph_upgrade_is_additive_repeatable_and_empty_downgrade_is_safe(
    test_database_url, alembic_config_factory
) -> None:
    config = alembic_config_factory(test_database_url)
    command.upgrade(config, PREVIOUS_HEAD)
    epic_id, _brief_id, _digest = asyncio.run(_seed_brief(test_database_url))
    retained = asyncio.run(_brief_bytes(test_database_url, epic_id))
    command.upgrade(config, "head")
    asyncio.run(_inspect_graph_storage(test_database_url))
    assert asyncio.run(_brief_bytes(test_database_url, epic_id)) == retained
    command.downgrade(config, PREVIOUS_HEAD)
    command.upgrade(config, "head")
    asyncio.run(_inspect_graph_storage(test_database_url))
    assert asyncio.run(_brief_bytes(test_database_url, epic_id)) == retained
    command.upgrade(config, "head")
    asyncio.run(_inspect_graph_storage(test_database_url))
    assert asyncio.run(_brief_bytes(test_database_url, epic_id)) == retained


@pytest.mark.integration
def test_graph_history_is_immutable_and_bindings_are_same_epic_exact_digest(
    test_database_url, alembic_config_factory
) -> None:
    command.upgrade(alembic_config_factory(test_database_url), "head")
    epic_id, brief_id, digest = asyncio.run(_seed_brief(test_database_url))
    other_epic_id, other_brief_id, _other_digest = asyncio.run(_seed_brief(test_database_url))
    graph_id = uuid4()
    insert = (
        "INSERT INTO epic_graph_revisions "
        "(id, epic_id, brief_revision_id, brief_digest, revision_number, epic_version, "
        "content, graph_digest) VALUES (:id, :epic, :brief, :brief_digest, 1, 2, "
        "CAST(:content AS jsonb), :digest)"
    )
    parameters = {
        "id": graph_id,
        "epic": epic_id,
        "brief": brief_id,
        "brief_digest": digest,
        "content": json.dumps({"schema_version": 1, "items": []}),
        "digest": "a" * 64,
    }
    asyncio.run(_execute(test_database_url, insert, parameters))
    for statement in (
        "UPDATE epic_graph_revisions SET graph_digest = :digest WHERE id = :id",
        "DELETE FROM epic_graph_revisions WHERE id = :id",
    ):
        with pytest.raises(DBAPIError, match="epic graph revisions are immutable"):
            asyncio.run(_execute(test_database_url, statement, {"id": graph_id, "digest": "c" * 64}))
    for invalid in (
        {"epic": other_epic_id},
        {"brief": other_brief_id},
        {"brief_digest": "c" * 64},
    ):
        with pytest.raises(DBAPIError):
            asyncio.run(_execute(test_database_url, insert, {**parameters, "id": uuid4(), **invalid}))
    selection = (
        "UPDATE epics SET accepted_graph_revision_id = :graph, "
        "accepted_graph_digest = :digest WHERE id = :epic"
    )
    for invalid in (
        {"epic": other_epic_id, "digest": "a" * 64},
        {"epic": epic_id, "digest": "c" * 64},
    ):
        with pytest.raises(DBAPIError):
            asyncio.run(_execute(test_database_url, selection, {"graph": graph_id, **invalid}))
    asyncio.run(
        _execute(
            test_database_url,
            selection,
            {"graph": graph_id, "epic": epic_id, "digest": "a" * 64},
        )
    )


@pytest.mark.integration
def test_graph_downgrade_refuses_populated_history(test_database_url, alembic_config_factory) -> None:
    config = alembic_config_factory(test_database_url)
    command.upgrade(config, "head")
    epic_id, brief_id, digest = asyncio.run(_seed_brief(test_database_url))
    asyncio.run(
        _execute(
            test_database_url,
            "INSERT INTO epic_graph_revisions "
            "(id, epic_id, brief_revision_id, brief_digest, revision_number, epic_version, "
            "content, graph_digest) VALUES (:id, :epic, :brief, :brief_digest, 1, 2, "
            "CAST(:content AS jsonb), :digest)",
            {
                "id": uuid4(),
                "epic": epic_id,
                "brief": brief_id,
                "brief_digest": digest,
                "content": '{"schema_version":1,"items":[]}',
                "digest": "a" * 64,
            },
        )
    )
    with pytest.raises(DBAPIError, match="cannot downgrade retained epic graph data"):
        command.downgrade(config, PREVIOUS_HEAD)
    command.upgrade(config, "head")
    assert asyncio.run(_brief_bytes(test_database_url, epic_id))
