"""Durable scheduler persistence contracts."""

from datetime import timedelta
from uuid import uuid4

import pytest


@pytest.mark.integration
async def test_ready_claim_is_single_owner_and_requires_frozen_envelope(
    session_factory, persisted_run
) -> None:
    """The first vertical scheduler invariant: no envelope, no runnable task."""
    from forge.domain.scheduling import SchedulerCapacityPolicy, ScheduleTask
    from forge.domain.subscription import (
        AuthMode,
        BillingMode,
        ExecutionEnvelope,
        LogicalTaskContract,
        OperatorProfile,
        ReasoningEffort,
        RolePreference,
        RouteBinding,
        RouteSpec,
        SpecialistPurpose,
        TaskBudget,
    )
    from forge.persistence.unit_of_work import PostgresUnitOfWork

    task_id = uuid4()
    task = ScheduleTask(
        run_id=persisted_run.id, task_id=task_id, worktree_id="tree-a", max_repairs=3
    )
    route = RouteSpec(
        provider="fake",
        client="fake",
        model="fake",
        effort=ReasoningEffort.LOW,
        auth_mode=AuthMode.SUBSCRIPTION,
        billing_mode=BillingMode.ALLOWANCE_ONLY,
    )
    profile = OperatorProfile(
        profile_id=uuid4(),
        version=1,
        preferences=(RolePreference(purpose=SpecialistPurpose.PRIMARY, preferred_route=route),),
    )
    envelope = ExecutionEnvelope(
        run_id=persisted_run.id,
        profile_id=profile.profile_id,
        profile_version=1,
        safety_policy_version=1,
        routes=(
            (
                SpecialistPurpose.PRIMARY,
                RouteBinding(requested=route, effective=route, is_primary=True),
            ),
        ),
    )
    async with PostgresUnitOfWork(session_factory) as work:
        # Capacity is deployment configuration.  It is frozen into run admission,
        # while the current global/provider ceilings remain live for new claims.
        await work.scheduler.configure_capacity(
            SchedulerCapacityPolicy(version=2, global_limit=1, run_limit=1, provider_limit=1)
        )
        await work.subscription.store_profile(profile)
        await work.subscription.freeze_envelope(envelope)
        await work.subscription.create_task(
            LogicalTaskContract(
                run_id=persisted_run.id,
                task_id=task_id,
                purpose=SpecialistPurpose.PRIMARY,
                route=RouteBinding(requested=route, effective=route, is_primary=True),
                budget=TaskBudget(),
            ),
            idempotency_key="parent",
        )
        await work.scheduler.enqueue(task)
        from forge.persistence.repositories.scheduling import SchedulingConflict

        with pytest.raises(SchedulingConflict, match="immutable"):
            await work.scheduler.enqueue(
                ScheduleTask(
                    run_id=task.run_id,
                    task_id=task.task_id,
                    worktree_id="different-managed-tree",
                    max_repairs=3,
                )
            )
        assert await work.scheduler.claim_ready("worker-a", timedelta(seconds=30)) is None
        await work.scheduler.admit_run(persisted_run.id)
        claim = await work.scheduler.claim_ready("worker-a", timedelta(seconds=30))
        assert claim is not None
        assert claim.task_id == task.task_id
        assert await work.scheduler.claim_ready("worker-b", timedelta(seconds=30)) is None
        child = ScheduleTask(
            run_id=persisted_run.id,
            task_id=uuid4(),
            parent_task_id=task.task_id,
            worktree_id="tree-a",
            max_repairs=3,
        )
        await work.subscription.create_task(
            LogicalTaskContract(
                run_id=persisted_run.id,
                task_id=child.task_id,
                parent_task_id=task.task_id,
                purpose=SpecialistPurpose.PRIMARY,
                route=RouteBinding(requested=route, effective=route, is_primary=True),
                budget=TaskBudget(),
            ),
            idempotency_key="child",
        )
        await work.scheduler.yield_to_children(claim, (child,))
        child_lease = await work.scheduler.claim_ready("worker-a", timedelta(seconds=30))
        assert child_lease is not None and child_lease.task_id == child.task_id
        await work.scheduler.finish(child_lease, successful=True)
        resumed = await work.scheduler.claim_ready("worker-a", timedelta(seconds=30))
        assert resumed is not None and resumed.task_id == task.task_id
        effect = await work.scheduler.admit_effect(resumed, uuid4())
        epoch = await work.scheduler.begin_candidate(persisted_run.id)
        with pytest.raises(SchedulingConflict, match="still draining"):
            await work.scheduler.close_candidate(persisted_run.id, epoch)
        await work.scheduler.finish(resumed, successful=True)
        # Provider effects drain independently from a task/client lease.
        with pytest.raises(SchedulingConflict, match="still draining"):
            await work.scheduler.close_candidate(persisted_run.id, epoch)
        await work.scheduler.settle_effect(effect, accepted=True)
        await work.scheduler.close_candidate(persisted_run.id, epoch)
