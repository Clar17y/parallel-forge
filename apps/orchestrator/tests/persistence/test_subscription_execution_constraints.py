"""Retain admitted identity and expired-lease quarantine across transactions."""

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
from forge.persistence.models.scheduling import SubscriptionScheduledTask
from forge.persistence.models.subscription import SubscriptionAttempt
from forge.persistence.unit_of_work import PostgresUnitOfWork
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from test_scheduler_acceptance import (  # noqa: F401
    _admit_run,
    _enqueue,
    _remove_disposable_subscription_rows,
    _route,
)
from test_subscription_usage import _reservation


async def _admitted(factory, run):
    async with PostgresUnitOfWork(factory) as work:
        primary = await _admit_run(work, run, (_route("p"), _route("p")))
        await _enqueue(
            work,
            run.id,
            provider="p",
            worktree="constraint-tree",
            parent_id=primary,
            paths=("apps",),
        )
        await work.commit()
    executor = SubscriptionDecisionExecutor(lambda: PostgresUnitOfWork(factory))
    admission = await executor.admit_next("worker", _reservation())
    assert admission is not None
    return executor, admission


@pytest.mark.integration
async def test_empty_claim_commits_expired_attempt_quarantine(session_factory, persisted_run):
    executor, admission = await _admitted(session_factory, persisted_run)
    async with PostgresUnitOfWork(session_factory) as work:
        row = await work.session.scalar(
            select(SubscriptionScheduledTask).where(
                SubscriptionScheduledTask.task_id == admission.task.task_id
            )
        )
        row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await work.commit()
    assert await executor.admit_next("replacement", _reservation()) is None
    async with PostgresUnitOfWork(session_factory) as work:
        row = await work.session.scalar(
            select(SubscriptionScheduledTask).where(
                SubscriptionScheduledTask.task_id == admission.task.task_id
            )
        )
        assert row.state == "reconciling"
        assert (
            await work.subscription_budget.usage(persisted_run.id)
        ).outstanding.provider_attempts == 1


@pytest.mark.integration
async def test_admitted_identity_cannot_be_partially_cleared(session_factory, persisted_run):
    _, admission = await _admitted(session_factory, persisted_run)
    with pytest.raises(IntegrityError):
        async with PostgresUnitOfWork(session_factory) as work:
            row = await work.session.get(SubscriptionAttempt, admission.attempt.attempt_id)
            row.lease_generation = None
            await work.commit()


@pytest.mark.integration
async def test_execution_migration_refuses_to_discard_admitted_identity(
    session_factory, persisted_run
):
    _, admission = await _admitted(session_factory, persisted_run)
    path = (
        Path(__file__).resolve().parents[2]
        / "migrations/versions/20260910_0013_subscription_execution.py"
    )
    spec = importlib.util.spec_from_file_location("execution_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def downgrade(connection):
        with Operations.context(MigrationContext.configure(connection)):
            module.downgrade()

    async with session_factory() as session, session.begin():
        with pytest.raises(RuntimeError, match="admitted subscription execution"):
            await (await session.connection()).run_sync(downgrade)
    async with PostgresUnitOfWork(session_factory) as work:
        row = await work.session.get(SubscriptionAttempt, admission.attempt.attempt_id)
        assert row.lease_owner == "worker"


@pytest.mark.integration
async def test_legacy_terminal_attempt_without_accounting_cannot_reset_usage(
    session_factory, persisted_run
):
    from test_subscription_usage import _case

    _task, attempt = await _case(session_factory, persisted_run)
    async with PostgresUnitOfWork(session_factory) as work:
        row = await work.session.get(SubscriptionAttempt, attempt)
        row.status = "terminal"
        await work.commit()
    executor = SubscriptionDecisionExecutor(lambda: PostgresUnitOfWork(session_factory))
    assert await executor.admit_next("replacement", _reservation()) is None


@pytest.mark.integration
async def test_unaccounted_candidate_does_not_stall_another_worktree(
    session_factory, persisted_run
):
    from uuid import uuid4

    from forge.domain.run import RunSnapshot
    from test_subscription_usage import _case

    _, attempt_id = await _case(session_factory, persisted_run)
    second = RunSnapshot(
        id=uuid4(),
        project_id=persisted_run.project_id,
        task_id=persisted_run.task_id,
        policy_version=persisted_run.policy_version,
    )
    async with PostgresUnitOfWork(session_factory) as work:
        attempt = await work.session.get(SubscriptionAttempt, attempt_id)
        attempt.status = "terminal"
        await work.runs.create(second)
        parent = await _admit_run(work, second, (_route("p"), _route("p")))
        await _enqueue(
            work,
            second.id,
            provider="p",
            worktree="independent-tree",
            parent_id=parent,
            paths=("apps",),
        )
        await work.commit()
    executor = SubscriptionDecisionExecutor(lambda: PostgresUnitOfWork(session_factory))
    admitted = await executor.admit_next("independent", _reservation())
    assert admitted is not None and admitted.lease.run_id == second.id
