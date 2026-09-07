"""Delivery must recover the plan actually approved after its gate is cleared."""

import hashlib
import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from forge.domain.run import RunState
from forge.persistence.models import Approval, Project, ProjectPolicyVersion, Run
from forge.persistence.unit_of_work import PostgresUnitOfWork
from test_planning_failed_usage import _build_case
from test_worker_planning_e2e import (
    workflow_session_factory as workflow_session_factory,  # noqa: PLC0414
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


async def approved_case(tmp_path, factory, *, database_enabled=False):
    case = await _build_case(tmp_path, factory, fail_invalid=False)
    if database_enabled:
        # Fixture configuration precedes planning and the human approval.
        async with factory() as session, session.begin():
            run = await session.get(Run, case.run_id)
            row = await session.get(ProjectPolicyVersion, (run.project_id, 1))
            document = {
                **row.document,
                "version": 2,
                "database": {"enabled": True, "admin_url_secret_reference": "secret://test-admin"},
            }
            digest = hashlib.sha256(
                json.dumps(
                    document,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            session.add(
                ProjectPolicyVersion(
                    project_id=run.project_id,
                    version=2,
                    policy_digest=digest,
                    document_schema_version=1,
                    document=document,
                )
            )
            await session.flush()
            project = await session.get(Project, run.project_id)
            project.current_policy_version = 2
            run.policy_version = 2
    async with PostgresUnitOfWork(factory) as work:
        await case.service.execute(case.command, work)
        run = await work.runs.get(case.run_id)
        approval = Approval(
            id=uuid4(),
            run_id=run.id,
            gate="plan",
            run_version=run.version,
            policy_version=run.policy_version,
            evidence_digest=run.pending_evidence_digest,
            authenticated_actor_id=uuid4(),
        )
        work.session.add(approval)
        await work.session.flush()
        await work.runs.transition(
            run.id,
            run.version,
            RunState.PREPARING_WORKTREE,
            "run.plan_approved",
            {"approval_id": str(approval.id)},
            actor_class="operator",
            actor_id=approval.authenticated_actor_id,
        )
        await work.commit()
    return case, approval.id


async def test_delivery_loads_exact_approved_plan_after_gate_clears(
    tmp_path, workflow_session_factory
):
    from forge.application.services.approved_plan import ApprovedPlanLoader

    factory = workflow_session_factory
    case, approval_id = await approved_case(tmp_path, factory)
    async with PostgresUnitOfWork(factory) as work:
        approved = await ApprovedPlanLoader(case.artifact_store).load(work, case.run_id)
        assert approved.approval_id == approval_id
        assert approved.run.pending_evidence_digest is None
        assert approved.run.state is RunState.PREPARING_WORKTREE
        assert approved.evidence.base_sha == approved.run.base_sha
        assert approved.plan.required_checks
        assert approved.policy.version == approved.evidence.policy_version


@pytest.mark.parametrize("tamper", ["actor", "invalidated", "policy", "base"])
async def test_delivery_rejects_unbound_approval_or_changed_authority(
    tmp_path, workflow_session_factory, tamper
):
    from forge.application.services.approved_plan import ApprovedPlanError, ApprovedPlanLoader

    factory = workflow_session_factory
    case, approval_id = await approved_case(tmp_path, factory)
    async with factory() as session, session.begin():
        approval = await session.get(Approval, approval_id)
        if tamper == "actor":
            approval.authenticated_actor_id = uuid4()
        elif tamper == "invalidated":
            approval.invalidated_at = datetime.now(UTC)
            approval.invalidation_reason = "test invalidation"
        elif tamper == "policy":
            approval.policy_version += 1
        else:
            run = await session.get(Run, case.run_id)
            run.base_sha = "b" * 40
    async with PostgresUnitOfWork(factory) as work:
        with pytest.raises(ApprovedPlanError):
            await ApprovedPlanLoader(case.artifact_store).load(work, case.run_id)
