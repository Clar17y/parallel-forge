"""Slice B migration 0045: direct_checks + direct_sessions.kind backfill."""

import asyncio
import uuid

import pytest
from alembic import command
from sqlalchemy import text

PREVIOUS = "20261022_0044"
HEAD_REVISION = "20261022_0045"


async def _scalar(database_url: str, sql: str, params=None):
    from forge.persistence.database import create_engine

    engine = create_engine(database_url)
    try:
        async with engine.connect() as connection:
            return await connection.scalar(text(sql), params or {})
    finally:
        await engine.dispose()


async def _seed_session_at_0044(database_url: str) -> None:
    from forge.persistence.database import create_engine

    engine = create_engine(database_url)
    pid, rid, sid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO projects (id, canonical_path, canonical_path_key,"
                    " github_repository, default_branch, name)"
                    " VALUES (:p, :cp, :cp, :gr, 'main', 'P')"
                ),
                {"p": pid, "cp": f"/tmp/{pid}", "gr": f"o/r-{pid}"},
            )
            await connection.execute(
                text(
                    "INSERT INTO direct_runs (id, project_id, task_text, client, model)"
                    " VALUES (:r, :p, 't', 'muse_code', 'm')"
                ),
                {"r": rid, "p": pid},
            )
            await connection.execute(
                text(
                    "INSERT INTO direct_sessions (id, run_id, client, model)"
                    " VALUES (:s, :r, 'muse_code', 'm')"
                ),
                {"s": sid, "r": rid},
            )
    finally:
        await engine.dispose()


@pytest.mark.integration
def test_direct_checks_migration_up_down_and_kind_backfill(
    test_database_url, alembic_config_factory
):
    config = alembic_config_factory(test_database_url)
    command.upgrade(config, PREVIOUS)
    asyncio.run(_seed_session_at_0044(test_database_url))
    command.upgrade(config, HEAD_REVISION)
    kind = asyncio.run(
        _scalar(test_database_url, "SELECT kind FROM direct_sessions LIMIT 1")
    )
    assert kind == "implement"
    tables = asyncio.run(
        _scalar(
            test_database_url,
            "SELECT count(*) FROM pg_tables WHERE schemaname='public'"
            " AND tablename='direct_checks'",
        )
    )
    assert tables == 1
    # CHECKING must be an allowed run state at head (constraint names come
    # from the SQLAlchemy naming convention, so match by table).
    states = asyncio.run(
        _scalar(
            test_database_url,
            "SELECT string_agg(pg_get_constraintdef(oid), ' ') FROM pg_constraint"
            " WHERE conrelid='direct_runs'::regclass AND contype='c'",
        )
    )
    assert "CHECKING" in states and "REPAIRING" in states
    command.downgrade(config, PREVIOUS)
    tables = asyncio.run(
        _scalar(
            test_database_url,
            "SELECT count(*) FROM pg_tables WHERE schemaname='public'"
            " AND tablename='direct_checks'",
        )
    )
    assert tables == 0
    command.upgrade(config, HEAD_REVISION)
