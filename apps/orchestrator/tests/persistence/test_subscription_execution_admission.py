"""Atomic attempt admission joins the scheduler and usage reservation."""

from datetime import timedelta

import pytest
from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
from forge.persistence.models.subscription import SubscriptionAttempt, SubscriptionTask
from forge.persistence.unit_of_work import PostgresUnitOfWork
from sqlalchemy import select
from test_scheduler_acceptance import (  # noqa: F401
    _admit_run,
    _enqueue,
    _remove_disposable_subscription_rows,
    _route,
)
from test_subscription_usage import _reservation


@pytest.mark.integration
async def test_execution_admission_persists_fresh_attempt_and_budget_together(
    session_factory, persisted_run
):
    async with PostgresUnitOfWork(session_factory) as work:
        primary = await _admit_run(work, persisted_run, (_route("p"), _route("p")))
        task = await _enqueue(
            work,
            persisted_run.id,
            provider="p",
            worktree="admission-tree",
            parent_id=primary,
            paths=("apps",),
        )
        await work.commit()
    executor = SubscriptionDecisionExecutor(lambda: PostgresUnitOfWork(session_factory))
    admission = await executor.admit_next("worker", _reservation(), lease_for=timedelta(seconds=30))
    assert admission is not None and admission.task.task_id == task
    assert admission.attempt.attempt_number == 1
    assert admission.envelope.run_id == persisted_run.id
    async with PostgresUnitOfWork(session_factory) as work:
        usage = await work.subscription_budget.usage(persisted_run.id, task)
        assert usage.outstanding.provider_attempts == 1
        rows = (
            await work.session.scalars(
                select(SubscriptionAttempt).where(SubscriptionAttempt.task_row_id == task)
            )
        ).all()
        assert len(rows) == 1 and rows[0].id == admission.attempt.attempt_id
        assert rows[0].status == "running"
        logical = await work.session.get(SubscriptionTask, task)
        assert logical.state == "running" and logical.version == 1
        assert rows[0].lease_owner == admission.lease.owner
        assert rows[0].lease_generation == admission.lease.generation
    assert await executor.admit_next("other", _reservation()) is None


@pytest.mark.integration
async def test_budget_rejection_rolls_back_claim_and_attempt(session_factory, persisted_run):
    from dataclasses import replace

    from forge.persistence.models.scheduling import SubscriptionScheduledTask

    async with PostgresUnitOfWork(session_factory) as work:
        primary = await _admit_run(work, persisted_run, (_route("p"), _route("p")))
        task = await _enqueue(
            work,
            persisted_run.id,
            provider="p",
            worktree="admission-tree",
            parent_id=primary,
            paths=("apps",),
        )
        await work.commit()
    executor = SubscriptionDecisionExecutor(lambda: PostgresUnitOfWork(session_factory))
    with pytest.raises(ValueError):
        await executor.admit_next("worker", replace(_reservation(), max_provider_attempts=2))
    async with PostgresUnitOfWork(session_factory) as work:
        scheduled = await work.session.scalar(
            select(SubscriptionScheduledTask).where(SubscriptionScheduledTask.task_id == task)
        )
        assert scheduled.state == "queued" and scheduled.lease_owner is None
        assert not (
            await work.session.scalars(
                select(SubscriptionAttempt).where(SubscriptionAttempt.task_row_id == task)
            )
        ).all()
        assert (
            await work.subscription_budget.usage(persisted_run.id)
        ).outstanding.provider_attempts == 0
    assert await executor.admit_next("worker", _reservation()) is not None


@pytest.mark.integration
async def test_admission_rejects_stale_lease_and_unsettled_prior_attempt(
    session_factory, persisted_run
):
    from dataclasses import replace
    from uuid import uuid4

    from forge.persistence.repositories.subscription import SubscriptionConflict

    async with PostgresUnitOfWork(session_factory) as work:
        primary = await _admit_run(work, persisted_run, (_route("p"), _route("p")))
        await _enqueue(
            work,
            persisted_run.id,
            provider="p",
            worktree="admission-tree",
            parent_id=primary,
            paths=("apps",),
        )
        lease = await work.scheduler.claim_ready("worker", timedelta(seconds=30))
        assert lease is not None
        with pytest.raises(SubscriptionConflict, match="lease"):
            await work.subscription_execution.admit(
                replace(lease, generation=lease.generation + 1), uuid4()
            )
        admission = await work.subscription_execution.admit(lease, uuid4())
        await work.subscription_budget.reserve_attempt(
            lease.run_id,
            lease.task_id,
            admission.attempt.attempt_id,
            _reservation(),
            idempotency_key="first",
        )
        await work.commit()
    async with PostgresUnitOfWork(session_factory) as work:
        with pytest.raises(SubscriptionConflict, match="previous attempt"):
            await work.subscription_execution.admit(lease, uuid4())


@pytest.mark.integration
async def test_locked_run_does_not_block_other_worktree_admission(session_factory, persisted_run):
    import asyncio
    from uuid import uuid4

    from forge.domain.run import RunSnapshot

    second = RunSnapshot(
        id=uuid4(),
        project_id=persisted_run.project_id,
        task_id=persisted_run.task_id,
        policy_version=persisted_run.policy_version,
    )
    async with PostgresUnitOfWork(session_factory) as work:
        await work.runs.create(second)
        for run in (persisted_run, second):
            primary = await _admit_run(work, run, (_route("p"), _route("p")))
            await _enqueue(
                work,
                run.id,
                provider="p",
                worktree=f"tree-{run.id}",
                parent_id=primary,
                paths=("apps",),
            )
        await work.commit()
    executor = SubscriptionDecisionExecutor(lambda: PostgresUnitOfWork(session_factory))
    async with PostgresUnitOfWork(session_factory) as held:
        await held.runs.get_for_update(persisted_run.id)
        admitted = await asyncio.wait_for(executor.admit_next("other", _reservation()), 1)
        assert admitted is not None and admitted.lease.run_id == second.id
