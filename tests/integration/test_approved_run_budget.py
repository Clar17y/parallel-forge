from datetime import timedelta

import pytest
from forge.domain.approval import PlanApprovalEvidence
from forge.persistence.models import Run
from forge.persistence.repositories.runs import ConcurrencyConflict
from forge.persistence.unit_of_work import PostgresUnitOfWork
from test_planning_failed_usage import _build_case
from test_worker_planning_e2e import (
    workflow_session_factory as workflow_session_factory,  # noqa: PLC0414
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


@pytest.mark.parametrize("fail_event", [False, True])
async def test_approval_atomically_persists_exact_frozen_run_budgets(
    tmp_path, workflow_session_factory, fail_event
):
    factory = workflow_session_factory
    case = await _build_case(tmp_path, factory, fail_invalid=False)
    async with PostgresUnitOfWork(factory) as work:
        await case.service.execute(case.command, work)
        before = await work.runs.get(case.run_id)
    evidence = PlanApprovalEvidence.model_validate_json(
        await case.artifact_store.open_bytes(before.pending_evidence_digest)
    )
    async with PostgresUnitOfWork(factory) as work:
        if fail_event:

            class FailingEvents:
                async def append(self, event):
                    raise RuntimeError("injected event failure")

            work.runs._events = FailingEvents()
            with pytest.raises(RuntimeError, match="injected event failure"):
                await work.runs.approve_plan(
                    case.run_id, before.version, evidence, "run.plan_approved", {}
                )
        else:
            await work.runs.approve_plan(
                case.run_id, before.version, evidence, "run.plan_approved", {}
            )
        await work.commit()
    async with PostgresUnitOfWork(factory) as work:
        row = await work.session.get(Run, case.run_id)
        assert row.duration_budget_seconds == (
            0 if fail_event else evidence.duration_budget_seconds
        )
        assert row.token_budget == (0 if fail_event else evidence.token_budget)
        assert row.cost_budget_minor == (0 if fail_event else evidence.cost_budget_minor)
        assert row.state == ("AWAITING_PLAN_APPROVAL" if fail_event else "PREPARING_WORKTREE")
        assert await work.runs.duration_deadline(case.run_id) == row.created_at + timedelta(
            seconds=row.duration_budget_seconds
        )
    if not fail_event:
        async with PostgresUnitOfWork(factory) as work:
            with pytest.raises(ConcurrencyConflict):
                await work.runs.approve_plan(
                    case.run_id, before.version, evidence, "run.plan_approved", {}
                )
