"""Adversarial PostgreSQL acceptance checks for scheduler coordination."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from forge.domain.run import RunSnapshot
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
from forge.domain.tool import ToolName
from forge.persistence.models.scheduling import SubscriptionScheduledTask
from forge.persistence.repositories.scheduling import SchedulingConflict
from forge.persistence.unit_of_work import PostgresUnitOfWork
from sqlalchemy import text, update


@pytest_asyncio.fixture(autouse=True)
async def _remove_disposable_subscription_rows(session_factory):
    """Leave migration-owned disposable tables empty for fail-closed downgrade."""
    yield
    async with session_factory() as session, session.begin():
        await session.execute(
            text(
                "TRUNCATE TABLE subscription_quota_observations, subscription_quota_admissions, subscription_quota_pools, "
                "subscription_scheduled_effects, subscription_decision_records, subscription_budget_reservations, "
                "subscription_budget_pools, subscription_operation_bindings, subscription_attempts, "
                "subscription_task_dependencies, subscription_tasks, subscription_envelopes, "
                "project_subscription_profiles, subscription_profile_versions CASCADE"
            )
        )


def _route(provider: str) -> RouteSpec:
    return RouteSpec(
        provider=provider,
        client=f"{provider}-client",
        model=f"{provider}-model",
        effort=ReasoningEffort.LOW,
        auth_mode=AuthMode.SUBSCRIPTION,
        billing_mode=BillingMode.ALLOWANCE_ONLY,
    )


async def _admit_run(
    work, run: RunSnapshot, routes: tuple[RouteSpec, ...], *, budget: TaskBudget | None = None, review_route: RouteSpec | None = None, worker_fallbacks: tuple[RouteSpec, ...] = ()
) -> UUID:
    purposes = (SpecialistPurpose.PRIMARY, SpecialistPurpose.ROUTINE_IMPLEMENTATION)
    if review_route is not None:
        purposes += (SpecialistPurpose.INDEPENDENT_REVIEW,)
        routes += (review_route,)
    profile = OperatorProfile(
        profile_id=uuid4(),
        version=1,
        preferences=tuple(
            RolePreference(
                purpose=purpose, preferred_route=route,
                fallback_routes=worker_fallbacks if purpose is SpecialistPurpose.ROUTINE_IMPLEMENTATION else (),
            )
            for purpose, route in zip(
                purposes,
                routes,
                strict=False,
            )
        ),
    )
    await work.subscription.store_profile(profile)
    await work.subscription.freeze_envelope(
        ExecutionEnvelope(
            run_id=run.id,
            profile_id=profile.profile_id,
            profile_version=1,
            safety_policy_version=1,
            allowed_fallbacks=((SpecialistPurpose.ROUTINE_IMPLEMENTATION, worker_fallbacks),)
            if worker_fallbacks else (),
            routes=tuple(
                (
                    purpose,
                    RouteBinding(
                        requested=route,
                        effective=route,
                        is_primary=purpose is SpecialistPurpose.PRIMARY,
                    ),
                )
                for purpose, route in zip(
                    purposes,
                    routes,
                    strict=False,
                )
            ),
        )
    )
    primary_id = uuid4()
    primary_route = routes[0]
    await work.subscription.create_task(
        LogicalTaskContract(
            run_id=run.id,
            task_id=primary_id,
            purpose=SpecialistPurpose.PRIMARY,
            route=RouteBinding(requested=primary_route, effective=primary_route, is_primary=True),
            budget=budget or TaskBudget(),
            owned_paths=("apps",),
        ),
        idempotency_key=f"primary-{primary_id}",
    )
    await work.scheduler.admit_run(run.id)
    return primary_id


async def _enqueue(
    work,
    run_id: UUID,
    *,
    provider: str,
    worktree: str,
    parent_id: UUID,
    paths: tuple[str, ...] = (),
    purpose: SpecialistPurpose = SpecialistPurpose.ROUTINE_IMPLEMENTATION,
) -> UUID:
    task_id = uuid4()
    route = _route(provider)
    binding = RouteBinding(requested=route, effective=route)
    await work.subscription.create_task(
        LogicalTaskContract(
            run_id=run_id,
            task_id=task_id,
            parent_task_id=parent_id,
            purpose=purpose,
            route=binding,
            budget=TaskBudget(),
            owned_paths=paths,
        ),
        idempotency_key=str(task_id),
    )
    await work.scheduler.enqueue(
        ScheduleTask(
            run_id=run_id,
            task_id=task_id,
            parent_task_id=parent_id,
            worktree_id=worktree,
            owned_paths=paths,
            max_repairs=3,
            read_only=purpose
            in {
                SpecialistPurpose.PLANNING,
                SpecialistPurpose.INDEPENDENT_REVIEW,
                SpecialistPurpose.SECURITY,
            },
        )
    )
    return task_id


async def _claim(session_factory, owner: str):
    async with PostgresUnitOfWork(session_factory) as work:
        lease = await work.scheduler.claim_ready(owner, timedelta(seconds=30))
        await work.commit()
        return lease


@pytest.mark.integration
@pytest.mark.parametrize("limit", ["global", "provider", "run"])
async def test_simultaneous_claims_never_exceed_each_capacity_dimension(
    session_factory, persisted_run, limit
) -> None:
    second = RunSnapshot(
        id=uuid4(),
        project_id=persisted_run.project_id,
        task_id=persisted_run.task_id,
        policy_version=persisted_run.policy_version,
    )
    limits = {
        "global": (1, 2, 2),
        "provider": (2, 2, 1),
        "run": (2, 1, 2),
    }[limit]
    async with PostgresUnitOfWork(session_factory) as work:
        await work.runs.create(second)
        await work.scheduler.configure_capacity(
            SchedulerCapacityPolicy(
                version=20, global_limit=limits[0], run_limit=limits[1], provider_limit=limits[2]
            )
        )
        first_parent = await _admit_run(work, persisted_run, (_route("same"),))
        second_parent = await _admit_run(work, second, (_route("same"),))
        await _enqueue(
            work, persisted_run.id, provider="same", worktree="tree-a", parent_id=first_parent
        )
        await _enqueue(
            work,
            persisted_run.id if limit == "run" else second.id,
            provider="same" if limit == "provider" else "other",
            worktree="tree-b",
            parent_id=first_parent if limit == "run" else second_parent,
        )
        await work.commit()

    claims = await asyncio.gather(
        _claim(session_factory, "worker-a"), _claim(session_factory, "worker-b")
    )
    assert sum(claim is not None for claim in claims) == 1, (
        f"simultaneous claims exceeded the configured {limit} capacity"
    )


@pytest.mark.integration
async def test_claim_skips_ineligible_run_and_progresses_another_run(
    session_factory, persisted_run
) -> None:
    second = RunSnapshot(
        id=uuid4(),
        project_id=persisted_run.project_id,
        task_id=persisted_run.task_id,
        policy_version=1,
    )
    async with PostgresUnitOfWork(session_factory) as work:
        await work.runs.create(second)
        await work.scheduler.configure_capacity(
            SchedulerCapacityPolicy(version=21, global_limit=2, run_limit=1, provider_limit=2)
        )
        first_parent = await _admit_run(work, persisted_run, (_route("p"),))
        second_parent = await _admit_run(work, second, (_route("p"),))
        first = await _enqueue(
            work, persisted_run.id, provider="p", worktree="a", parent_id=first_parent
        )
        await _enqueue(work, persisted_run.id, provider="p", worktree="a2", parent_id=first_parent)
        other = await _enqueue(work, second.id, provider="p", worktree="b", parent_id=second_parent)
        await work.commit()

    claim1 = await _claim(session_factory, "one")
    claim2 = await _claim(session_factory, "two")
    assert claim1 is not None and claim1.task_id == first
    assert claim2 is not None and claim2.task_id == other


@pytest.mark.integration
async def test_path_fences_overlap_only_within_the_same_managed_worktree(
    session_factory, persisted_run
) -> None:
    async with PostgresUnitOfWork(session_factory) as work:
        await work.scheduler.configure_capacity(
            SchedulerCapacityPolicy(version=22, global_limit=3, run_limit=3, provider_limit=3)
        )
        primary = await _admit_run(work, persisted_run, (_route("p"),))
        parent = await _enqueue(
            work,
            persisted_run.id,
            provider="p",
            worktree="tree-a",
            parent_id=primary,
            paths=("apps/core",),
        )
        await _enqueue(
            work,
            persisted_run.id,
            provider="p",
            worktree="tree-a",
            parent_id=primary,
            paths=("apps/core/file.py",),
        )
        independent = await _enqueue(
            work,
            persisted_run.id,
            provider="p",
            worktree="tree-b",
            parent_id=primary,
            paths=("apps/core/file.py",),
        )
        await work.commit()

    first = await _claim(session_factory, "one")
    second = await _claim(session_factory, "two")
    third = await _claim(session_factory, "three")
    assert first is not None and first.task_id == parent
    assert second is not None and second.task_id == independent
    assert third is None


@pytest.mark.integration
async def test_expired_effect_must_be_reconciled_before_task_can_be_retried(
    session_factory, persisted_run
) -> None:
    async with PostgresUnitOfWork(session_factory) as work:
        await work.scheduler.configure_capacity(SchedulerCapacityPolicy(version=23))
        primary = await _admit_run(work, persisted_run, (_route("p"),))
        task_id = await _enqueue(
            work, persisted_run.id, provider="p", worktree="tree", parent_id=primary
        )
        await work.commit()
    lease = await _claim(session_factory, "owner")
    assert lease is not None
    async with PostgresUnitOfWork(session_factory) as work:
        await work.scheduler.admit_effect(lease, uuid4())
        await work.session.execute(
            update(SubscriptionScheduledTask)
            .where(SubscriptionScheduledTask.task_id == task_id)
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await work.commit()
    assert await _claim(session_factory, "scanner") is None

    async with PostgresUnitOfWork(session_factory) as work:
        with pytest.raises(SchedulingConflict, match="effect|reconcil"):
            await work.scheduler.reconcile_expired(persisted_run.id, task_id, retry=True)


@pytest.mark.integration
async def test_stop_rejects_late_provider_effect_and_terminal_output(
    session_factory, persisted_run
) -> None:
    from forge.persistence.models.scheduling import SubscriptionScheduledEffect

    async with PostgresUnitOfWork(session_factory) as work:
        await work.scheduler.configure_capacity(SchedulerCapacityPolicy(version=24))
        primary = await _admit_run(work, persisted_run, (_route("p"),))
        task_id = await _enqueue(
            work, persisted_run.id, provider="p", worktree="tree", parent_id=primary
        )
        await work.commit()
    lease = await _claim(session_factory, "owner")
    assert lease is not None
    effect_id = uuid4()
    async with PostgresUnitOfWork(session_factory) as work:
        effect = await work.scheduler.admit_effect(lease, effect_id)
        await work.commit()
    async with PostgresUnitOfWork(session_factory) as work:
        await work.scheduler.request_stop(persisted_run.id, task_id, cancel=True)
        await work.commit()
    async with PostgresUnitOfWork(session_factory) as work:
        await work.scheduler.finish(lease, successful=True)
        await work.scheduler.settle_effect(effect, accepted=True)
        await work.commit()
    async with session_factory() as session:
        row = await session.get(SubscriptionScheduledEffect, effect_id)
        assert row is not None and row.state == "rejected"


@pytest.mark.integration
async def test_effect_admission_enforces_scope_epoch_barrier_and_worktree_exclusivity(
    session_factory, persisted_run
) -> None:
    async with PostgresUnitOfWork(session_factory) as work:
        await work.scheduler.configure_capacity(
            SchedulerCapacityPolicy(version=25, global_limit=2, run_limit=2, provider_limit=2)
        )
        primary = await _admit_run(work, persisted_run, (_route("p"),))
        first_id = await _enqueue(
            work,
            persisted_run.id,
            provider="p",
            worktree="tree",
            parent_id=primary,
            paths=("apps/one",),
        )
        second_id = await _enqueue(
            work,
            persisted_run.id,
            provider="p",
            worktree="tree",
            parent_id=primary,
            paths=("apps/two",),
        )
        await work.commit()
    first, second = await _claim(session_factory, "one"), await _claim(session_factory, "two")
    assert first is not None and first.task_id == first_id
    assert second is not None and second.task_id == second_id

    async with PostgresUnitOfWork(session_factory) as work:
        with pytest.raises(SchedulingConflict, match="ownership"):
            await work.scheduler.admit_effect(first, uuid4(), owned_paths=("apps/two",))
        with pytest.raises(SchedulingConflict, match="ownership"):
            await work.scheduler.admit_effect(first, uuid4(), owned_paths=("apps",))
        with pytest.raises(SchedulingConflict, match="exclusively"):
            await work.scheduler.admit_effect(first, uuid4(), whole_worktree_exclusive=True)
        epoch = await work.scheduler.begin_candidate(persisted_run.id)
        with pytest.raises(SchedulingConflict, match="epoch differs"):
            await work.scheduler.admit_effect(first, uuid4(), expected_candidate_epoch=epoch - 1)
        with pytest.raises(SchedulingConflict, match="barrier"):
            await work.scheduler.admit_effect(first, uuid4(), expected_candidate_epoch=epoch)


@pytest.mark.integration
async def test_missing_durable_lease_expiry_cannot_admit_effect(session_factory, persisted_run):
    async with PostgresUnitOfWork(session_factory) as work:
        primary = await _admit_run(work, persisted_run, (_route("p"),))
        task_id = await _enqueue(
            work, persisted_run.id, provider="p", worktree="tree", parent_id=primary
        )
        await work.commit()
    lease = await _claim(session_factory, "owner")
    assert lease is not None
    async with session_factory() as session, session.begin():
        await session.execute(
            update(SubscriptionScheduledTask)
            .where(SubscriptionScheduledTask.task_id == task_id)
            .values(lease_expires_at=None)
        )
    async with PostgresUnitOfWork(session_factory) as work:
        with pytest.raises(SchedulingConflict):
            await work.scheduler.admit_effect(lease, uuid4())


@pytest.mark.integration
@pytest.mark.parametrize("prior_state", ["admitted", "reconciling", "settled", "rejected"])
async def test_effect_cleanup_is_idempotent_without_reopening_terminal_evidence(
    session_factory, persisted_run, prior_state
) -> None:
    from dataclasses import replace

    from forge.persistence.models.scheduling import SubscriptionScheduledEffect

    async with PostgresUnitOfWork(session_factory) as work:
        primary = await _admit_run(work, persisted_run, (_route("p"),))
        await _enqueue(work, persisted_run.id, provider="p", worktree="tree", parent_id=primary)
        await work.commit()
    lease = await _claim(session_factory, "owner")
    assert lease is not None
    async with PostgresUnitOfWork(session_factory) as work:
        effect = await work.scheduler.admit_effect(lease, uuid4())
        await work.session.execute(
            update(SubscriptionScheduledEffect)
            .where(SubscriptionScheduledEffect.id == effect.effect_id)
            .values(state=prior_state)
        )
        await work.commit()
    for _ in range(2):
        async with PostgresUnitOfWork(session_factory) as work:
            await work.scheduler.reconcile_effect(effect)
            await work.commit()
    async with session_factory() as session:
        row = await session.get(SubscriptionScheduledEffect, effect.effect_id)
        assert row is not None
        assert row.state == ("reconciling" if prior_state == "admitted" else prior_state)
    async with PostgresUnitOfWork(session_factory) as work:
        with pytest.raises(SchedulingConflict):
            await work.scheduler.reconcile_effect(
                replace(effect, task_lease=replace(lease, generation=lease.generation + 1))
            )


@pytest.mark.integration
@pytest.mark.parametrize("expired", [False, True])
async def test_effect_settlement_requires_current_unexpired_lease(
    session_factory, persisted_run, expired
) -> None:
    from forge.persistence.models.scheduling import SubscriptionScheduledEffect

    async with PostgresUnitOfWork(session_factory) as work:
        primary = await _admit_run(work, persisted_run, (_route("p"),))
        task_id = await _enqueue(
            work,
            persisted_run.id,
            provider="p",
            worktree="tree",
            parent_id=primary,
            paths=("apps/one",),
        )
        await work.commit()
    lease = await _claim(session_factory, "owner")
    assert lease is not None
    effect_id = uuid4()
    async with PostgresUnitOfWork(session_factory) as work:
        effect = await work.scheduler.admit_effect(
            lease, effect_id, owned_paths=("apps/one/file.py",)
        )
        if expired:
            await work.session.execute(
                update(SubscriptionScheduledTask)
                .where(SubscriptionScheduledTask.task_id == task_id)
                .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
        await work.commit()
    async with PostgresUnitOfWork(session_factory) as work:
        await work.scheduler.settle_effect(effect, accepted=True)
        await work.commit()
    async with session_factory() as session:
        row = await session.get(SubscriptionScheduledEffect, effect_id)
        assert row is not None
        assert row.state == ("rejected" if expired else "settled")


@pytest.mark.integration
@pytest.mark.parametrize(
    "tool_name,arguments,denied",
    [
        (ToolName.REPOSITORY_READ_FILE, {"path": "apps/one/file.py"}, False),
        (ToolName.REPOSITORY_WRITE_FILE, {"path": "apps/one/file.py", "content": "x"}, False),
        (ToolName.REPOSITORY_WRITE_FILE, {"path": "apps/two/file.py", "content": "x"}, True),
        (
            ToolName.REPOSITORY_RENAME_FILE,
            {
                "source": "apps/one/file.py",
                "destination": "apps/two/file.py",
                "expected_digest": "a" * 64,
            },
            True,
        ),
    ],
)
@pytest.mark.parametrize(
    "completion_mode", ["normal", "stop", "expired", "concurrent", "receipt_failure"]
)
async def test_real_broker_composition_settles_and_replays_one_effect(
    session_factory, persisted_run, tool_name, arguments, denied, completion_mode, monkeypatch
) -> None:
    from forge.application.services.subscription_broker import BrokerDenied, SubscriptionToolBroker
    from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
    from forge.domain.subscription import BrokerAuthorizationBinding
    from forge.domain.tool import ToolCallStatus, ToolResult
    from forge.persistence.models.scheduling import SubscriptionScheduledEffect
    from test_subscription_usage import _reservation

    async with PostgresUnitOfWork(session_factory) as work:
        primary = await _admit_run(work, persisted_run, (_route("p"),))
        task_id = await _enqueue(
            work,
            persisted_run.id,
            provider="p",
            worktree="tree",
            parent_id=primary,
            paths=("apps/one",),
        )
        await work.commit()
    executor = SubscriptionDecisionExecutor(lambda: PostgresUnitOfWork(session_factory))
    admission = await executor.admit_next("owner", _reservation())
    assert admission is not None
    lease = admission.lease
    attempt_id = admission.attempt.attempt_id
    calls = []
    barrier = asyncio.Barrier(2)

    async def effect(operation_id, name, arguments):
        calls.append(operation_id)
        if completion_mode == "stop":
            async with PostgresUnitOfWork(session_factory) as work:
                await work.scheduler.request_stop(persisted_run.id, task_id, cancel=True)
                await work.commit()
        if completion_mode == "expired":
            async with PostgresUnitOfWork(session_factory) as work:
                await work.session.execute(
                    update(SubscriptionScheduledTask)
                    .where(SubscriptionScheduledTask.task_id == task_id)
                    .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
                )
                await work.commit()
        if completion_mode == "concurrent":
            await asyncio.wait_for(barrier.wait(), timeout=5)
        return ToolResult(tool_name=name, status=ToolCallStatus.SUCCEEDED)

    broker = SubscriptionToolBroker(
        lambda: PostgresUnitOfWork(session_factory),
        lease=lease,
        authority=BrokerAuthorizationBinding(
            run_id=persisted_run.id,
            task_id=task_id,
            attempt_id=attempt_id,
            worktree_id="tree",
            role=SpecialistPurpose.ROUTINE_IMPLEMENTATION,
            policy_version=1,
            permitted_tools=frozenset({tool_name}),
            broker_token="test-only",
        ),
        effect=effect,
    )
    if denied:
        with pytest.raises(BrokerDenied):
            await broker.invoke(
                token="test-only",
                provider_call_key="call",
                tool_name=tool_name,
                arguments=arguments,
            )
        assert calls == []
        return

    async def invoke():
        return await broker.invoke(
            token="test-only", provider_call_key="call", tool_name=tool_name, arguments=arguments
        )

    if completion_mode == "receipt_failure":
        from forge.persistence.models.subscription import SubscriptionOperationBinding
        from forge.persistence.repositories.subscription import PostgresSubscriptionRepository

        async def fail_receipt(*args, **kwargs):
            raise RuntimeError("injected receipt persistence failure")

        monkeypatch.setattr(
            PostgresSubscriptionRepository, "record_operation_receipt", fail_receipt
        )
        with pytest.raises(BrokerDenied):
            await invoke()
        async with session_factory() as session:
            row = await session.get(SubscriptionScheduledEffect, calls[0])
            assert row is not None and row.state == "admitted"
            from sqlalchemy import select

            receipt = await session.scalar(
                select(SubscriptionOperationBinding).where(
                    SubscriptionOperationBinding.durable_operation_id == calls[0]
                )
            )
            assert receipt is not None and receipt.receipt_payload is None
        return
    if completion_mode == "concurrent":
        first, second = await asyncio.gather(invoke(), invoke())
        assert first == second
    else:
        first = await invoke()
    if completion_mode in {"stop", "expired"}:
        assert first.accepted is False
        assert first.result["status"] == "denied"
        async with session_factory() as session:
            row = await session.get(SubscriptionScheduledEffect, first.operation_id)
            assert row is not None and row.state == "rejected"
            from forge.persistence.models.subscription import SubscriptionOperationBinding
            from sqlalchemy import select

            stored = await session.scalar(
                select(SubscriptionOperationBinding).where(
                    SubscriptionOperationBinding.durable_operation_id == first.operation_id
                )
            )
            assert stored is not None and stored.receipt_payload == {
                "accepted": False,
                "result": dict(first.result),
            }
        return
    replay = await broker.invoke(
        token="test-only",
        provider_call_key="call",
        tool_name=tool_name,
        arguments=arguments,
    )
    assert first == replay
    assert first.accepted
    assert calls == [first.operation_id] * (2 if completion_mode == "concurrent" else 1)
    async with session_factory() as session:
        row = await session.get(SubscriptionScheduledEffect, first.operation_id)
        assert row is not None and row.state == "settled"
