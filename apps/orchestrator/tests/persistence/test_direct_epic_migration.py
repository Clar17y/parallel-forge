"""Direct epic-shape migration: upgrade from 0048, downgrade guard."""

import asyncio
import uuid

import pytest
from alembic import command
from sqlalchemy import text

PREVIOUS = "20261026_0048"
HEAD_REVISION = "20261110_0049"


async def _legacy_rows(database_url: str, *, seed: bool):
    from forge.persistence.database import create_engine

    engine = create_engine(database_url)
    try:
        async with engine.begin() as connection:
            if seed:
                pid = uuid.uuid4()
                await connection.execute(text("INSERT INTO projects (id, canonical_path, canonical_path_key, github_repository, default_branch, name) VALUES (:p, :path, :path, 'o/r', 'main', 'P')"), {"p": pid, "path": f"/tmp/{pid}"})
                for shape in ("quick", "planned"):
                    await connection.execute(text("INSERT INTO direct_runs (id, project_id, task_text, client, model, shape, state, plan_text, plan_approved) VALUES (:r, :p, :task, 'muse_code', 'm', :shape, 'DONE', 'preserved plan', true)"), {"r": uuid.uuid4(), "p": pid, "shape": shape, "task": f"original {shape}"})
            rows = await connection.execute(text("SELECT id, task_text, shape, state, plan_text, plan_approved FROM direct_runs ORDER BY shape"))
            return [tuple(row) for row in rows]
    finally:
        await engine.dispose()


async def _tables(database_url: str) -> set[str]:
    from forge.persistence.database import create_engine

    engine = create_engine(database_url)
    try:
        async with engine.connect() as connection:
            return set(
                (
                    await connection.execute(
                        text(
                            "SELECT tablename FROM pg_tables "
                            "WHERE schemaname = 'public'"
                        )
                    )
                ).scalars()
            )
    finally:
        await engine.dispose()


async def _columns(database_url: str, table: str) -> set[str]:
    from forge.persistence.database import create_engine

    engine = create_engine(database_url)
    try:
        async with engine.connect() as connection:
            return set(
                (
                    await connection.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns "
                            f"WHERE table_name = '{table}'"
                        )
                    )
                ).scalars()
            )
    finally:
        await engine.dispose()


@pytest.mark.integration
def test_epic_migration_upgrade_and_downgrade(test_database_url, alembic_config_factory):
    config = alembic_config_factory(test_database_url)
    command.upgrade(config, PREVIOUS)
    assert "direct_tasks" not in asyncio.run(_tables(test_database_url))
    legacy = asyncio.run(_legacy_rows(test_database_url, seed=True))
    command.upgrade(config, HEAD_REVISION)
    assert asyncio.run(_legacy_rows(test_database_url, seed=False)) == legacy
    assert "direct_tasks" in asyncio.run(_tables(test_database_url))
    assert "task_id" in asyncio.run(_columns(test_database_url, "direct_sessions"))
    assert "task_id" in asyncio.run(_columns(test_database_url, "direct_checks"))
    assert "task_id" in asyncio.run(_columns(test_database_url, "direct_reviews"))
    # Empty downgrade succeeds.
    command.downgrade(config, PREVIOUS)
    assert "direct_tasks" not in asyncio.run(_tables(test_database_url))
    assert asyncio.run(_legacy_rows(test_database_url, seed=False)) == legacy
    command.upgrade(config, HEAD_REVISION)


@pytest.mark.integration
def test_epic_migration_downgrade_refuses_with_epic_data(
    test_database_url, alembic_config_factory, session_factory
):
    async def _seed() -> None:
        pid = uuid.uuid4()
        rid = uuid.uuid4()
        async with session_factory() as session:
            await session.execute(
                text(
                    "INSERT INTO projects (id, canonical_path, canonical_path_key,"
                    " github_repository, default_branch, name)"
                    " VALUES (:p, :cp, :cp, :gr, 'main', 'P')"
                ),
                {"p": pid, "cp": f"/tmp/{pid}", "gr": f"o/r-{pid}"},
            )
            await session.execute(
                text(
                    "INSERT INTO direct_runs"
                    " (id, project_id, task_text, client, model, shape)"
                    " VALUES (:r, :p, 't', 'muse_code', 'm', 'epic')"
                ),
                {"r": rid, "p": pid},
            )
            await session.commit()

    asyncio.run(_seed())
    config = alembic_config_factory(test_database_url)
    with pytest.raises(RuntimeError, match="must not be discarded"):
        command.downgrade(config, PREVIOUS)
