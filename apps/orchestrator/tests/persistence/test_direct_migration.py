"""Direct-runs migration: upgrade from 0043, downgrade guard (Slice A)."""

import asyncio

import pytest
from alembic import command
from sqlalchemy import text

PREVIOUS = "20261021_0043"
HEAD_REVISION = "20261022_0044"


async def _tables(database_url: str) -> set[str]:
    from forge.persistence.database import create_engine

    engine = create_engine(database_url)
    try:
        async with engine.connect() as connection:
            return set(
                (
                    await connection.execute(
                        text("SELECT tablename FROM pg_tables WHERE schemaname='public'")
                    )
                ).scalars()
            )
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
def test_direct_migration_upgrade_and_downgrade(test_database_url, alembic_config_factory):
    config = alembic_config_factory(test_database_url)
    command.upgrade(config, PREVIOUS)
    assert asyncio.run(_revision(test_database_url)) == PREVIOUS
    command.upgrade(config, HEAD_REVISION)
    tables = asyncio.run(_tables(test_database_url))
    assert {"direct_runs", "direct_sessions"} <= tables
    # Empty downgrade succeeds.
    command.downgrade(config, PREVIOUS)
    assert "direct_runs" not in asyncio.run(_tables(test_database_url))
    command.upgrade(config, HEAD_REVISION)


@pytest.mark.integration
def test_direct_migration_downgrade_refuses_with_rows(
    test_database_url, alembic_config_factory, session_factory
):
    # session_factory fixture migrates to head; insert a run row, then downgrade must refuse.
    import uuid

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
                    "INSERT INTO direct_runs (id, project_id, task_text, client, model)"
                    " VALUES (:r, :p, 't', 'muse_code', 'm')"
                ),
                {"r": rid, "p": pid},
            )
            await session.commit()

    asyncio.run(_seed())
    config = alembic_config_factory(test_database_url)
    with pytest.raises(RuntimeError, match="must not be discarded"):
        command.downgrade(config, PREVIOUS)
    assert "direct_runs" in asyncio.run(_tables(test_database_url))
