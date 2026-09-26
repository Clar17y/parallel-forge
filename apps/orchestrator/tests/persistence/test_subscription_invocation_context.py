"""Invocation context must still belong to the admitted durable attempt."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
from forge.persistence.models.scheduling import SubscriptionScheduledTask, SubscriptionSchedulerRun
from forge.persistence.models.subscription import SubscriptionAttempt, SubscriptionTask
from forge.persistence.repositories.subscription import SubscriptionConflict
from forge.persistence.unit_of_work import PostgresUnitOfWork
from sqlalchemy import select
from test_scheduler_acceptance import (  # noqa: F401
    _admit_run,
    _enqueue,
    _remove_disposable_subscription_rows,
    _route,
)
from test_subscription_usage import _reservation


async def _admission(session_factory, run):
    async with PostgresUnitOfWork(session_factory) as work:
        primary = await _admit_run(work, run, (_route("p"), _route("p")))
        await _enqueue(
            work,
            run.id,
            provider="p",
            worktree="invocation-tree",
            parent_id=primary,
            paths=("apps",),
        )
        await work.commit()
    admission = await SubscriptionDecisionExecutor(
        lambda: PostgresUnitOfWork(session_factory)
    ).admit_next("worker", _reservation())
    assert admission is not None
    return admission


@pytest.mark.integration
async def test_invocation_context_requires_current_admission(session_factory, persisted_run):
    admission = await _admission(session_factory, persisted_run)
    async with PostgresUnitOfWork(session_factory) as work:
        context = await work.subscription_execution.invocation_context(admission)
        assert context.worktree_id == "invocation-tree" and not context.candidate_closed
        known = await work.subscription.invocation_tasks(persisted_run.id, admission.task.task_id)
        assert len(known) == 1 and known[0].task_id == admission.task.parent_task_id
        assert await work.subscription.invocation_tasks(uuid4(), admission.task.task_id) == ()
        with pytest.raises(SubscriptionConflict):
            await work.subscription_execution.invocation_context(
                replace(admission, task_version=admission.task_version + 1)
            )


@pytest.mark.integration
async def test_known_task_context_rejects_foreign_payload_identity(session_factory, persisted_run):
    from forge.domain.subscription import encode_subscription_record

    admission = await _admission(session_factory, persisted_run)
    async with PostgresUnitOfWork(session_factory) as work:
        primary = await work.subscription.get_task(persisted_run.id, admission.task.parent_task_id)
        row = await work.session.get(SubscriptionTask, primary.task_id)
        row.payload = encode_subscription_record(replace(primary, run_id=uuid4()))
        await work.commit()
    async with PostgresUnitOfWork(session_factory) as work:
        with pytest.raises(SubscriptionConflict, match="identity"):
            await work.subscription.invocation_tasks(persisted_run.id, admission.task.task_id)


@pytest.mark.integration
@pytest.mark.parametrize(
    "change",
    [
        "expired",
        "owner",
        "generation",
        "task_version",
        "task_contract",
        "attempt_terminal",
        "candidate",
        "pause",
        "scheduled_pause",
        "scheduled_paths",
        "scheduled_provider",
        "scheduled_readonly",
        "scheduled_parent",
        "scheduled_dependencies",
        "run_pause",
        "pending_cancel",
    ],
)
async def test_invocation_context_rejects_revoked_context(session_factory, persisted_run, change):
    admission = await _admission(session_factory, persisted_run)
    async with PostgresUnitOfWork(session_factory) as work:
        logical = await work.session.get(SubscriptionTask, admission.task.task_id)
        scheduled = await work.session.scalar(
            select(SubscriptionScheduledTask).where(
                SubscriptionScheduledTask.task_id == admission.task.task_id
            )
        )
        if change == "expired":
            scheduled.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        elif change == "owner":
            scheduled.lease_owner = "other"
        elif change == "generation":
            scheduled.lease_generation += 1
        elif change == "task_version":
            logical.version += 1
        elif change == "task_contract":
            from forge.domain.subscription import encode_subscription_record

            logical.payload = encode_subscription_record(
                replace(admission.task, owned_paths=("other",))
            )
        elif change == "attempt_terminal":
            attempt = await work.session.get(SubscriptionAttempt, admission.attempt.attempt_id)
            attempt.status = "terminal"
        elif change == "candidate":
            row = await work.session.get(SubscriptionSchedulerRun, persisted_run.id)
            row.candidate_epoch += 1
        elif change == "pause":
            logical.pause_requested = True
        elif change == "scheduled_pause":
            scheduled.pause_requested = True
        elif change == "scheduled_paths":
            scheduled.owned_paths = ["foreign"]
        elif change == "scheduled_provider":
            scheduled.provider = "foreign"
        elif change == "scheduled_readonly":
            scheduled.read_only = True
        elif change == "scheduled_parent":
            scheduled.parent_task_id = uuid4()
        elif change == "scheduled_dependencies":
            scheduled.dependency_task_ids = [uuid4()]
        elif change == "run_pause":
            run = await work.runs.get_for_update(persisted_run.id)
            await work.runs.pause(run.id, run.version, "run.paused", {})
        elif change == "pending_cancel":
            run = await work.runs.get_for_update(persisted_run.id)
            await work.commands.enqueue(
                run_id=run.id,
                command_type="cancel",
                payload={},
                expected_run_version=run.version,
                idempotency_key="stop",
                actor_id=uuid4(),
            )
        await work.commit()
    async with PostgresUnitOfWork(session_factory) as work:
        with pytest.raises(SubscriptionConflict):
            await work.subscription_execution.invocation_context(admission)

@pytest.mark.integration
@pytest.mark.parametrize("state", ["draining", "closed"])
async def test_writer_context_rejects_candidate_state_change_without_epoch_change(
    session_factory, persisted_run, state
):
    admission = await _admission(session_factory, persisted_run)
    async with PostgresUnitOfWork(session_factory) as work:
        row = await work.session.get(SubscriptionSchedulerRun, persisted_run.id)
        row.candidate_state = state
        await work.commit()
    async with PostgresUnitOfWork(session_factory) as work:
        with pytest.raises(SubscriptionConflict, match="candidate barrier"):
            await work.subscription_execution.invocation_context(admission)


@pytest.mark.integration
@pytest.mark.parametrize("state", ["draining", "closed"])
async def test_execution_admission_rechecks_candidate_after_lease(
    session_factory, persisted_run, state
):
    async with PostgresUnitOfWork(session_factory) as work:
        primary = await _admit_run(work, persisted_run, (_route("p"), _route("p")))
        await _enqueue(
            work, persisted_run.id, provider="p", worktree="invocation-tree",
            parent_id=primary, paths=("apps",),
        )
        lease = await work.scheduler.claim_execution_ready("worker", timedelta(seconds=30))
        assert lease is not None
        row = await work.session.get(SubscriptionSchedulerRun, persisted_run.id)
        row.candidate_state = state
        await work.commit()
    async with PostgresUnitOfWork(session_factory) as work:
        with pytest.raises(SubscriptionConflict, match="candidate barrier"):
            await work.subscription_execution.admit(lease, uuid4())
