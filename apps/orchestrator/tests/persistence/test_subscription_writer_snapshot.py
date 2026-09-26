"""A writer's verified snapshot freezes effects without requiring idle peers to exit."""

from uuid import uuid4

import pytest
from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
from forge.domain.operation import canonical_digest
from forge.domain.scheduling import SchedulerCapacityPolicy
from forge.domain.subscription import ToolCallBinding
from forge.domain.tool import ToolName
from forge.persistence.models.scheduling import (
    SubscriptionScheduledEffect,
    SubscriptionScheduledTask,
)
from forge.persistence.models.subscription import SubscriptionAttempt
from forge.persistence.repositories.scheduling import SchedulingConflict
from forge.persistence.unit_of_work import PostgresUnitOfWork
from test_scheduler_acceptance import (
    _admit_run,
    _enqueue,
    _remove_disposable_subscription_rows,  # noqa: F401 - autouse disposable cleanup
    _route,
)
from test_subscription_usage import _reservation


async def writers(session_factory, run):
    factory = lambda: PostgresUnitOfWork(session_factory)
    async with factory() as work:
        await work.scheduler.configure_capacity(SchedulerCapacityPolicy(version=2))
        primary = await _admit_run(work, run, (_route("p"), _route("p")))
        for name in ("a", "b"):
            await _enqueue(
                work,
                run.id,
                provider="p",
                worktree="shared",
                parent_id=primary,
                paths=(f"apps/{name}",),
            )
        await work.commit()
    executor = SubscriptionDecisionExecutor(factory)
    first = await executor.admit_next("writer-a", _reservation())
    second = await executor.admit_next("writer-b", _reservation())
    assert first is not None and second is not None
    return factory, first, second


async def bind_snapshot(work, admission, *, fault=None):
    effect_id = uuid4()
    if fault != "missing":
        tool = {
            "status": ToolName.GIT_STATUS,
            "check": ToolName.BUILD_RUN_NAMED_CHECK,
            "commit": ToolName.GIT_COMMIT,
        }.get(fault, ToolName.GIT_DIFF)
        arguments = {"scope": "working" if fault == "scope" else "snapshot"}
        await work.subscription.bind_operation(
            ToolCallBinding(
                attempt_id=admission.attempt.attempt_id,
                provider_call_key="snapshot",
                durable_operation_id=effect_id,
                tool_name=tool,
                arguments_digest=canonical_digest(arguments),
            ),
            run_id=admission.task.run_id,
            task_id=admission.task.task_id,
        )
    if fault == "generation":
        (
            await work.session.get(SubscriptionAttempt, admission.attempt.attempt_id)
        ).lease_generation += 1
    await work.commit()
    return effect_id


@pytest.mark.integration
@pytest.mark.parametrize(
    "fault", [None, "missing", "scope", "status", "check", "commit", "generation"]
)
async def test_only_exact_bound_snapshot_can_read_while_other_writer_is_idle(
    session_factory, persisted_run, fault
):
    factory, first, second = await writers(session_factory, persisted_run)
    async with factory() as work:
        identity = await bind_snapshot(work, first, fault=fault)
    async with factory() as work:
        if fault:
            with pytest.raises(SchedulingConflict, match="not exclusively available"):
                await work.scheduler.admit_effect(
                    first.lease, identity, whole_worktree_exclusive=True
                )
            assert await work.session.get(SubscriptionScheduledEffect, identity) is None
        else:
            effect = await work.scheduler.admit_effect(
                first.lease, identity, whole_worktree_exclusive=True
            )
            assert (
                await work.session.get(SubscriptionScheduledEffect, identity)
            ).whole_worktree_exclusive
            await work.commit()
            with pytest.raises(SchedulingConflict, match="barrier"):
                await work.scheduler.admit_effect(
                    second.lease, uuid4(), owned_paths=second.task.owned_paths
                )
            await work.rollback()
            # Exact replay retains the same barrier, including after restart.
            assert (
                await work.scheduler.admit_effect(
                    first.lease, identity, whole_worktree_exclusive=True
                )
                == effect
            )
            await work.commit()
    if fault is None:
        async with factory() as work:
            assert await work.scheduler.settle_effect(effect, accepted=True)
            await work.commit()
        async with factory() as work:
            await work.scheduler.admit_effect(
                second.lease, uuid4(), owned_paths=second.task.owned_paths
            )
            await work.commit()


@pytest.mark.integration
@pytest.mark.parametrize("state", ["admitted", "reconciling", "orphan", "owner-reconciling"])
async def test_snapshot_waits_for_every_unsettled_peer_effect_or_uncertain_owner(
    session_factory, persisted_run, state
):
    factory, first, second = await writers(session_factory, persisted_run)
    async with factory() as work:
        identity = await bind_snapshot(work, first)
        if state == "owner-reconciling":
            (
                await work.session.get(SubscriptionScheduledTask, second.task.task_id)
            ).state = "reconciling"
        else:
            effect = await work.scheduler.admit_effect(
                second.lease, uuid4(), owned_paths=second.task.owned_paths
            )
            row = await work.session.get(SubscriptionScheduledEffect, effect.effect_id)
            if state == "orphan":
                row.task_id = uuid4()
            else:
                row.state = state
        await work.commit()
    async with factory() as work:
        with pytest.raises(SchedulingConflict, match="not exclusively available"):
            await work.scheduler.admit_effect(first.lease, identity, whole_worktree_exclusive=True)
        assert await work.session.get(SubscriptionScheduledEffect, identity) is None
    if state == "admitted":
        async with factory() as work:
            assert await work.scheduler.settle_effect(effect, accepted=True)
            await work.commit()
        async with factory() as work:
            await work.scheduler.admit_effect(first.lease, identity, whole_worktree_exclusive=True)
            await work.commit()
