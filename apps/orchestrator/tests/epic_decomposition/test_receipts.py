"""Adoption receipt, audit, and absence of execution side effects."""

import pytest
from forge.persistence.models.api import ApiMutation, OperatorAuditEvent
from sqlalchemy import select, text

from .test_persistence import prepared, settle, submit


@pytest.mark.asyncio
async def test_adoption_receipt_audit_and_no_run_or_approval(decomposition_session_factory) -> None:
    factory = decomposition_session_factory
    service, actor, epic_id, project_id = await prepared(factory)
    _, _, _, receipt = await submit(service, actor, epic_id, project_id)
    await settle(factory, receipt.job_id)
    outcome = await service.observe(epic_id=epic_id, project_id=project_id, job_id=receipt.job_id)
    async with factory() as session:
        before = {
            table: await session.scalar(text(f"SELECT count(*) FROM {table}"))
            for table in ("tasks", "runs", "approvals")
        }
    result = await service.adopt(
        epic_id=epic_id, project_id=project_id, job_id=receipt.job_id,
        proposal_digest=outcome.proposal_digest, expected_job_version=outcome.job_version,
        expected_epic_version=1, actor=actor, key="audit-adopt",
    )
    async with factory() as session:
        mutation = (await session.scalars(select(ApiMutation).where(
            ApiMutation.actor_id == actor.actor_id,
            ApiMutation.action == "epic.decomposition.adopt",
        ))).one()
        audit = (await session.scalars(select(OperatorAuditEvent).where(
            OperatorAuditEvent.actor_id == actor.actor_id,
            OperatorAuditEvent.event_type == "epic.decomposition.adopt",
        ))).one()
        assert mutation.lifecycle_state == "COMPLETED"
        assert mutation.response_payload["graph_revision_id"] == str(result.graph_revision_id)
        assert mutation.response_payload["graph_digest"] == result.graph_digest
        assert audit.subject_id == epic_id
        assert audit.payload["job_id"] == str(receipt.job_id)
        assert audit.payload["proposal_digest"] == outcome.proposal_digest
        after = {
            table: await session.scalar(text(f"SELECT count(*) FROM {table}"))
            for table in before
        }
        assert after == before
