from __future__ import annotations

import pytest
from forge.domain.run import RunState
from forge.persistence.repositories.runs import ConcurrencyConflict
from forge.persistence.unit_of_work import PostgresUnitOfWork
from sqlalchemy import text

pytestmark = pytest.mark.integration


async def _monitoring(factory, run_id):
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE runs SET state='MONITORING_PR', version=0, pending_gate=NULL, pending_evidence_digest=NULL WHERE id=:id"
            ),
            {"id": run_id},
        )


async def test_remote_remediation_counts_once_and_rejects_stale(session_factory, persisted_run):
    await _monitoring(session_factory, persisted_run.id)
    async with PostgresUnitOfWork(session_factory) as work:
        result = await work.runs.begin_remote_remediation(
            persisted_run.id, 0, limit=1, event_type="test.remote", event_payload={}
        )
        await work.commit()
    assert result.state is RunState.REMEDIATING and result.remote_remediation_count == 1
    async with PostgresUnitOfWork(session_factory) as work:
        with pytest.raises(ConcurrencyConflict):
            await work.runs.begin_remote_remediation(
                persisted_run.id, 0, limit=1, event_type="test.remote", event_payload={}
            )


async def test_remote_limit_zero_intervenes_without_increment(session_factory, persisted_run):
    await _monitoring(session_factory, persisted_run.id)
    async with PostgresUnitOfWork(session_factory) as work:
        result = await work.runs.begin_remote_remediation(
            persisted_run.id, 0, limit=0, event_type="test.remote", event_payload={}
        )
        await work.commit()
    assert (
        result.state is RunState.AWAITING_HUMAN_INTERVENTION
        and result.remote_remediation_count == 0
    )
