"""Operator capacity observations share the scheduler's actual slot accounting."""

from dataclasses import replace
from uuid import uuid4

import pytest
from forge.api.schemas.subscription_tasks import SubscriptionTaskPage
from forge.domain.scheduling import SchedulerCapacityPolicy
from forge.persistence.queries.subscription_tasks import SubscriptionTaskQuery
from forge.persistence.unit_of_work import PostgresUnitOfWork
from test_scheduler_acceptance import (
    _admit_run,
    _claim,
    _enqueue,
    _remove_disposable_subscription_rows,  # noqa: F401
    _route,
)


@pytest.mark.integration
@pytest.mark.parametrize("dimension", ["host", "run", "provider"])
async def test_capacity_wait_and_fair_resume_are_visible(session_factory, persisted_run, dimension):
    other = replace(persisted_run, id=uuid4())
    limits = {"host": (1, 3, 3), "run": (3, 1, 3), "provider": (3, 3, 1)}[dimension]
    async with PostgresUnitOfWork(session_factory) as work:
        await work.runs.create(other)
        await work.scheduler.configure_capacity(
            SchedulerCapacityPolicy(
                version=30, global_limit=limits[0], run_limit=limits[1], provider_limit=limits[2]
            )
        )
        parent_a = await _admit_run(work, persisted_run, (_route("p"),))
        parent_b = await _admit_run(work, other, (_route("p"),))
        first = await _enqueue(
            work, persisted_run.id, provider="p", worktree="a", parent_id=parent_a
        )
        queued = await _enqueue(
            work, persisted_run.id, provider="p", worktree="a", parent_id=parent_a
        )
        peer = await _enqueue(work, other.id, provider="p", worktree="b", parent_id=parent_b)
        await work.commit()
    active = await _claim(session_factory, "capacity-a")
    assert active is not None and active.task_id == first
    query = SubscriptionTaskQuery(session_factory)
    page = SubscriptionTaskPage.model_validate(await query.tasks(persisted_run.id))
    assert page.capacity is not None
    assert page.capacity.policy_version == 30
    assert page.capacity.host.active == page.capacity.run.active == 1
    assert page.capacity.run.limit == limits[1]
    assert page.capacity.queue_order == "least_recently_served_run_then_oldest_task"
    waits = next(task.capacity_waits for task in page.tasks if task.task_id == queued)
    assert waits == [dimension]
    assert next(task.capacity_waits for task in page.tasks if task.task_id == first) == []
    if dimension != "run":
        assert await _claim(session_factory, "full-host-or-provider") is None
    async with PostgresUnitOfWork(session_factory) as work:
        await work.scheduler.finish(active, successful=True)
        await work.commit()
    released = SubscriptionTaskPage.model_validate(await query.tasks(persisted_run.id))
    assert next(task.capacity_waits for task in released.tasks if task.task_id == queued) == []
    next_claim = await _claim(session_factory, "capacity-b")
    assert next_claim is not None and next_claim.task_id == peer
    # The ordering description corresponds to real admission: the other run
    # wins even though A's second task was enqueued before B's first task.


@pytest.mark.integration
async def test_capacity_read_preserves_frozen_run_limit_and_legacy_unknown(
    session_factory, persisted_run
):
    query = SubscriptionTaskQuery(session_factory)
    legacy = SubscriptionTaskPage.model_validate(await query.tasks(persisted_run.id))
    assert legacy.capacity is None
    async with PostgresUnitOfWork(session_factory) as work:
        await work.scheduler.configure_capacity(
            SchedulerCapacityPolicy(version=30, global_limit=2, run_limit=3, provider_limit=2)
        )
        await _admit_run(work, persisted_run, (_route("p"),))
        await work.scheduler.configure_capacity(
            SchedulerCapacityPolicy(version=31, global_limit=4, run_limit=1, provider_limit=4)
        )
        await work.commit()
    page = SubscriptionTaskPage.model_validate(await query.tasks(persisted_run.id))
    assert page.capacity.policy_version == 31
    assert page.capacity.host.limit == 4 and page.capacity.run.limit == 3
    assert page.capacity.host.active == 0


@pytest.mark.integration
@pytest.mark.parametrize("confirmed", [False, True])
async def test_capacity_excludes_only_a_proved_stopped_pause(session_factory, tmp_path, confirmed):
    from forge.application.ports.subscription_gateway import (
        SubscriptionFailure,
        SubscriptionInvocationResult,
    )
    from forge.application.services.subscription_task_controls import SubscriptionTaskControlService
    from test_subscription_task_control_recovery import (
        active_case,
        control,
        finish_client,
        scope_result,
    )
    from test_subscription_usage import _known

    factory, _, child, executor, proof = await active_case(session_factory, tmp_path)
    await control(factory, child, "pause")
    query = SubscriptionTaskQuery(session_factory)
    before = SubscriptionTaskPage.model_validate(await query.tasks(child.task.run_id))
    assert before.capacity.host.active == 1
    if confirmed:
        await finish_client(factory, child, proof)
        await executor.settle(child, scope_result(child, proof))
    else:
        uncertain = proof.model_copy(
            update={"stop_confirmed": False, "outcome": "uncertain", "return_code": None}
        )
        await finish_client(factory, child, uncertain, uncertain=True)
        await executor.settle(
            child,
            SubscriptionInvocationResult(
                attempt=child.attempt,
                failure=SubscriptionFailure.UNCERTAIN,
                telemetry=_known(),
                launch_proof=uncertain,
            ),
        )
    await SubscriptionTaskControlService(factory).reconcile_all()
    after = SubscriptionTaskPage.model_validate(await query.tasks(child.task.run_id))
    assert after.capacity.host.active == after.capacity.run.active == int(not confirmed)
    assert all(not task.capacity_waits for task in after.tasks)
