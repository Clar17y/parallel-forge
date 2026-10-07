"""A verified integration handoff prevents destructive rollback."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from alembic import command
from forge.persistence.database import create_engine
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from apps.orchestrator.tests.epic_run_bridge.test_eligibility import BridgeWork
from apps.orchestrator.tests.epic_run_bridge.test_eligibility import (
    test_settled_merge_requires_integration_and_is_immutable as _produce_verified_handoff,
)

pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


@pytest.mark.integration
async def test_nonempty_verified_handoff_refuses_downgrade_without_losing_proof(
    session_factory, migrated_database_url, alembic_config_factory, tmp_path
) -> None:
    # Reuse the real-Git producer fixture; its proof is written only after a
    # merged child is observed on the integration branch.
    await _produce_verified_handoff(
        session_factory, lambda: BridgeWork(session_factory), tmp_path
    )

    async def snapshot() -> list[tuple]:
        engine = create_engine(migrated_database_url)
        try:
            async with engine.connect() as connection:
                result = await connection.execute(text(
                    "SELECT id, execution_id, item_id, attempt_id, run_id, run_version, "
                    "merge_intent_id, merge_sha, integration_ref, verified_base_sha, "
                    "evidence_digest FROM epic_completion_handoffs ORDER BY id"
                ))
                return [tuple(row) for row in result]
        finally:
            await engine.dispose()

    before = await snapshot()
    assert len(before) == 1
    with pytest.raises(DBAPIError, match="cannot discard epic completion handoffs"):
        await asyncio.to_thread(
            command.downgrade, alembic_config_factory(migrated_database_url), "20261005_0035"
        )
    assert await snapshot() == before


@pytest.mark.integration
async def test_nonempty_dispatch_authority_refuses_downgrade_and_retains_exact_row(
    session_factory, migrated_database_url, alembic_config_factory, tmp_path
) -> None:
    # The real-Git producer supplies a valid frozen execution FK. The row is
    # deliberately nonempty so migration 0037 must refuse a lossy rollback.
    await _produce_verified_handoff(
        session_factory, lambda: BridgeWork(session_factory), tmp_path
    )
    engine = create_engine(migrated_database_url)
    try:
        async with engine.begin() as connection:
            inserted = await connection.execute(text(
                "INSERT INTO epic_dispatch_settings "
                "(execution_id, epic_id, version, enabled, actor_id, session_id, blocker_code) "
                "SELECT e.id, e.epic_id, 3, FALSE, :actor, :session, 'owner_paused' "
                "FROM epic_executions e JOIN epic_completion_handoffs h "
                "ON h.execution_id = e.id RETURNING execution_id"
            ), {"actor": uuid4(), "session": uuid4()})
            assert len(inserted.all()) == 1

        async def snapshot() -> list[tuple]:
            async with engine.connect() as connection:
                result = await connection.execute(text(
                    "SELECT execution_id, epic_id, version, enabled, actor_id, session_id, "
                    "profile_id, profile_version, claim_item_id, claim_token, "
                    "claim_expires_at, blocker_code, checked_at "
                    "FROM epic_dispatch_settings ORDER BY execution_id"
                ))
                return [tuple(row) for row in result]

        before = await snapshot()
        assert len(before) == 1 and before[0][2:4] == (3, False)
        with pytest.raises(DBAPIError, match="cannot discard epic dispatch authority"):
            await asyncio.to_thread(
                command.downgrade,
                alembic_config_factory(migrated_database_url),
                "20261007_0036",
            )
        assert await snapshot() == before
    finally:
        await engine.dispose()
