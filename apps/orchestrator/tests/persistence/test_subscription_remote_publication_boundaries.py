"""A retained remote repair grants no authority after approval, evidence, or lease drift."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from forge.application.ports.commands import CommandLeaseLost, CommandRecoveryRequired
from forge.application.ports.worktrees import GitSnapshotFile
from forge.application.services.pr_evidence import PrEvidenceValidationError
from forge.application.services.recovery import OperationExecutor
from forge.application.services.release import ReleaseService
from forge.domain.run import RunState
from forge.persistence.models import Approval, RunCommand, RunEvent
from forge.persistence.repositories.operations import PostgresOperationRepository
from sqlalchemy import select
from test_scheduler_acceptance import (
    _remove_disposable_subscription_rows,  # noqa: F401 - pytest fixture
)
from test_subscription_remote_publication import remote_acceptance_case


async def pending_push_case(session_factory, tmp_path):
    case = await remote_acceptance_case(session_factory, tmp_path)
    async with case.factory() as work:
        await case.controller.validate(case.validation_command, work)
    await case.commands.complete(
        case.validation_command.id, worker_id=case.validation_command.lease_owner
    )
    command = await case.commands.claim_next(worker_id="pending-push", lease_seconds=120)
    assert command is not None and command.command_type == "push_reviewed_pr"
    return case, command


@pytest.mark.integration
@pytest.mark.parametrize(
    "changed", ["approval", "acceptance-event", "acceptance-kind", "repair-event"]
)
async def test_revised_push_rejects_changed_retained_authority(session_factory, tmp_path, changed):
    case, command = await pending_push_case(session_factory, tmp_path)
    async with case.factory() as work:
        if changed == "approval":
            approval = await work.session.get(Approval, UUID(command.payload["approval_id"]))
            approval.invalidated_at = datetime.now(UTC)
        else:
            event = await work.session.scalar(
                select(RunEvent).where(
                    RunEvent.run_id == command.run_id,
                    RunEvent.event_type
                    == (
                        "run.subscription_acceptance_decided"
                        if changed.startswith("acceptance")
                        else "run.subscription_remote_repair_requested"
                    ),
                    RunEvent.run_version == command.expected_run_version
                    if changed.startswith("acceptance")
                    else RunEvent.run_version == case.repair_command.expected_run_version,
                )
            )
            if changed == "acceptance-kind":
                event.event_type, event.actor_id = "run.review_decided", None
            else:
                event.payload = {**event.payload, "extra": True}
        await work.commit()
    async with case.factory() as work:
        with pytest.raises((CommandRecoveryRequired, PrEvidenceValidationError)):
            await case.validator.validate_reviewed_push(work, command)
        assert not await work.operations.list_unresolved()
        record = await work.releases.get_for_run(command.run_id)
        assert record.reviewed_push_intent_id is None


@pytest.mark.integration
async def test_changed_candidate_blocks_remote_push_and_preserves_human_reconciliation(
    session_factory, tmp_path, monkeypatch
):
    case, command = await pending_push_case(session_factory, tmp_path)
    snapshot = case.git.working_tree_snapshot(case.original.worktree)
    monkeypatch.setattr(
        case.git,
        "working_tree_snapshot",
        lambda *args, **kwargs: replace(
            snapshot,
            files=(
                GitSnapshotFile(
                    path="apps/unaccepted.py",
                    mode="100644",
                    content_digest="e" * 64,
                    byte_count=1,
                ),
            ),
            changed_paths=("apps/unaccepted.py",),
        ),
    )
    pushes = []

    class Push:
        async def push(self, *args):
            pushes.append(args)

    service = ReleaseService(
        case.validator,
        case.writes,
        lambda _: Push(),
        OperationExecutor(PostgresOperationRepository(session_factory)),
    )
    async with case.factory() as work:
        await service.push_reviewed(command, work)
    async with case.factory() as work:
        run = await work.runs.get(command.run_id)
        assert run.state is RunState.AWAITING_HUMAN_INTERVENTION
        assert (
            await work.session.get(Approval, UUID(command.payload["approval_id"]))
        ).invalidated_at is not None
        assert not await work.operations.list_unresolved()
        await work.runs.transition(run.id, run.version, RunState.CANCELLED, "test.cleanup", {})
        await work.commit()
    assert pushes == []


@pytest.mark.integration
async def test_expired_final_validation_lease_cannot_queue_a_remote_push(
    session_factory, tmp_path, monkeypatch
):
    case = await remote_acceptance_case(session_factory, tmp_path)
    async with case.factory() as work:
        enqueue = work.commands.enqueue

        async def expire(**kwargs):
            queued = await enqueue(**kwargs)
            if kwargs["command_type"] == "push_reviewed_pr":
                row = await work.session.get(RunCommand, case.validation_command.id)
                row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
                await work.session.flush()
            return queued

        monkeypatch.setattr(work.commands, "enqueue", expire)
        with pytest.raises(CommandLeaseLost):
            await case.controller.validate(case.validation_command, work)
    async with case.factory() as work:
        run = await work.runs.get(case.validation_command.run_id)
        assert run.state is RunState.VALIDATING
        assert (
            await work.commands.get_by_idempotency_key(f"{run.id}:push-reviewed:{run.version + 1}")
            is None
        )
        assert not case.writes.pull_requests[case.original.policy.github_repository, 1].merged
