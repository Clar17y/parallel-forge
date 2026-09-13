"""Primary thinking does not reserve child paths; primary effects remain exclusive."""

import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest
from forge.application.ports.scheduling import SchedulingConflict
from forge.application.ports.subscription_gateway import (
    SubscriptionFailure,
    SubscriptionInvocationResult,
)
from forge.application.services.subscription_broker import SubscriptionToolBroker
from forge.application.services.subscription_decisions import SubscriptionDecisionApplication
from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
from forge.domain.scheduling import SchedulerCapacityPolicy, ScheduleTask
from forge.domain.subscription import (
    BrokerAuthorizationBinding,
    SpecialistPurpose,
    TaskBudget,
    ToolCallBinding,
    encode_subscription_record,
)
from forge.domain.tool import ToolCallStatus, ToolName, ToolResult
from forge.persistence.models.scheduling import (
    SubscriptionScheduledEffect,
    SubscriptionSchedulerRun,
)
from forge.persistence.models.subscription import (
    SubscriptionAttempt,
    SubscriptionOperationBinding,
    SubscriptionTask,
)
from forge.persistence.unit_of_work import PostgresUnitOfWork
from sqlalchemy import select
from test_scheduler_acceptance import (
    _admit_run,
    _claim,
    _enqueue,
    _remove_disposable_subscription_rows,  # noqa: F401
    _route,
)
from test_subscription_delegation_application import delegation_case
from test_subscription_usage import _known, _reservation


async def enqueue_primary(work, run_id, primary):
    await work.scheduler.enqueue(
        ScheduleTask(
            run_id=run_id, task_id=primary, worktree_id="one", owned_paths=("apps",), max_repairs=3
        )
    )


@pytest.mark.integration
async def test_completed_child_wakes_primary_while_sibling_keeps_working(session_factory, tmp_path):
    factory, parent, children, _ = await delegation_case(
        session_factory,
        tmp_path,
        lambda child, _: (child, replace(child, task_id=uuid4(), owned_paths=("apps/other",))),
        primary_budget=replace(TaskBudget(), max_provider_attempts=8),
        plan_scope=("apps",),
    )
    assert (
        await SubscriptionDecisionApplication(factory).apply_delegation(parent.attempt.attempt_id)
    ).accepted
    executor = SubscriptionDecisionExecutor(factory)
    first = await executor.admit_next("first-child", _reservation())
    second = await executor.admit_next("second-child", _reservation())
    assert first is not None and second is not None
    assert {first.task.task_id, second.task.task_id} == {child.task_id for child in children}
    await executor.settle(
        first,
        SubscriptionInvocationResult(
            attempt=first.attempt, failure=SubscriptionFailure.PROTOCOL, telemetry=_known()
        ),
    )
    resumed = await executor.admit_next("primary-resumes", _reservation())
    assert resumed is not None and resumed.task.task_id == parent.task.task_id
    async with factory() as work:
        with pytest.raises(SchedulingConflict, match="exclusively available"):
            await work.scheduler.admit_effect(resumed.lease, uuid4(), owned_paths=("apps/file.py",))


@pytest.mark.integration
@pytest.mark.parametrize("first", ["primary", "child"])
async def test_primary_and_child_can_coordinate_but_primary_cannot_write_over_child(
    session_factory, persisted_run, first
):
    async with PostgresUnitOfWork(session_factory) as work:
        primary = await _admit_run(work, persisted_run, (_route("p"), _route("p")))
        if first == "primary":
            await enqueue_primary(work, persisted_run.id, primary)
        else:
            await _enqueue(
                work,
                persisted_run.id,
                provider="p",
                worktree="one",
                parent_id=primary,
                paths=("apps/feature",),
            )
        await work.commit()
    first_lease = await _claim(session_factory, "first")
    assert first_lease is not None
    async with PostgresUnitOfWork(session_factory) as work:
        if first == "primary":
            await _enqueue(
                work,
                persisted_run.id,
                provider="p",
                worktree="one",
                parent_id=primary,
                paths=("apps/feature",),
            )
        else:
            await enqueue_primary(work, persisted_run.id, primary)
        await work.commit()
    second_lease = await _claim(session_factory, "second")
    assert second_lease is not None
    primary_lease = first_lease if first == "primary" else second_lease
    async with PostgresUnitOfWork(session_factory) as work:
        with pytest.raises(SchedulingConflict):
            await work.scheduler.admit_effect(
                primary_lease, uuid4(), owned_paths=("apps/feature/file.py",)
            )


async def primary_owner(session_factory, run):
    async with PostgresUnitOfWork(session_factory) as work:
        primary = await _admit_run(work, run, (_route("p"), _route("p")))
        await enqueue_primary(work, run.id, primary)
        await work.commit()
    lease = await _claim(session_factory, "primary")
    assert lease is not None and lease.task_id == primary
    return primary, lease


@pytest.mark.integration
@pytest.mark.parametrize(
    "legacy,state",
    [(False, "admitted"), (False, "reconciling"), (True, "admitted"), (True, "reconciling")],
)
async def test_primary_effect_fences_workers_and_replays_but_other_worktree_progresses(
    session_factory, persisted_run, legacy, state
):
    primary, lease = await primary_owner(session_factory, persisted_run)
    async with PostgresUnitOfWork(session_factory) as work:
        effect = await work.scheduler.admit_effect(lease, uuid4(), owned_paths=("apps/file.py",))
        row = await work.session.get(SubscriptionScheduledEffect, effect.effect_id)
        assert row.whole_worktree_exclusive
        if legacy:
            row.whole_worktree_exclusive = False
        row.state = state
        await _enqueue(
            work,
            persisted_run.id,
            provider="p",
            worktree="one",
            parent_id=primary,
            paths=("apps/other",),
        )
        second = replace(persisted_run, id=uuid4())
        await work.runs.create(second)
        other_primary = await _admit_run(work, second, (_route("p"), _route("p")))
        other = await _enqueue(
            work, second.id, provider="p", worktree="two", parent_id=other_primary, paths=("apps",)
        )
        await work.commit()
    other_lease = await _claim(session_factory, "other")
    assert other_lease is not None and other_lease.task_id == other
    assert await _claim(session_factory, "blocked-writer") is None
    async with PostgresUnitOfWork(session_factory) as work:
        replay = await work.scheduler.admit_effect(
            lease, effect.effect_id, owned_paths=("apps/file.py",)
        )
        assert replay == effect
        await work.commit()
    if legacy:
        async with PostgresUnitOfWork(session_factory) as work:
            with pytest.raises(SchedulingConflict, match="identity conflicts"):
                await work.scheduler.admit_effect(
                    lease, effect.effect_id, whole_worktree_exclusive=True
                )


@pytest.mark.integration
async def test_primary_reads_and_child_check_use_real_broker_bindings(
    session_factory, persisted_run
):
    factory = lambda: PostgresUnitOfWork(session_factory)
    async with factory() as work:
        primary = await _admit_run(work, persisted_run, (_route("p"), _route("p")))
        await enqueue_primary(work, persisted_run.id, primary)
        await work.commit()
    admission = await SubscriptionDecisionExecutor(factory).admit_next("primary", _reservation())
    assert admission is not None
    async with factory() as work:
        await _enqueue(
            work,
            persisted_run.id,
            provider="p",
            worktree="one",
            parent_id=primary,
            paths=("apps/feature",),
        )
        await work.commit()
    child = await _claim(session_factory, "child")
    assert child is not None
    calls, checks = [], []

    async def read(operation_id, name, arguments):
        calls.append(operation_id)
        if not checks:
            async with factory() as work:
                checks.append(
                    await work.scheduler.admit_effect(child, uuid4(), whole_worktree_exclusive=True)
                )
                await work.commit()
        return ToolResult(tool_name=name, status=ToolCallStatus.SUCCEEDED)

    broker = SubscriptionToolBroker(
        factory,
        lease=admission.lease,
        authority=BrokerAuthorizationBinding(
            run_id=persisted_run.id,
            task_id=primary,
            attempt_id=admission.attempt.attempt_id,
            worktree_id="one",
            role=SpecialistPurpose.PRIMARY,
            policy_version=1,
            permitted_tools=frozenset({ToolName.REPOSITORY_READ_FILE}),
            broker_token="test-only",
        ),
        effect=read,
        owned_paths=admission.task.owned_paths,
    )
    for key in ("first", "second", "first"):
        result = await broker.invoke(
            token="test-only",
            provider_call_key=key,
            tool_name=ToolName.REPOSITORY_READ_FILE,
            arguments={"path": "apps/feature/file.py"},
        )
        assert result.accepted
    assert len(calls) == 2 and len(checks) == 1
    async with factory() as work:
        for identity in calls:
            assert not (
                await work.session.get(SubscriptionScheduledEffect, identity)
            ).whole_worktree_exclusive


@pytest.mark.integration
async def test_primary_broker_write_fences_child_until_settlement_and_replays(
    session_factory, persisted_run
):
    factory = lambda: PostgresUnitOfWork(session_factory)
    async with factory() as work:
        primary = await _admit_run(work, persisted_run, (_route("p"), _route("p")))
        await enqueue_primary(work, persisted_run.id, primary)
        await work.commit()
    admission = await SubscriptionDecisionExecutor(factory).admit_next("primary", _reservation())
    assert admission is not None
    async with factory() as work:
        await _enqueue(
            work,
            persisted_run.id,
            provider="p",
            worktree="one",
            parent_id=primary,
            paths=("apps/feature",),
        )
        await work.commit()
    calls = []

    async def write(operation_id, name, arguments):
        calls.append(operation_id)
        assert await _claim(session_factory, "blocked-child") is None
        async with factory() as work:
            effect = await work.session.get(SubscriptionScheduledEffect, operation_id)
            assert effect.whole_worktree_exclusive
        return ToolResult(tool_name=name, status=ToolCallStatus.SUCCEEDED)

    broker = SubscriptionToolBroker(
        factory,
        lease=admission.lease,
        authority=BrokerAuthorizationBinding(
            run_id=persisted_run.id,
            task_id=primary,
            attempt_id=admission.attempt.attempt_id,
            worktree_id="one",
            role=SpecialistPurpose.PRIMARY,
            policy_version=1,
            permitted_tools=frozenset({ToolName.REPOSITORY_WRITE_FILE}),
            broker_token="test-only",
        ),
        effect=write,
        owned_paths=admission.task.owned_paths,
    )
    for _ in range(2):
        result = await broker.invoke(
            token="test-only",
            provider_call_key="write",
            tool_name=ToolName.REPOSITORY_WRITE_FILE,
            arguments={"path": "apps/file.py", "content": "value = 1\n"},
        )
        assert result.accepted
    assert len(calls) == 1
    assert await _claim(session_factory, "released-child") is not None


@pytest.mark.integration
async def test_primary_can_start_coordination_during_child_exclusive_effect(
    session_factory, persisted_run
):
    async with PostgresUnitOfWork(session_factory) as work:
        primary = await _admit_run(work, persisted_run, (_route("p"), _route("p")))
        await _enqueue(
            work,
            persisted_run.id,
            provider="p",
            worktree="one",
            parent_id=primary,
            paths=("apps/feature",),
        )
        await work.commit()
    child = await _claim(session_factory, "child")
    async with PostgresUnitOfWork(session_factory) as work:
        await work.scheduler.admit_effect(child, uuid4(), whole_worktree_exclusive=True)
        await enqueue_primary(work, persisted_run.id, primary)
        await work.commit()
    parent = await _claim(session_factory, "parent-coordinates")
    assert parent is not None and parent.task_id == primary


@pytest.mark.integration
@pytest.mark.parametrize("limit", ["global", "run", "provider"])
async def test_coordinator_does_not_bypass_capacity(session_factory, persisted_run, limit):
    async with PostgresUnitOfWork(session_factory) as work:
        limits = {"global_limit": 3, "run_limit": 3, "provider_limit": 3}
        limits[f"{limit}_limit"] = 1
        await work.scheduler.configure_capacity(SchedulerCapacityPolicy(version=2, **limits))
        await work.commit()
    primary, _ = await primary_owner(session_factory, persisted_run)
    async with PostgresUnitOfWork(session_factory) as work:
        await _enqueue(
            work,
            persisted_run.id,
            provider="p",
            worktree="one",
            parent_id=primary,
            paths=("apps/feature",),
        )
        await work.commit()
    assert await _claim(session_factory, "capacity-blocked") is None


@pytest.mark.integration
async def test_unknown_primary_contract_does_not_exempt_its_reservation(
    session_factory, persisted_run
):
    primary, _ = await primary_owner(session_factory, persisted_run)
    async with PostgresUnitOfWork(session_factory) as work:
        await _enqueue(
            work,
            persisted_run.id,
            provider="p",
            worktree="one",
            parent_id=primary,
            paths=("apps/feature",),
        )
        (await work.session.get(SubscriptionTask, primary)).payload = {"invalid": True}
        await work.commit()
    assert await _claim(session_factory, "blocked-by-unknown-owner") is None


@pytest.mark.integration
@pytest.mark.parametrize("closed", [False, True])
@pytest.mark.parametrize("change", ["none", "attempt", "key", "duplicate", "generation", "write"])
async def test_read_exemption_requires_exact_durable_binding(
    session_factory, persisted_run, change, closed
):
    factory = lambda: PostgresUnitOfWork(session_factory)
    async with factory() as work:
        primary = await _admit_run(work, persisted_run, (_route("p"), _route("p")))
        await enqueue_primary(work, persisted_run.id, primary)
        if closed:
            epoch = await work.scheduler.begin_candidate(persisted_run.id)
            await work.scheduler.close_candidate(persisted_run.id, epoch)
        await work.commit()
    admission = await SubscriptionDecisionExecutor(factory).admit_next("primary", _reservation())
    assert admission is not None
    binding = ToolCallBinding(
        attempt_id=admission.attempt.attempt_id,
        provider_call_key="read",
        durable_operation_id=uuid4(),
        tool_name=ToolName.REPOSITORY_READ_FILE,
        arguments_digest="a" * 64,
    )
    async with factory() as work:
        await work.subscription.bind_operation(binding, run_id=persisted_run.id, task_id=primary)
        if not closed:
            await _enqueue(
                work,
                persisted_run.id,
                provider="p",
                worktree="one",
                parent_id=primary,
                paths=("apps/feature",),
            )
        record = await work.session.scalar(
            select(SubscriptionOperationBinding).where(
                SubscriptionOperationBinding.durable_operation_id == binding.durable_operation_id
            )
        )
        if change == "attempt":
            record.payload = encode_subscription_record(replace(binding, attempt_id=uuid4()))
        elif change == "key":
            record.payload = encode_subscription_record(replace(binding, provider_call_key="wrong"))
        elif change == "write":
            record.payload = encode_subscription_record(
                replace(binding, tool_name=ToolName.REPOSITORY_WRITE_FILE)
            )
        elif change == "duplicate":
            await work.subscription.bind_operation(
                replace(binding, provider_call_key="alias"),
                run_id=persisted_run.id,
                task_id=primary,
            )
        elif change == "generation":
            (await work.session.get(SubscriptionAttempt, binding.attempt_id)).lease_generation += 1
        await work.commit()
    if not closed:
        assert await _claim(session_factory, "child") is not None
    async with factory() as work:
        if change == "none":
            effect = await work.scheduler.admit_effect(
                admission.lease, binding.durable_operation_id
            )
            assert not (
                await work.session.get(SubscriptionScheduledEffect, effect.effect_id)
            ).whole_worktree_exclusive
        else:
            with pytest.raises(SchedulingConflict, match="candidate barrier" if closed else "exclusively available"):
                await work.scheduler.admit_effect(admission.lease, binding.durable_operation_id)


@pytest.mark.integration
@pytest.mark.parametrize("iteration", range(3))
async def test_primary_effect_and_worker_claim_are_mutually_exclusive(
    session_factory, persisted_run, iteration
):
    primary, lease = await primary_owner(session_factory, persisted_run)
    async with PostgresUnitOfWork(session_factory) as work:
        await _enqueue(
            work,
            persisted_run.id,
            provider="p",
            worktree="one",
            parent_id=primary,
            paths=("apps/feature",),
        )
        await work.commit()

    async def mutate():
        async with PostgresUnitOfWork(session_factory) as work:
            try:
                await work.scheduler.admit_effect(lease, uuid4(), owned_paths=("apps/file.py",))
            except SchedulingConflict:
                return False
            await work.commit()
            return True

    admitted, child = await asyncio.wait_for(
        asyncio.gather(mutate(), _claim(session_factory, "worker")), 10
    )
    assert admitted != (child is not None)

@pytest.mark.integration
@pytest.mark.parametrize("state", ["closed", "draining"])
async def test_candidate_barrier_schedules_primary_without_reopening_writes(
    session_factory, persisted_run, state
):
    async with PostgresUnitOfWork(session_factory) as work:
        primary = await _admit_run(work, persisted_run, (_route("p"), _route("p")))
        await enqueue_primary(work, persisted_run.id, primary)
        await _enqueue(
            work, persisted_run.id, provider="p", worktree="one",
            parent_id=primary, paths=("apps/feature",),
        )
        epoch = await work.scheduler.begin_candidate(persisted_run.id)
        if state == "closed":
            await work.scheduler.close_candidate(persisted_run.id, epoch)
        await work.commit()
    lease = await _claim(session_factory, "candidate-primary")
    if state == "draining":
        assert lease is None
        return
    assert lease is not None and lease.task_id == primary
    assert await _claim(session_factory, "candidate-writer") is None
    async with PostgresUnitOfWork(session_factory) as work:
        row = await work.session.get(SubscriptionSchedulerRun, persisted_run.id)
        assert row.candidate_state == "closed" and row.candidate_epoch == epoch + 1
        with pytest.raises(SchedulingConflict, match="candidate barrier"):
            await work.scheduler.admit_effect(lease, uuid4(), owned_paths=("apps/file.py",))
