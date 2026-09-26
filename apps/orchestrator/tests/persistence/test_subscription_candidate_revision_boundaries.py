"""Candidate reopening remains bounded, atomic and bound to retained evidence."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from forge.application.handlers.release import ApprovePrHandler
from forge.application.ports.commands import CommandLeaseLost, CommandRecoveryRequired
from forge.application.ports.subscription_decisions import SubscriptionDecisionError
from forge.application.ports.worktrees import GitSnapshotFile
from forge.application.services.approved_plan import ApprovedPlanLoader
from forge.application.services.candidate_revision import CandidateRevisionService
from forge.application.services.subscription_candidate_revision import (
    EVENT,
    SubscriptionCandidateRevisionController,
)
from forge.domain.run import RunState
from forge.persistence.models import Approval, Run, RunCommand, RunEvent
from forge.persistence.models.scheduling import SubscriptionScheduledTask, SubscriptionSchedulerRun
from forge.persistence.models.subscription import SubscriptionTask
from forge.persistence.models.subscription_results import SubscriptionRepairDebit
from sqlalchemy import select
from test_scheduler_acceptance import (
    _remove_disposable_subscription_rows,  # noqa: F401 - pytest fixture
)
from test_subscription_candidate_revision import frozen_publication_case, revision_command
from test_subscription_publication import publication_validator


def operator_service(case):
    _, _, dispatch, _, _, _, git = case
    return CandidateRevisionService(
        dispatch._store, ApprovedPlanLoader(dispatch._store), lambda _: git
    )


async def drift_case(session_factory, tmp_path, monkeypatch):
    case, _ = await frozen_publication_case(session_factory, tmp_path)
    factory, proposal, dispatch, _, _, _, git = case
    command = await revision_command(
        factory, session_factory, proposal.decision.run_id, approve=True
    )
    snapshot = git.working_tree_snapshot

    def drift(*args, **kwargs):
        return replace(
            snapshot(*args, **kwargs),
            files=(
                GitSnapshotFile(
                    path="apps/changed.py", mode="100644", content_digest="e" * 64, byte_count=1
                ),
            ),
            changed_paths=("apps/changed.py",),
        )

    monkeypatch.setattr(git, "working_tree_snapshot", drift)
    handler = ApprovePrHandler(
        publication_validator(dispatch, proposal, git),
        ApprovedPlanLoader(dispatch._store),
        subscription_revisions=SubscriptionCandidateRevisionController(
            dispatch._store, lambda _: git
        ),
    )
    return case, command, handler


@pytest.mark.integration
@pytest.mark.parametrize("stale", ["epoch", "task-pause"])
async def test_revision_cannot_reopen_stale_primary_authority(session_factory, tmp_path, stale):
    case, _ = await frozen_publication_case(session_factory, tmp_path)
    factory, proposal, _, _, _, _, _ = case
    command = await revision_command(factory, session_factory, proposal.decision.run_id)
    async with factory() as work:
        if stale == "epoch":
            (await work.session.get(SubscriptionSchedulerRun, command.run_id)).candidate_epoch += 1
        else:
            (
                await work.session.get(SubscriptionTask, proposal.decision.task_id)
            ).pause_requested = True
        await work.commit()
    async with factory() as work:
        with pytest.raises((SubscriptionDecisionError, CommandRecoveryRequired)):
            await operator_service(case).execute(command, work)
    async with factory() as work:
        assert (await work.runs.get(command.run_id)).state is RunState.AWAITING_PR_APPROVAL
        assert await work.session.get(SubscriptionRepairDebit, proposal.attempt_id) is None


@pytest.mark.integration
async def test_expired_revision_lease_after_observation_cannot_requeue(
    session_factory, tmp_path, monkeypatch
):
    case, _ = await frozen_publication_case(session_factory, tmp_path)
    factory, proposal, _, _, _, _, _ = case
    command = await revision_command(factory, session_factory, proposal.decision.run_id)
    service = operator_service(case)
    capture, calls = service._subscription._observe, 0
    async with factory() as work:

        async def expire(source):
            nonlocal calls
            value = await capture(source)
            calls += 1
            if calls == 2:
                (await work.session.get(RunCommand, command.id)).lease_expires_at = datetime.now(
                    UTC
                ) - timedelta(seconds=1)
                await work.session.flush()
            return value

        monkeypatch.setattr(service._subscription, "_observe", expire)
        with pytest.raises(CommandLeaseLost):
            await service.execute(command, work)
    async with factory() as work:
        assert (await work.runs.get(command.run_id)).state is RunState.AWAITING_PR_APPROVAL
        assert await work.session.get(SubscriptionRepairDebit, proposal.attempt_id) is None


@pytest.mark.integration
async def test_approval_consumption_failure_rolls_back_requeue_and_invalidation(
    session_factory, tmp_path, monkeypatch
):
    case, command, handler = await drift_case(session_factory, tmp_path, monkeypatch)
    factory, proposal, _, _, _, runner, _ = case
    settled = handler._settled

    async def crash(*args, **kwargs):
        await settled(*args, **kwargs)
        raise RuntimeError("after approval consumption")

    with monkeypatch.context() as patch:
        patch.setattr(handler, "_settled", crash)
        async with factory() as work:
            with pytest.raises(RuntimeError, match="after approval consumption"):
                await handler(command, work)
    async with factory() as work:
        run = await work.runs.get(command.run_id)
        assert run.state is RunState.AWAITING_PR_APPROVAL and run.local_remediation_count == 0
        assert (
            await work.session.get(Approval, UUID(command.payload["approval_id"]))
        ).invalidated_at is None
        assert await work.session.get(SubscriptionRepairDebit, proposal.attempt_id) is None
        assert (
            await work.session.get(SubscriptionTask, proposal.decision.task_id)
        ).state == "blocked"
    async with factory() as work:
        await handler(command, work)
        assert (await work.runs.get(command.run_id)).state is RunState.REMEDIATING
    assert runner.calls == runner.runner.calls == 1


@pytest.mark.integration
@pytest.mark.parametrize("limit", ["task", "automatic-run"])
async def test_exhausted_drift_revision_requires_human_intervention(
    session_factory, tmp_path, monkeypatch, limit
):
    case, command, handler = await drift_case(session_factory, tmp_path, monkeypatch)
    factory, proposal, _, _, _, _, _ = case
    async with factory() as work:
        if limit == "task":
            task = await work.session.get(SubscriptionScheduledTask, proposal.decision.task_id)
            task.repairs = task.max_repairs
        else:
            (
                await work.session.get(Run, command.run_id)
            ).local_remediation_count = proposal.policy.local_remediation_limit
        await work.commit()
    async with factory() as work:
        await handler(command, work)
    async with factory() as work:
        await handler(command, work)
        run = await work.runs.get(command.run_id)
        assert run.state is RunState.AWAITING_HUMAN_INTERVENTION
        assert await work.session.get(SubscriptionRepairDebit, proposal.attempt_id) is None
        assert (
            await work.session.get(SubscriptionSchedulerRun, command.run_id)
        ).candidate_state == "closed"
        # Resolve this disposable intervention before the fixture's downgrade,
        # as in the retained legacy PR approval tests. Keep the migration guard.
        await work.runs.transition(run.id, run.version, RunState.CANCELLED, "test.cleanup", {})
        await work.commit()


@pytest.mark.integration
async def test_operator_revision_preserves_existing_automatic_repairs(session_factory, tmp_path):
    case, _ = await frozen_publication_case(session_factory, tmp_path)
    factory, proposal, _, _, _, _, _ = case
    command = await revision_command(factory, session_factory, proposal.decision.run_id)
    async with factory() as work:
        (
            await work.session.get(Run, command.run_id)
        ).local_remediation_count = proposal.policy.local_remediation_limit
        await work.commit()
    async with factory() as work:
        await operator_service(case).execute(command, work)
    async with factory() as work:
        run = await work.runs.get(command.run_id)
        assert run.state is RunState.REMEDIATING
        assert run.local_remediation_count == proposal.policy.local_remediation_limit
        assert await work.session.get(SubscriptionRepairDebit, proposal.attempt_id) is not None


@pytest.mark.integration
@pytest.mark.parametrize("corruption", ["feedback-bytes", "revision-event"])
async def test_revision_replay_reproves_retained_feedback_and_decision(
    session_factory, tmp_path, monkeypatch, corruption
):
    case, _ = await frozen_publication_case(session_factory, tmp_path)
    factory, proposal, dispatch, _, _, runner, _ = case
    command = await revision_command(factory, session_factory, proposal.decision.run_id)
    service = operator_service(case)
    async with factory() as work:
        await service.execute(command, work)
    async with factory() as work:
        event = await work.session.scalar(
            select(RunEvent).where(RunEvent.run_id == command.run_id, RunEvent.event_type == EVENT)
        )
        if corruption == "revision-event":
            event.payload = {**event.payload, "unbound_instruction": True}
            await work.commit()
        feedback_digest = event.payload["revision"]["feedback_digest"]
    if corruption == "feedback-bytes":
        original = dispatch._store.open_bytes

        async def corrupt(digest, **kwargs):
            value = await original(digest, **kwargs)
            return value + b" " if digest == feedback_digest else value

        monkeypatch.setattr(dispatch._store, "open_bytes", corrupt)
    async with factory() as work:
        with pytest.raises((CommandRecoveryRequired, SubscriptionDecisionError)):
            await service.execute(command, work)
    assert runner.calls == runner.runner.calls == 1
