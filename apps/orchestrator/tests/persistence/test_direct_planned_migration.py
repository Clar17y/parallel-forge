"""Direct planned-shape migration: upgrade from 0047, downgrade guard."""

import asyncio
import uuid

import pytest
from alembic import command
from sqlalchemy import text

PREVIOUS = "20261022_0047"
HEAD_REVISION = "20261026_0048"


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
def test_planned_migration_upgrade_and_downgrade(test_database_url, alembic_config_factory):
    config = alembic_config_factory(test_database_url)
    command.upgrade(config, PREVIOUS)
    assert "plan_text" not in asyncio.run(_columns(test_database_url, "direct_runs"))
    command.upgrade(config, HEAD_REVISION)
    columns = asyncio.run(_columns(test_database_url, "direct_runs"))
    assert {"shape", "review_plan_first", "plan_text", "plan_approved"} <= columns
    # Empty downgrade succeeds.
    command.downgrade(config, PREVIOUS)
    assert "plan_text" not in asyncio.run(_columns(test_database_url, "direct_runs"))
    command.upgrade(config, HEAD_REVISION)


@pytest.mark.integration
def test_planned_migration_downgrade_refuses_with_plan_data(
    test_database_url, alembic_config_factory, session_factory
):
    # session_factory fixture migrates to head; a planned row blocks downgrade.
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
                    " (id, project_id, task_text, client, model, shape, plan_text)"
                    " VALUES (:r, :p, 't', 'muse_code', 'm', 'planned', 'a plan')"
                ),
                {"r": rid, "p": pid},
            )
            await session.commit()

    asyncio.run(_seed())
    config = alembic_config_factory(test_database_url)
    with pytest.raises(RuntimeError, match="must not be discarded"):
        command.downgrade(config, PREVIOUS)
    assert "plan_text" in asyncio.run(_columns(test_database_url, "direct_runs"))
