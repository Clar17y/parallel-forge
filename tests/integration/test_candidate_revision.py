"""Operator feedback is bound to the exact durable frozen candidate."""

from dataclasses import replace
from uuid import uuid4

import pytest
from forge.application.services.approved_plan import ApprovedPlanLoader
from forge.application.services.candidate_revision import (
    CandidateRevisionError,
    CandidateRevisionService,
)
from forge.application.services.review_decision import ReviewDecisionService
from forge.domain.run import RunState
from forge.persistence.repositories.commands import PostgresCommandRepository
from forge.persistence.unit_of_work import PostgresUnitOfWork
from test_delivery_review import _review_case
from test_worker_planning_e2e import (
    workflow_session_factory as workflow_session_factory,  # noqa: PLC0414
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


async def _frozen_candidate(tmp_path, factory):
    case, review_command, reviewer, _gateway, git, _validation = await _review_case(
        tmp_path, factory
    )
    async with PostgresUnitOfWork(factory) as work:
        await reviewer.execute(review_command, work)
    async with PostgresUnitOfWork(factory) as work:
        frozen = await ReviewDecisionService(
            case.artifact_store, git_factory=lambda _policy: git
        ).decide(review_command, work)
    commands = PostgresCommandRepository(factory)
    await commands.complete(review_command.id, worker_id=review_command.lease_owner)
    # Feedback can come from an operator other than the original plan approver.
    actor = uuid4()
    await commands.enqueue(
        run_id=case.run_id,
        command_type="request_candidate_changes",
        idempotency_key=f"{case.run_id}:feedback",
        payload={"feedback": "Clarify the example."},
        expected_run_version=frozen.version,
        actor_id=actor,
    )
    command = await commands.claim_next(worker_id="revision", lease_seconds=60)
    assert command is not None and command.command_type == "request_candidate_changes"
    service = CandidateRevisionService(
        case.artifact_store, ApprovedPlanLoader(case.artifact_store), lambda _policy: git
    )
    return case, command, commands, service, git


async def test_revision_renewal_and_replay_preserve_plan_and_counter(
    tmp_path, workflow_session_factory
):
    case, command, commands, service, _git = await _frozen_candidate(
        tmp_path, workflow_session_factory
    )
    await commands.renew(command.id, worker_id="revision", lease_seconds=120)
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        await service.execute(command, work)
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        await service.execute(command, work)
        approved = await ApprovedPlanLoader(case.artifact_store).load(work, case.run_id)
        assert approved.run.state is RunState.REMEDIATING
        assert approved.run.local_remediation_count == 0
        events = [
            e
            for e in await work.events.list_after(case.run_id, 0)
            if e.event_type == "run.candidate_revision_requested"
        ]
        assert len(events) == 1 and events[0].actor_id == command.actor_id
        queued = await work.commands.get_by_idempotency_key(events[0].payload["queued_key"])
        assert queued.payload["automatic"] is False
        assert queued.payload["validation_evidence_set_id"]
        assert queued.payload["prior_review_evidence_set_id"]


@pytest.mark.parametrize("drift", ["actor", "head", "freeze_event"])
async def test_revision_rejects_substituted_authority(tmp_path, workflow_session_factory, drift):
    case, command, _commands, service, git = await _frozen_candidate(
        tmp_path, workflow_session_factory
    )
    if drift == "actor":
        command = replace(command, actor_id=uuid4())
    elif drift == "head":
        git.head = "c" * 40
    else:
        from forge.persistence.models import RunEvent
        from sqlalchemy import update

        async with workflow_session_factory() as session, session.begin():
            await session.execute(
                update(RunEvent)
                .where(RunEvent.run_id == case.run_id, RunEvent.event_type == "run.review_decided")
                .values(actor_class="operator")
            )
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        with pytest.raises(CandidateRevisionError):
            await service.execute(command, work)
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        run = await work.runs.get(case.run_id)
        assert run.state is RunState.AWAITING_PR_APPROVAL
        assert run.local_remediation_count == 0
