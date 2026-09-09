"""The worker consumes an existing approval exactly once."""

from uuid import uuid4

import pytest
from forge.application.handlers.release import ApprovePrHandler
from forge.application.services.approved_plan import ApprovedPlanLoader
from forge.application.services.pr_evidence import PrEvidenceValidationError, PrEvidenceValidator
from forge.domain.run import RunState
from forge.persistence.models import Approval
from forge.persistence.repositories.commands import PostgresCommandRepository
from forge.persistence.unit_of_work import PostgresUnitOfWork
from test_pr_evidence import _frozen
from test_worker_planning_e2e import (
    workflow_session_factory as workflow_session_factory,  # noqa: PLC0414
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


async def authorized(tmp_path, factory):
    case, git, github = await _frozen(tmp_path, factory)
    approved_plans = ApprovedPlanLoader(case.artifact_store)
    actor, approval_id = uuid4(), uuid4()
    async with PostgresUnitOfWork(factory) as work:
        run = await work.runs.get_for_update(case.run_id)
        work.session.add(
            Approval(
                id=approval_id,
                run_id=run.id,
                gate="pr",
                evidence_digest=run.pending_evidence_digest,
                run_version=run.version,
                policy_version=run.policy_version,
                authenticated_actor_id=actor,
            )
        )
        await work.commands.enqueue(
            run_id=run.id,
            command_type="approve_pr",
            idempotency_key=f"{run.id}:approve-pr",
            payload={"approval_id": str(approval_id)},
            expected_run_version=run.version,
            actor_id=actor,
        )
        await work.commit()
    commands = PostgresCommandRepository(factory)
    command = await commands.claim_next(worker_id="approve-pr", lease_seconds=120)
    assert command.command_type == "approve_pr"
    handler = ApprovePrHandler(
        PrEvidenceValidator(
            case.artifact_store,
            approved_plans,
            lambda _: git,
            github,
        ),
        approved_plans,
    )
    return case, git, github, handler, command, approval_id


async def test_pr_approval_replays_after_lease_renewal(tmp_path, workflow_session_factory):
    case, _git, _github, handler, command, _approval = await authorized(
        tmp_path, workflow_session_factory
    )
    await PostgresCommandRepository(workflow_session_factory).renew(
        command.id, worker_id="approve-pr", lease_seconds=240
    )
    for _ in range(2):
        async with PostgresUnitOfWork(workflow_session_factory) as work:
            await handler(command, work)
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        run = await work.runs.get(case.run_id)
        assert run.state is RunState.PUBLISHING_PR
        assert run.version == command.expected_run_version + 1
        queued = await work.commands.get_by_idempotency_key(f"{run.id}:publish-pr:{run.version}")
        assert queued.payload == command.payload
        assert (
            len(
                [
                    e
                    for e in await work.events.list_after(run.id, 0)
                    if e.event_type == "run.pr_approved"
                ]
            )
            == 1
        )


@pytest.mark.parametrize("drift", ["content", "base"])
async def test_stale_pr_approval_never_publishes(tmp_path, workflow_session_factory, drift):
    case, git, github, handler, command, approval_id = await authorized(
        tmp_path, workflow_session_factory
    )
    if drift == "content":
        git.head = "c" * 40
    else:
        for key in github.bases:
            github.bases[key] = "c" * 40
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        await handler(command, work)
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        await handler(command, work)
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        run = await work.runs.get(case.run_id)
        assert run.state is (
            RunState.VALIDATING if drift == "content" else RunState.AWAITING_HUMAN_INTERVENTION
        )
        assert (await work.auth.get_approval(approval_id=approval_id)).invalidated_at is not None
        assert (
            await work.commands.get_by_idempotency_key(
                f"{run.id}:publish-pr:{command.expected_run_version + 1}"
            )
            is None
        )
        if drift == "base":
            # Resolve the test-only intervention before proving schema downgrade.
            await work.runs.transition(run.id, run.version, RunState.CANCELLED, "test.cleanup", {})
            await work.commit()


async def test_publication_rechecks_the_consumed_approval_and_current_candidate(
    tmp_path, workflow_session_factory
):
    case, git, github, handler, command, approval_id = await authorized(
        tmp_path, workflow_session_factory
    )
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        await handler(command, work)
    validator = PrEvidenceValidator(
        case.artifact_store, ApprovedPlanLoader(case.artifact_store), lambda _: git, github
    )
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        valid = await validator.validate_for_publication(work, case.run_id, approval_id)
        assert valid.approved.run.state is RunState.PUBLISHING_PR
        assert valid.evidence.candidate_commit == git.head
        with pytest.raises(PrEvidenceValidationError):
            await validator.validate_for_publication(work, case.run_id, uuid4())
    git.head = "c" * 40
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        with pytest.raises(PrEvidenceValidationError, match="content_drift"):
            await validator.validate_for_publication(work, case.run_id, approval_id)
