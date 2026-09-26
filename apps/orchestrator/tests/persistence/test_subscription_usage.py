"""Durable measured usage and atomic attempt budget admission."""

from uuid import uuid4

import pytest
from forge.domain.subscription import AttemptIdentity, AttemptTelemetry, RouteBinding, TaskBudget
from forge.persistence.repositories.subscription_budget import SubscriptionBudgetConflict
from forge.persistence.unit_of_work import PostgresUnitOfWork
from test_scheduler_acceptance import (  # noqa: F401
    _admit_run,
    _enqueue,
    _remove_disposable_subscription_rows,
    _route,
)


async def _case(session_factory, run):
    async with PostgresUnitOfWork(session_factory) as work:
        primary = await _admit_run(work, run, (_route("p"), _route("p")))
        task = await _enqueue(
            work, run.id, provider="p", worktree="budget-tree", parent_id=primary, paths=("apps",)
        )
        attempt = uuid4()
        await work.subscription.create_attempt(
            AttemptIdentity(run_id=run.id, task_id=task, attempt_id=attempt),
            route_payload=RouteBinding(requested=_route("p"), effective=_route("p")),
            idempotency_key=str(attempt),
        )
        await work.commit()
    return task, attempt


def _reservation():
    return TaskBudget(
        max_duration_seconds=10,
        max_tool_calls=8,
        max_named_checks=2,
        max_provider_attempts=1,
        max_repairs=0,
        max_input_tokens=100,
        max_output_tokens=40,
        max_cost_minor=20,
    )


@pytest.mark.integration
async def test_attempt_usage_persists_actual_consumption_and_replays_once(
    session_factory, persisted_run
):
    task, attempt = await _case(session_factory, persisted_run)
    async with PostgresUnitOfWork(session_factory) as work:
        await work.subscription_budget.reserve_attempt(
            persisted_run.id, task, attempt, _reservation(), idempotency_key="attempt"
        )
        await work.commit()
    telemetry = AttemptTelemetry(
        duration_ms=1250,
        tool_call_count=2,
        named_check_count=1,
        input_tokens=30,
        output_tokens=8,
        estimated_api_cost_minor=3,
    )
    async with PostgresUnitOfWork(session_factory) as work:
        receipt = await work.subscription_budget.settle_attempt(
            persisted_run.id, task, attempt, telemetry
        )
        assert receipt.charge.charged.duration_ms == 1250
        await work.commit()
    async with PostgresUnitOfWork(session_factory) as work:
        assert (
            await work.subscription_budget.settle_attempt(
                persisted_run.id, task, attempt, telemetry
            )
            == receipt
        )
        usage = await work.subscription_budget.usage(persisted_run.id, task)
        assert usage.consumed.provider_attempts == 1
        assert usage.consumed.input_tokens == 30
        assert usage.outstanding.provider_attempts == 0
        with pytest.raises(SubscriptionBudgetConflict):
            await work.subscription_budget.settle_attempt(persisted_run.id, task, attempt, None)


async def _next_attempt(work, run_id, task_id, number):
    attempt = uuid4()
    await work.subscription.create_attempt(
        AttemptIdentity(run_id=run_id, task_id=task_id, attempt_id=attempt, attempt_number=number),
        route_payload=RouteBinding(requested=_route("p"), effective=_route("p")),
        idempotency_key=str(attempt),
    )
    return attempt


def _known(**changes):
    from dataclasses import replace

    from forge.domain.subscription import QuotaStatus

    return replace(
        AttemptTelemetry(
            duration_ms=1,
            tool_call_count=0,
            named_check_count=0,
            input_tokens=0,
            output_tokens=0,
            estimated_api_cost_minor=0,
            quota_status=QuotaStatus.OK,
        ),
        **changes,
    )


@pytest.mark.integration
async def test_unknown_attempt_retains_ceiling_and_blocks_uncertainty_retry(
    session_factory, persisted_run
):
    task, attempt = await _case(session_factory, persisted_run)
    async with PostgresUnitOfWork(session_factory) as work:
        await work.subscription_budget.reserve_attempt(
            persisted_run.id, task, attempt, _reservation(), idempotency_key="unknown"
        )
        receipt = await work.subscription_budget.settle_attempt(
            persisted_run.id, task, attempt, None
        )
        assert receipt.charge.observed.input_tokens is None
        assert receipt.charge.charged.input_tokens == 100
        assert receipt.charge.charged.provider_attempts == 1
        next_attempt = await _next_attempt(work, persisted_run.id, task, 2)
        await work.commit()
    async with PostgresUnitOfWork(session_factory) as work:
        usage = await work.subscription_budget.usage(persisted_run.id)
        assert usage.uncertain_attempts == 1
        assert usage.consumed.input_tokens == 100
        with pytest.raises(SubscriptionBudgetConflict, match="exhausted"):
            await work.subscription_budget.reserve_attempt(
                persisted_run.id, task, next_attempt, _reservation(), idempotency_key="retry"
            )


@pytest.mark.integration
async def test_measured_release_makes_unused_reservation_available(session_factory, persisted_run):
    from dataclasses import replace

    task, attempt = await _case(session_factory, persisted_run)
    large = replace(_reservation(), max_duration_seconds=1000)
    async with PostgresUnitOfWork(session_factory) as work:
        await work.subscription_budget.reserve_attempt(
            persisted_run.id, task, attempt, large, idempotency_key="one"
        )
        next_attempt = await _next_attempt(work, persisted_run.id, task, 2)
        await work.commit()
    async with PostgresUnitOfWork(session_factory) as work:
        with pytest.raises(SubscriptionBudgetConflict, match="exhausted"):
            await work.subscription_budget.reserve_attempt(
                persisted_run.id, task, next_attempt, large, idempotency_key="two"
            )
    async with PostgresUnitOfWork(session_factory) as work:
        await work.subscription_budget.settle_attempt(persisted_run.id, task, attempt, _known())
        await work.subscription_budget.reserve_attempt(
            persisted_run.id, task, next_attempt, large, idempotency_key="two"
        )
        await work.commit()
    async with PostgresUnitOfWork(session_factory) as work:
        usage = await work.subscription_budget.usage(persisted_run.id)
        assert usage.consumed.duration_ms == 1
        assert usage.outstanding.duration_ms == 1_000_000
        assert usage.consumed.provider_attempts + usage.outstanding.provider_attempts == 2


@pytest.mark.integration
async def test_concurrent_siblings_cannot_overreserve_run_budget(session_factory, persisted_run):
    import asyncio
    from dataclasses import replace

    from forge.persistence.models.subscription import SubscriptionTask

    task, attempt = await _case(session_factory, persisted_run)
    async with PostgresUnitOfWork(session_factory) as work:
        parent = (await work.session.get(SubscriptionTask, task)).parent_task_id
        sibling = await _enqueue(
            work,
            persisted_run.id,
            provider="p",
            worktree="budget-tree",
            parent_id=parent,
            paths=("apps/other",),
        )
        sibling_attempt = await _next_attempt(work, persisted_run.id, sibling, 1)
        await work.commit()

    async def admit(task_id, attempt_id):
        async with PostgresUnitOfWork(session_factory) as work:
            try:
                await work.subscription_budget.reserve_attempt(
                    persisted_run.id,
                    task_id,
                    attempt_id,
                    replace(_reservation(), max_duration_seconds=1000),
                    idempotency_key=str(attempt_id),
                )
            except SubscriptionBudgetConflict:
                return False
            await work.commit()
            return True

    results = await asyncio.gather(admit(task, attempt), admit(sibling, sibling_attempt))
    assert sorted(results) == [False, True]
    async with PostgresUnitOfWork(session_factory) as work:
        assert (
            await work.subscription_budget.usage(persisted_run.id)
        ).outstanding.provider_attempts == 1


@pytest.mark.integration
async def test_measured_overage_is_retained_and_prevents_further_admission(
    session_factory, persisted_run
):
    task, attempt = await _case(session_factory, persisted_run)
    async with PostgresUnitOfWork(session_factory) as work:
        await work.subscription_budget.reserve_attempt(
            persisted_run.id, task, attempt, _reservation(), idempotency_key="one"
        )
        receipt = await work.subscription_budget.settle_attempt(
            persisted_run.id, task, attempt, _known(duration_ms=2_000_000)
        )
        assert receipt.charge.charged.duration_ms == 2_000_000
        assert "duration_ms" in receipt.charge.exceeded_fields
        next_attempt = await _next_attempt(work, persisted_run.id, task, 2)
        await work.commit()
    async with PostgresUnitOfWork(session_factory) as work:
        with pytest.raises(SubscriptionBudgetConflict):
            await work.subscription_budget.reserve_attempt(
                persisted_run.id, task, next_attempt, _reservation(), idempotency_key="two"
            )
        assert (
            await work.subscription_budget.usage(persisted_run.id)
        ).consumed.duration_ms == 2_000_000


@pytest.mark.integration
async def test_reservation_and_settlement_rollbacks_do_not_double_debit(
    session_factory, persisted_run
):
    task, attempt = await _case(session_factory, persisted_run)
    async with PostgresUnitOfWork(session_factory) as work:
        await work.subscription_budget.reserve_attempt(
            persisted_run.id, task, attempt, _reservation(), idempotency_key="one"
        )
        await work.rollback()
    async with PostgresUnitOfWork(session_factory) as work:
        assert (
            await work.subscription_budget.usage(persisted_run.id)
        ).outstanding.provider_attempts == 0
        await work.subscription_budget.reserve_attempt(
            persisted_run.id, task, attempt, _reservation(), idempotency_key="one"
        )
        await work.commit()
    async with PostgresUnitOfWork(session_factory) as work:
        await work.subscription_budget.settle_attempt(persisted_run.id, task, attempt, _known())
        await work.rollback()
    async with PostgresUnitOfWork(session_factory) as work:
        usage = await work.subscription_budget.usage(persisted_run.id)
        assert usage.outstanding.provider_attempts == 1 and usage.consumed.provider_attempts == 0
        await work.subscription_budget.settle_attempt(persisted_run.id, task, attempt, _known())
        await work.commit()
    async with PostgresUnitOfWork(session_factory) as work:
        assert (
            await work.subscription_budget.usage(persisted_run.id)
        ).consumed.provider_attempts == 1


@pytest.mark.integration
async def test_foreign_attempt_and_conflicting_reservation_replays_are_rejected(
    session_factory, persisted_run
):
    from dataclasses import replace

    task, attempt = await _case(session_factory, persisted_run)
    async with PostgresUnitOfWork(session_factory) as work:
        with pytest.raises(SubscriptionBudgetConflict):
            await work.subscription_budget.reserve_attempt(
                persisted_run.id, uuid4(), attempt, _reservation(), idempotency_key="one"
            )
        await work.subscription_budget.reserve_attempt(
            persisted_run.id, task, attempt, _reservation(), idempotency_key="one"
        )
        await work.commit()
    async with PostgresUnitOfWork(session_factory) as work:
        await work.subscription_budget.reserve_attempt(
            persisted_run.id, task, attempt, _reservation(), idempotency_key="one"
        )
        with pytest.raises(SubscriptionBudgetConflict):
            await work.subscription_budget.reserve_attempt(
                persisted_run.id,
                task,
                attempt,
                replace(_reservation(), max_tool_calls=7),
                idempotency_key="one",
            )
        assert (
            await work.subscription_budget.usage(persisted_run.id)
        ).outstanding.provider_attempts == 1


@pytest.mark.integration
async def test_superseded_unreserved_attempt_cannot_gain_budget(session_factory, persisted_run):
    task, attempt = await _case(session_factory, persisted_run)
    async with PostgresUnitOfWork(session_factory) as work:
        await _next_attempt(work, persisted_run.id, task, 2)
        await work.commit()
    async with PostgresUnitOfWork(session_factory) as work:
        with pytest.raises(SubscriptionBudgetConflict):
            await work.subscription_budget.reserve_attempt(
                persisted_run.id, task, attempt, _reservation(), idempotency_key="old"
            )
        assert (
            await work.subscription_budget.usage(persisted_run.id)
        ).outstanding.provider_attempts == 0


@pytest.mark.integration
async def test_unbounded_unknowns_remain_null_in_durable_usage(session_factory, persisted_run):
    from dataclasses import replace

    task, attempt = await _case(session_factory, persisted_run)
    async with PostgresUnitOfWork(session_factory) as work:
        await work.subscription_budget.reserve_attempt(
            persisted_run.id,
            task,
            attempt,
            replace(
                _reservation(), max_input_tokens=None, max_output_tokens=None, max_cost_minor=None
            ),
            idempotency_key="unknown",
        )
        receipt = await work.subscription_budget.settle_attempt(
            persisted_run.id, task, attempt, None
        )
        assert receipt.charge.charged.input_tokens is None
        assert receipt.charge.charged.cost_minor is None
        await work.commit()
    async with PostgresUnitOfWork(session_factory) as work:
        usage = await work.subscription_budget.usage(persisted_run.id)
        assert usage.consumed.input_tokens is None
        assert usage.consumed.output_tokens is None
        assert usage.consumed.cost_minor is None
        assert usage.consumed.provider_attempts == 1


@pytest.mark.integration
async def test_known_zero_and_unknown_fields_are_charged_distinctly(session_factory, persisted_run):
    task, attempt = await _case(session_factory, persisted_run)
    async with PostgresUnitOfWork(session_factory) as work:
        await work.subscription_budget.reserve_attempt(
            persisted_run.id, task, attempt, _reservation(), idempotency_key="partial"
        )
        receipt = await work.subscription_budget.settle_attempt(
            persisted_run.id, task, attempt, _known(input_tokens=None)
        )
        assert receipt.charge.observed.input_tokens is None
        assert receipt.charge.charged.input_tokens == 100
        assert receipt.charge.charged.output_tokens == 0
        assert receipt.charge.charged.cost_minor == 0
        await work.commit()


@pytest.mark.integration
async def test_usage_migration_preserves_admitted_accounting_on_downgrade(
    session_factory, persisted_run
):
    import importlib.util
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    task, attempt = await _case(session_factory, persisted_run)
    async with PostgresUnitOfWork(session_factory) as work:
        await work.subscription_budget.reserve_attempt(
            persisted_run.id, task, attempt, _reservation(), idempotency_key="one"
        )
        await work.commit()
    path = (
        Path(__file__).resolve().parents[2]
        / "migrations/versions/20260910_0012_subscription_usage.py"
    )
    spec = importlib.util.spec_from_file_location("usage_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def downgrade(connection):
        with Operations.context(MigrationContext.configure(connection)):
            module.downgrade()

    async with session_factory() as session, session.begin():
        connection = await session.connection()
        with pytest.raises(RuntimeError, match="admitted subscription usage"):
            await connection.run_sync(downgrade)
    async with PostgresUnitOfWork(session_factory) as work:
        assert (
            await work.subscription_budget.usage(persisted_run.id)
        ).outstanding.provider_attempts == 1


@pytest.mark.integration
async def test_cancelled_run_cannot_admit_new_usage_but_can_settle_existing(
    session_factory, persisted_run
):
    from forge.persistence.models.run import Run
    from sqlalchemy import update

    task, attempt = await _case(session_factory, persisted_run)
    async with PostgresUnitOfWork(session_factory) as work:
        await work.subscription_budget.reserve_attempt(
            persisted_run.id, task, attempt, _reservation(), idempotency_key="one"
        )
        next_attempt = await _next_attempt(work, persisted_run.id, task, 2)
        await work.session.execute(
            update(Run).where(Run.id == persisted_run.id).values(state="CANCELLED")
        )
        await work.commit()
    async with PostgresUnitOfWork(session_factory) as work:
        with pytest.raises(SubscriptionBudgetConflict):
            await work.subscription_budget.reserve_attempt(
                persisted_run.id, task, next_attempt, _reservation(), idempotency_key="two"
            )
        await work.subscription_budget.settle_attempt(persisted_run.id, task, attempt, _known())
        await work.commit()
    async with PostgresUnitOfWork(session_factory) as work:
        assert (
            await work.subscription_budget.usage(persisted_run.id)
        ).consumed.provider_attempts == 1


@pytest.mark.integration
async def test_other_run_progresses_while_first_run_holds_admission_lock(
    session_factory, persisted_run
):
    import asyncio

    from forge.domain.run import RunSnapshot

    second_run = RunSnapshot(
        id=uuid4(),
        project_id=persisted_run.project_id,
        task_id=persisted_run.task_id,
        policy_version=persisted_run.policy_version,
    )
    async with PostgresUnitOfWork(session_factory) as work:
        await work.runs.create(second_run)
        await work.commit()
    first_task, first_attempt = await _case(session_factory, persisted_run)
    second_task, second_attempt = await _case(session_factory, second_run)

    async def second_admission():
        async with PostgresUnitOfWork(session_factory) as work:
            await work.subscription_budget.reserve_attempt(
                second_run.id, second_task, second_attempt, _reservation(), idempotency_key="second"
            )
            await work.commit()

    async with PostgresUnitOfWork(session_factory) as work:
        await work.subscription_budget.reserve_attempt(
            persisted_run.id, first_task, first_attempt, _reservation(), idempotency_key="first"
        )
        await asyncio.wait_for(second_admission(), timeout=5)
        await work.commit()
    async with PostgresUnitOfWork(session_factory) as work:
        assert (
            await work.subscription_budget.usage(persisted_run.id)
        ).outstanding.provider_attempts == 1
        assert (
            await work.subscription_budget.usage(second_run.id)
        ).outstanding.provider_attempts == 1


@pytest.mark.integration
async def test_completed_attempt_count_survives_retry_and_exhaustion(
    session_factory, persisted_run
):
    task, attempt = await _case(session_factory, persisted_run)
    for number in range(1, 4):
        async with PostgresUnitOfWork(session_factory) as work:
            if number > 1:
                attempt = await _next_attempt(work, persisted_run.id, task, number)
            await work.subscription_budget.reserve_attempt(
                persisted_run.id, task, attempt, _reservation(), idempotency_key=str(number)
            )
            await work.subscription_budget.settle_attempt(persisted_run.id, task, attempt, _known())
            await work.commit()
    async with PostgresUnitOfWork(session_factory) as work:
        fourth = await _next_attempt(work, persisted_run.id, task, 4)
        with pytest.raises(SubscriptionBudgetConflict):
            await work.subscription_budget.reserve_attempt(
                persisted_run.id, task, fourth, _reservation(), idempotency_key="four"
            )
        assert (
            await work.subscription_budget.usage(persisted_run.id)
        ).consumed.provider_attempts == 3


@pytest.mark.integration
@pytest.mark.parametrize("task_scope", [False, True])
async def test_existing_frozen_budget_pool_ceiling_is_enforced(
    session_factory, persisted_run, task_scope
):
    task, attempt = await _case(session_factory, persisted_run)
    async with PostgresUnitOfWork(session_factory) as work:
        await work.subscription.initialize_budget(
            persisted_run.id, task if task_scope else None, TaskBudget(max_tool_calls=1)
        )
        await work.commit()
    async with PostgresUnitOfWork(session_factory) as work:
        with pytest.raises(SubscriptionBudgetConflict):
            await work.subscription_budget.reserve_attempt(
                persisted_run.id, task, attempt, _reservation(), idempotency_key="one"
            )
        assert (
            await work.subscription_budget.usage(persisted_run.id)
        ).outstanding.provider_attempts == 0


@pytest.mark.integration
async def test_explicit_run_pool_covers_siblings_separately_from_primary_task_budget(
    session_factory, persisted_run
):
    from forge.persistence.models.subscription import SubscriptionTask

    task, attempt = await _case(session_factory, persisted_run)
    async with PostgresUnitOfWork(session_factory) as work:
        parent = (await work.session.get(SubscriptionTask, task)).parent_task_id
        await work.subscription.initialize_budget(
            persisted_run.id, None, TaskBudget(max_provider_attempts=4)
        )
        tasks = [(task, attempt)]
        for index in range(3):
            child = await _enqueue(
                work,
                persisted_run.id,
                provider="p",
                worktree="budget-tree",
                parent_id=parent,
                paths=(f"apps/child{index}",),
            )
            tasks.append((child, await _next_attempt(work, persisted_run.id, child, 1)))
        for child, child_attempt in tasks:
            await work.subscription_budget.reserve_attempt(
                persisted_run.id,
                child,
                child_attempt,
                _reservation(),
                idempotency_key=str(child_attempt),
            )
        await work.commit()
    async with PostgresUnitOfWork(session_factory) as work:
        assert (
            await work.subscription_budget.usage(persisted_run.id)
        ).outstanding.provider_attempts == 4
        for child, _ in tasks:
            assert (
                await work.subscription_budget.usage(persisted_run.id, child)
            ).outstanding.provider_attempts == 1
