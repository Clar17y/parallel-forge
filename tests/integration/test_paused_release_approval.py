"""Paused release gates retire obsolete approval deliveries before restoration."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from forge.application.handlers.run_controls import PauseRunHandler, ResumeRunHandler
from forge.application.ports.commands import CommandLane, CommandRecoveryRequired
from forge.application.services.release_monitor import ReleaseMonitor
from forge.domain.command import CommandStatus
from forge.domain.github import CheckSnapshot, MergeProtection
from forge.domain.run import RunState
from forge.persistence.models import Approval, RunCommand
from forge.persistence.repositories.commands import PostgresCommandRepository
from forge.persistence.unit_of_work import PostgresUnitOfWork
from test_pr_evidence import _frozen
from test_pr_monitoring import published
from test_worker_planning_e2e import (
    workflow_session_factory as workflow_session_factory,  # noqa: PLC0414
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


@pytest.mark.parametrize("gate", ["pr", "merge"])
@pytest.mark.parametrize("delivery", ["pending", "expired", "renewed", "wrong_actor"])
async def test_paused_pr_gate_retires_old_approval_before_resume(
    tmp_path, workflow_session_factory, delivery, gate
):
    factory = workflow_session_factory
    if gate == "pr":
        case, git, read = await _frozen(tmp_path, factory)
    else:
        case, git, read, writes, validator, poll, policy, _ = await published(tmp_path, factory)
        read.checks[policy.github_repository.casefold(), git.head] = (
            CheckSnapshot("ci", "completed", "success", head_sha=git.head),
        )
        read.merge_protections[policy.github_repository.casefold(), "main"] = MergeProtection(
            True, False, False, "classic", required_check_names=("ci",)
        )
        async with PostgresUnitOfWork(factory) as work:
            await ReleaseMonitor(case.artifact_store, validator, read, writes)(poll, work)
        await PostgresCommandRepository(factory).complete(poll.id, worker_id=poll.lease_owner)
    actor, approval_id = uuid4(), uuid4()
    commands = PostgresCommandRepository(factory)
    async with PostgresUnitOfWork(factory) as work:
        run = await work.runs.get(case.run_id)
        digest = run.pending_evidence_digest
        work.session.add(
            Approval(
                id=approval_id,
                run_id=run.id,
                gate=gate,
                evidence_digest=digest,
                run_version=run.version,
                policy_version=run.policy_version,
                authenticated_actor_id=actor,
            )
        )
        source = await work.commands.enqueue(
            run_id=run.id,
            command_type=f"approve_{gate}",
            idempotency_key=f"{run.id}:approval-before-pause",
            payload={"approval_id": str(approval_id)},
            expected_run_version=run.version,
            actor_id=actor,
        )
        await work.commit()
    if delivery in {"expired", "renewed"}:
        source = await commands.claim_next(worker_id="old-approval", lease_seconds=120)
    async with PostgresUnitOfWork(factory) as work:
        row = await work.session.get(RunCommand, source.id)
        row.available_at = datetime.now(UTC) + timedelta(hours=1)
        if delivery in {"expired", "renewed"}:
            row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await work.commands.enqueue(
            run_id=run.id,
            command_type="pause",
            idempotency_key=f"{run.id}:pause",
            payload={},
            expected_run_version=run.version,
            actor_id=actor,
        )
        await work.commit()
    pause = await commands.claim_next(
        worker_id="control", lease_seconds=120, lane=CommandLane.CONTROL
    )
    async with PostgresUnitOfWork(factory) as work:
        await PauseRunHandler()(pause, work)
    await commands.complete(pause.id, worker_id=pause.lease_owner)
    async with PostgresUnitOfWork(factory) as work:
        paused = await work.runs.get(run.id)
        await work.commands.enqueue(
            run_id=run.id,
            command_type="resume",
            idempotency_key=f"{run.id}:resume",
            payload={},
            expected_run_version=paused.version,
            actor_id=actor,
        )
        await work.commit()
    resume = await commands.claim_next(worker_id="resume", lease_seconds=120)
    assert resume.command_type == "resume"
    if delivery in {"renewed", "wrong_actor"}:
        async with PostgresUnitOfWork(factory) as work:
            row = await work.session.get(RunCommand, source.id)
            if delivery == "renewed":
                row.lease_expires_at = datetime.now(UTC) + timedelta(minutes=2)
            else:
                row.actor_id = uuid4()
            await work.commit()
        async with PostgresUnitOfWork(factory) as work:
            with pytest.raises(CommandRecoveryRequired, match="paused approval"):
                await ResumeRunHandler()(resume, work)
        async with PostgresUnitOfWork(factory) as work:
            assert (await work.runs.get(run.id)).state is RunState.PAUSED
            assert (await work.commands.get(source.id)).status is not CommandStatus.CANCELLED
            assert (await work.auth.get_approval(approval_id=approval_id)).invalidated_at is None
        return
    for _ in range(2):
        async with PostgresUnitOfWork(factory) as work:
            await ResumeRunHandler()(resume, work)
    async with PostgresUnitOfWork(factory) as work:
        restored = await work.runs.get(run.id)
        assert restored.state is (
            RunState.AWAITING_PR_APPROVAL if gate == "pr" else RunState.AWAITING_MERGE_APPROVAL
        )
        assert restored.pending_evidence_digest == digest
        assert restored.version == paused.version + 1
        assert (await work.commands.get(source.id)).status is CommandStatus.CANCELLED
        assert (await work.auth.get_approval(approval_id=approval_id)).invalidated_at is not None
        receipts = [
            e
            for e in await work.events.list_after(run.id, 0)
            if e.event_type == "approval.superseded_by_pause"
        ]
        assert len(receipts) == 1
        assert receipts[0].payload["command_id"] == str(source.id)
    await commands.complete(resume.id, worker_id=resume.lease_owner)
    fresh_id = uuid4()
    async with PostgresUnitOfWork(factory) as work:
        work.session.add(
            Approval(
                id=fresh_id,
                run_id=run.id,
                gate=gate,
                evidence_digest=digest,
                run_version=restored.version,
                policy_version=restored.policy_version,
                authenticated_actor_id=actor,
            )
        )
        await work.commands.enqueue(
            run_id=run.id,
            command_type=f"approve_{gate}",
            idempotency_key=f"{run.id}:fresh-approval",
            payload={"approval_id": str(fresh_id)},
            expected_run_version=restored.version,
            actor_id=actor,
        )
        await work.commit()
    fresh = await commands.claim_next(worker_id="fresh-approval", lease_seconds=120)
    from forge.application.handlers.merge import ApproveMergeHandler
    from forge.application.handlers.release import ApprovePrHandler
    from forge.application.services.approved_plan import ApprovedPlanLoader
    from forge.application.services.merge_evidence import MergeEvidenceValidator
    from forge.application.services.pr_evidence import PrEvidenceValidator
    from forge.release.merge import MergeController

    plans = ApprovedPlanLoader(case.artifact_store)
    if gate == "pr":
        handler = ApprovePrHandler(
            PrEvidenceValidator(case.artifact_store, plans, lambda _: git, read), plans
        )
    else:
        handler = ApproveMergeHandler(
            MergeEvidenceValidator(case.artifact_store, validator, MergeController(read, writes))
        )
    async with PostgresUnitOfWork(factory) as work:
        await handler(fresh, work)
        assert (await work.runs.get(run.id)).state is (
            RunState.PUBLISHING_PR if gate == "pr" else RunState.MERGING
        )
