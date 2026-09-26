"""Only constructible frozen routes reach atomic attempt admission."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
from forge.domain.subscription import (
    TaskBudget,
    UnknownTelemetryPolicy,
    decode_subscription_record,
    encode_subscription_record,
)
from forge.persistence.models.scheduling import SubscriptionScheduledTask
from forge.persistence.models.subscription import SubscriptionAttempt, SubscriptionTask
from sqlalchemy import func, select
from test_scheduler_acceptance import (  # noqa: F401
    _admit_run,
    _enqueue,
    _remove_disposable_subscription_rows,
    _route,
)
from test_subscription_quota import _factory, _seed
from test_subscription_usage import _reservation


@pytest.mark.integration
async def test_unregistered_route_does_not_starve_other_run_or_spend_budget(
    session_factory, persisted_run
):
    factory = _factory(session_factory, [datetime.now(UTC)])
    runs, tasks = await _seed(factory, persisted_run, ("missing", "ready"))
    admitted = await SubscriptionDecisionExecutor(factory).admit_next(
        "configured-worker", _reservation(), eligible_routes=frozenset({_route("ready")})
    )
    assert admitted is not None and admitted.task.task_id == tasks[1]
    async with factory() as work:
        skipped = await work.session.get(SubscriptionScheduledTask, tasks[0])
        assert skipped.state == "queued" and skipped.lease_owner is None
        usage = await work.subscription_budget.usage(runs[0].id)
        assert usage.consumed.provider_attempts == usage.outstanding.provider_attempts == 0
        assert usage.consumed.repairs == usage.outstanding.repairs == 0
        assert await work.scheduler._active_count() == 1
        assert await work.session.scalar(select(func.count()).select_from(SubscriptionAttempt)) == 1


@pytest.mark.integration
async def test_concurrent_workers_share_remaining_run_duration_atomically(
    session_factory, persisted_run
):
    import asyncio

    from forge.domain.scheduling import SchedulerCapacityPolicy

    factory = _factory(session_factory, [datetime.now(UTC)])
    async with factory() as work:
        await work.scheduler.configure_capacity(
            SchedulerCapacityPolicy(version=1, global_limit=8, run_limit=3, provider_limit=8)
        )
        primary = await _admit_run(
            work,
            persisted_run,
            (_route("ready"), _route("ready")),
            budget=TaskBudget(max_duration_seconds=15),
        )
        for path in ("apps/a", "apps/b", "apps/c"):
            await _enqueue(
                work,
                persisted_run.id,
                provider="ready",
                worktree="shared-tree",
                parent_id=primary,
                paths=(path,),
            )
        await work.commit()
    admissions = await asyncio.gather(
        *(
            SubscriptionDecisionExecutor(factory).admit_next(owner, _reservation())
            for owner in ("worker-a", "worker-b", "worker-c")
        )
    )
    assert sum(value is not None for value in admissions) == 2
    async with factory() as work:
        usage = await work.subscription_budget.usage(persisted_run.id)
        assert usage.outstanding.duration_ms == 15_000
        assert usage.outstanding.provider_attempts == 2
        assert await work.scheduler._active_count() == 2
        budgets = [
            await work.subscription_budget.reserved_budget(
                value.task.run_id, value.task.task_id, value.attempt.attempt_id
            )
            for value in admissions
            if value is not None
        ]
        assert sorted(value.max_duration_seconds for value in budgets) == [5, 10]


@pytest.mark.integration
@pytest.mark.parametrize("registered", ["approved", "unapproved", "none"])
async def test_missing_route_uses_only_exact_approved_specialist_fallback(
    session_factory, persisted_run, registered
):
    factory = _factory(session_factory, [datetime.now(UTC)])
    _, tasks = await _seed(factory, persisted_run, ("missing",), fallbacks=(_route("approved"),))
    routes = frozenset() if registered == "none" else frozenset({_route(registered)})
    admitted = await SubscriptionDecisionExecutor(factory).admit_next(
        "configured-worker", _reservation(), eligible_routes=routes
    )
    if registered != "approved":
        assert admitted is None
        async with factory() as work:
            assert (
                await work.session.scalar(select(func.count()).select_from(SubscriptionAttempt))
                == 0
            )
        return
    assert admitted is not None and admitted.task.task_id == tasks[0]
    assert admitted.task.route.requested == _route("missing")
    assert admitted.task.route.effective == _route("approved")
    mapping = admitted.task.route.mapping_applied
    assert mapping is not None and "unavailable" in mapping.reason.lower()
    assert "exhaustion" not in mapping.reason.lower()
    assert admitted.envelope.route_for(admitted.task.purpose).effective == _route("missing")


async def test_worker_without_registered_routes_does_not_open_admission_transaction():
    def unavailable():
        raise AssertionError("unconfigured production worker touched PostgreSQL admission")

    assert (
        await SubscriptionDecisionExecutor(unavailable).admit_next(
            "unconfigured-worker", _reservation(), eligible_routes=frozenset()
        )
        is None
    )


@pytest.mark.integration
async def test_production_reservation_fits_frozen_task_limits_without_expanding_them(
    session_factory, persisted_run
):
    factory = _factory(session_factory, [datetime.now(UTC)])
    _, tasks = await _seed(factory, persisted_run, ("ready",))
    async with factory() as work:
        row = await work.session.get(SubscriptionTask, tasks[0])
        contract = decode_subscription_record(row.payload)
        budget = replace(
            contract.budget,
            max_duration_seconds=4,
            max_tool_calls=3,
            max_named_checks=0,
            max_input_tokens=12,
            max_output_tokens=5,
        )
        row.payload = encode_subscription_record(replace(contract, budget=budget))
        await work.commit()
    admitted = await SubscriptionDecisionExecutor(factory).admit_next(
        "bounded-worker", _reservation(), eligible_routes=frozenset({_route("ready")})
    )
    assert admitted is not None
    async with factory() as work:
        reserved = await work.subscription_budget.reserved_budget(
            admitted.task.run_id, admitted.task.task_id, admitted.attempt.attempt_id
        )
        assert reserved.max_duration_seconds == 4
        assert reserved.max_tool_calls == 3 and reserved.max_named_checks == 0
        assert reserved.max_input_tokens == 12 and reserved.max_output_tokens == 5
        assert reserved.max_provider_attempts == 1 and reserved.max_repairs == 0
        assert (await work.subscription.get_task(admitted.task.run_id, tasks[0])).budget == budget


@pytest.mark.integration
@pytest.mark.parametrize("scope", ["task", "run"])
async def test_reservation_preserves_stricter_uncertainty_authority(
    session_factory, persisted_run, scope
):
    factory = _factory(session_factory, [datetime.now(UTC)])
    _, tasks = await _seed(factory, persisted_run, ("ready",))
    strict = UnknownTelemetryPolicy(
        allow_unknown_tokens=False,
        allow_unknown_cost=False,
        allow_unknown_quota=False,
        max_uncertain_attempts=2,
    )
    permissive = UnknownTelemetryPolicy(max_uncertain_attempts=8)
    async with factory() as work:
        child = await work.session.get(SubscriptionTask, tasks[0])
        parent = await work.session.get(SubscriptionTask, child.parent_task_id)
        for row, name in ((child, "task"), (parent, "run")):
            contract = decode_subscription_record(row.payload)
            row.payload = encode_subscription_record(
                replace(
                    contract,
                    budget=replace(
                        contract.budget,
                        unknown_telemetry_policy=strict if name == scope else permissive,
                    ),
                )
            )
        await work.commit()
    admitted = await SubscriptionDecisionExecutor(factory).admit_next(
        "bounded-worker", replace(_reservation(), unknown_telemetry_policy=permissive)
    )
    assert admitted is not None
    async with factory() as work:
        reserved = await work.subscription_budget.reserved_budget(
            admitted.task.run_id, admitted.task.task_id, admitted.attempt.attempt_id
        )
        assert reserved.unknown_telemetry_policy == strict
        stored = await work.subscription.get_task(admitted.task.run_id, tasks[0])
        assert stored == admitted.task


@pytest.mark.integration
@pytest.mark.parametrize("empty", ["duration", "attempts"])
async def test_task_without_admissible_budget_does_not_starve_another_run(
    session_factory, persisted_run, empty
):
    factory = _factory(session_factory, [datetime.now(UTC)])
    runs, tasks = await _seed(factory, persisted_run, ("ready", "ready"))
    async with factory() as work:
        row = await work.session.get(SubscriptionTask, tasks[0])
        contract = decode_subscription_record(row.payload)
        changes = (
            {"max_duration_seconds": 0} if empty == "duration" else {"max_provider_attempts": 0}
        )
        row.payload = encode_subscription_record(
            replace(contract, budget=replace(contract.budget, **changes))
        )
        await work.commit()
    admitted = await SubscriptionDecisionExecutor(factory).admit_next(
        "bounded-worker", _reservation()
    )
    assert admitted is not None and admitted.task.task_id == tasks[1]
    async with factory() as work:
        usage = await work.subscription_budget.usage(runs[0].id)
        assert usage.consumed.provider_attempts == usage.outstanding.provider_attempts == 0
