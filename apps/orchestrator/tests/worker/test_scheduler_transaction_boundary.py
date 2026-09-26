"""Real-database transaction boundary checks for the durable scheduler worker."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
import pytest_asyncio
from forge.domain.run import RunSnapshot
from forge.domain.scheduling import SchedulerCapacityPolicy
from forge.persistence.unit_of_work import PostgresUnitOfWork
from forge.worker.subscription import DurableSubscriptionTaskWorker
from sqlalchemy import text

from apps.orchestrator.tests.persistence.test_scheduler_acceptance import (
    _admit_run,
    _claim,
    _enqueue,
    _route,
)


@pytest_asyncio.fixture(autouse=True)
async def _remove_disposable_subscription_rows(session_factory):
    yield
    async with session_factory() as session, session.begin():
        await session.execute(
            text(
                "TRUNCATE TABLE subscription_decision_records, subscription_budget_reservations, "
                "subscription_budget_pools, subscription_operation_bindings, subscription_attempts, "
                "subscription_task_dependencies, subscription_tasks, subscription_envelopes, "
                "project_subscription_profiles, subscription_profile_versions CASCADE"
            )
        )


@pytest.mark.integration
async def test_another_database_session_claims_while_gateway_is_blocked(
    session_factory, persisted_run
) -> None:
    """A provider wait must retain neither the scheduler lock nor its DB transaction."""
    second_run = RunSnapshot(
        id=uuid4(),
        project_id=persisted_run.project_id,
        task_id=persisted_run.task_id,
        policy_version=persisted_run.policy_version,
    )
    async with PostgresUnitOfWork(session_factory) as work:
        await work.runs.create(second_run)
        await work.scheduler.configure_capacity(
            SchedulerCapacityPolicy(version=30, global_limit=2, run_limit=1, provider_limit=2)
        )
        first_parent = await _admit_run(work, persisted_run, (_route("provider-a"),))
        second_parent = await _admit_run(work, second_run, (_route("provider-b"),))
        first_task = await _enqueue(
            work,
            persisted_run.id,
            provider="provider-a",
            worktree="tree-a",
            parent_id=first_parent,
        )
        second_task = await _enqueue(
            work,
            second_run.id,
            provider="provider-b",
            worktree="tree-b",
            parent_id=second_parent,
        )
        await work.commit()

    gateway_entered = asyncio.Event()
    release_gateway = asyncio.Event()

    async def gateway(lease):
        assert lease.task_id == first_task
        gateway_entered.set()
        await release_gateway.wait()
        return True

    worker = DurableSubscriptionTaskWorker(
        lambda: PostgresUnitOfWork(session_factory), gateway, owner="worker-a"
    )
    running = asyncio.create_task(worker.run_once())
    await asyncio.wait_for(gateway_entered.wait(), timeout=2)
    independent = await asyncio.wait_for(_claim(session_factory, "worker-b"), timeout=2)
    assert independent is not None and independent.task_id == second_task
    release_gateway.set()
    assert await asyncio.wait_for(running, timeout=2) is True
