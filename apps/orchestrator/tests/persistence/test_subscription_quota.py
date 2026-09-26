"""Durable account quota admission with controlled time and no provider IO."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
from forge.domain.provider_quota import QuotaExhaustion
from forge.domain.subscription_quota import QuotaPoolKey
from forge.persistence.models.subscription import SubscriptionAttempt
from forge.persistence.unit_of_work import PostgresUnitOfWork
from sqlalchemy import func, select
from test_scheduler_acceptance import (  # noqa: F401
    _admit_run,
    _enqueue,
    _remove_disposable_subscription_rows,
    _route,
)
from test_subscription_usage import _reservation


@pytest.mark.integration
async def test_block_survives_new_worker_without_charging_or_holding_capacity(
    session_factory, persisted_run
):
    now = datetime.now(UTC)
    key = QuotaPoolKey("p", "local", "subscription-allowance_only")
    async with PostgresUnitOfWork(session_factory) as work:
        primary = await _admit_run(work, persisted_run, (_route("p"), _route("p")))
        task_id = await _enqueue(
            work,
            persisted_run.id,
            provider="p",
            worktree="quota-tree",
            parent_id=primary,
            paths=("apps",),
        )
        await work.quota.report_exhaustion(
            key,
            QuotaExhaustion(now, "operator_report", now + timedelta(hours=2)),
            actor_id=uuid4(),
            idempotency_key="first-report",
        )
        await work.commit()
    for owner in ("worker-before-restart", "worker-after-restart"):
        executor = SubscriptionDecisionExecutor(lambda: PostgresUnitOfWork(session_factory))
        assert await executor.admit_next(owner, _reservation()) is None
    async with PostgresUnitOfWork(session_factory) as work:
        status = await work.quota.status(key)
        assert status.status == "blocked" and status.retry_basis == "known_reset"
        assert status.reset_at == now + timedelta(hours=2)
        assert await work.session.scalar(select(func.count()).select_from(SubscriptionAttempt)) == 0
        usage = await work.subscription_budget.usage(persisted_run.id, task_id)
        assert usage.outstanding.provider_attempts == 0
        assert usage.consumed.provider_attempts == 0
        assert await work.scheduler._active_count() == 0


@pytest.mark.integration
async def test_expired_probe_recovery_and_settlement_share_lock_order(
    session_factory, persisted_run
):
    import asyncio

    from forge.persistence.repositories.subscription_quota import (
        PostgresSubscriptionQuotaRepository,
    )

    clock = [datetime.now(UTC)]
    factory = _factory(session_factory, clock)
    await _seed(factory, persisted_run, ("p", "p"))
    key = QuotaPoolKey("p", "local", "subscription-allowance_only")
    await _report(factory, key, clock[0], reset=clock[0] + timedelta(seconds=1))
    clock[0] += timedelta(seconds=1)
    probe = await SubscriptionDecisionExecutor(factory).admit_next("probe", _reservation())
    assert probe is not None
    clock[0] = probe.lease.expires_at + timedelta(seconds=1)
    settling = asyncio.Event()

    class SignaledQuota(PostgresSubscriptionQuotaRepository):
        async def _lock(self, pool):
            settling.set()
            return await super()._lock(pool)

    async def settle():
        async with factory() as work:
            quota = SignaledQuota(work.session, clock=lambda: clock[0])
            await quota.settle(
                probe.attempt.attempt_id, exhaustion=None, succeeded=False, stopped=True
            )
            await work.commit()

    async def race():
        async with factory() as work:
            row = await work.quota._lock(key)
            pending = asyncio.create_task(settle())
            try:
                await settling.wait()
                await work.quota._recover_probe(row)
                await work.commit()
                await pending
            finally:
                if not pending.done():
                    pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

    await asyncio.wait_for(race(), timeout=10)
    async with factory() as work:
        state = await work.quota.status(key)
        assert state.probe_attempt_id is None and state.status == "blocked"


async def _seed(factory, run, providers, *, fallbacks=()):
    from forge.domain.run import RunSnapshot
    from forge.domain.scheduling import SchedulerCapacityPolicy
    from forge.domain.subscription import (
        ExecutionEnvelope,
        OperatorProfile,
        RolePreference,
        RouteBinding,
        SpecialistPurpose,
    )

    runs = [run]
    async with factory() as work:
        await work.scheduler.configure_capacity(
            SchedulerCapacityPolicy(version=1, global_limit=8, run_limit=2, provider_limit=8)
        )
        for _ in providers[1:]:
            next_run = RunSnapshot(
                id=uuid4(), project_id=run.project_id, task_id=run.task_id, policy_version=1
            )
            await work.runs.create(next_run)
            runs.append(next_run)
        tasks = []
        for current, provider in zip(runs, providers, strict=True):
            if fallbacks:
                route = _route(provider)
                profile = OperatorProfile(
                    profile_id=uuid4(),
                    version=1,
                    preferences=(
                        RolePreference(purpose=SpecialistPurpose.PRIMARY, preferred_route=route),
                        RolePreference(
                            purpose=SpecialistPurpose.ROUTINE_IMPLEMENTATION,
                            preferred_route=route,
                            fallback_routes=fallbacks,
                        ),
                    ),
                )
                await work.subscription.store_profile(profile)
                await work.subscription.freeze_envelope(
                    ExecutionEnvelope(
                        run_id=current.id,
                        profile_id=profile.profile_id,
                        profile_version=1,
                        safety_policy_version=1,
                        routes=(
                            (
                                SpecialistPurpose.PRIMARY,
                                RouteBinding(requested=route, effective=route, is_primary=True),
                            ),
                            (
                                SpecialistPurpose.ROUTINE_IMPLEMENTATION,
                                RouteBinding(requested=route, effective=route),
                            ),
                        ),
                        allowed_fallbacks=((SpecialistPurpose.ROUTINE_IMPLEMENTATION, fallbacks),),
                    )
                )
                from forge.domain.subscription import LogicalTaskContract, TaskBudget

                primary = uuid4()
                await work.subscription.create_task(
                    LogicalTaskContract(
                        run_id=current.id,
                        task_id=primary,
                        purpose=SpecialistPurpose.PRIMARY,
                        route=RouteBinding(requested=route, effective=route, is_primary=True),
                        budget=TaskBudget(),
                        owned_paths=("apps",),
                    ),
                    idempotency_key=str(primary),
                )
                await work.scheduler.admit_run(current.id)
            else:
                primary = await _admit_run(work, current, (_route(provider), _route(provider)))
            tasks.append(
                await _enqueue(
                    work,
                    current.id,
                    provider=provider,
                    worktree=f"tree-{current.id}",
                    parent_id=primary,
                    paths=("apps",),
                )
            )
        await work.commit()
    return runs, tasks


def _factory(session_factory, clock, *, policy=None):
    return lambda: PostgresUnitOfWork(
        session_factory, quota_policy=policy, quota_clock=lambda: clock[0]
    )


@pytest.mark.integration
async def test_downgrade_keeps_confirmed_quota_evidence(session_factory):
    import importlib.util
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    now = datetime.now(UTC)
    factory = _factory(session_factory, [now])
    key = QuotaPoolKey("p", "retained", "weekly")
    await _report(factory, key, now)
    path = (
        Path(__file__).resolve().parents[2]
        / "migrations/versions/20260912_0019_subscription_quota.py"
    )
    spec = importlib.util.spec_from_file_location("quota_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    def downgrade(session):
        with Operations.context(MigrationContext.configure(session.connection())):
            migration.downgrade()

    async with factory() as work:
        before = await work.quota.status(key)
        with pytest.raises(RuntimeError, match="quota evidence must not be discarded"):
            await work.session.run_sync(downgrade)
        assert await work.quota.status(key) == before


async def _report(factory, key, observed, *, reset=None, actor=None, identity=None):
    async with factory() as work:
        value = await work.quota.report_exhaustion(
            key,
            QuotaExhaustion(observed, "operator_report", reset),
            actor_id=actor or uuid4(),
            idempotency_key=identity or str(uuid4()),
        )
        await work.commit()
        return value


async def _success(session_factory, executor, admission):
    from forge.application.ports.subscription_gateway import SubscriptionInvocationResult
    from forge.domain.subscription import HandoffStatus, TaskHandoff
    from subscription_launch_fixture import record_stopped_launch
    from test_subscription_usage import _known

    proof = await record_stopped_launch(session_factory, admission)
    result = SubscriptionInvocationResult(
        attempt=admission.attempt,
        launch_proof=proof,
        telemetry=_known(),
        decision=TaskHandoff(
            run_id=admission.task.run_id,
            task_id=admission.task.task_id,
            attempt_id=admission.attempt.attempt_id,
            status=HandoffStatus.COMPLETED,
            candidate_tree_digest="a" * 64,
            evidence_receipt_ids=("fixture-receipt",),
        ),
    )
    await executor.settle(admission, result)


@pytest.mark.integration
async def test_shared_block_does_not_starve_another_route(session_factory, persisted_run):
    import asyncio

    clock = [datetime.now(UTC)]
    factory = _factory(session_factory, clock)
    runs, tasks = await _seed(factory, persisted_run, ("p", "p", "q"))
    key = QuotaPoolKey("p", "local", "subscription-allowance_only")
    await _report(factory, key, clock[0])
    results = await asyncio.gather(
        *(
            SubscriptionDecisionExecutor(factory).admit_next(owner, _reservation())
            for owner in ("one", "two")
        )
    )
    admitted = [value for value in results if value]
    assert len(admitted) == 1 and admitted[0].task.task_id == tasks[2]
    async with factory() as work:
        assert await work.scheduler._active_count() == 1
        for run in runs[:2]:
            assert (await work.subscription_budget.usage(run.id)).outstanding.provider_attempts == 0


@pytest.mark.integration
async def test_one_recovery_probe_and_unknown_reset_cooldown_survive_restart(
    session_factory, persisted_run
):
    import asyncio

    from forge.application.ports.subscription_gateway import (
        SubscriptionFailure,
        SubscriptionInvocationResult,
    )
    from forge.domain.subscription_quota import QuotaPolicy
    from test_subscription_usage import _known

    clock = [datetime.now(UTC)]
    factory = _factory(
        session_factory, clock, policy=QuotaPolicy(unknown_reset_cooldown_seconds=60)
    )
    await _seed(factory, persisted_run, ("p", "p"))
    key = QuotaPoolKey("p", "local", "subscription-allowance_only")
    reset = clock[0] + timedelta(seconds=120)
    await _report(factory, key, clock[0], reset=reset)
    clock[0] = reset
    async with factory() as work:
        status = await work.quota.status(key)
        assert status.status == "eligible" and status.reason == "operator_report"
    executor = SubscriptionDecisionExecutor(factory)
    results = await asyncio.gather(
        *(executor.admit_next(owner, _reservation()) for owner in ("one", "two"))
    )
    probes = [value for value in results if value]
    assert len(probes) == 1
    probe = probes[0]
    assert (
        await SubscriptionDecisionExecutor(factory).admit_next("restarted", _reservation()) is None
    )
    async with factory() as work:
        assert (await work.quota.status(key)).probe_attempt_id == probe.attempt.attempt_id
    exhausted = SubscriptionInvocationResult(
        attempt=probe.attempt,
        failure=SubscriptionFailure.QUOTA,
        telemetry=_known(),
        quota_exhaustion=QuotaExhaustion(clock[0], "codex_account_usage_exhausted"),
    )
    assert (await executor.settle(probe, exhausted)).disposition == "quota_deferred"
    assert (await executor.settle(probe, exhausted)).replayed
    async with factory() as work:
        blocked = await work.quota.status(key)
        assert blocked.retry_basis == "probe_cooldown" and blocked.reset_at is None
        assert blocked.next_eligible_at == clock[0] + timedelta(seconds=60)
        usage = await work.subscription_budget.usage(probe.task.run_id, probe.task.task_id)
        assert usage.consumed.provider_attempts == 1 and usage.consumed.repairs == 0
        assert usage.outstanding.provider_attempts == 0
        assert await work.scheduler._active_count() == 0
    clock[0] += timedelta(seconds=59)
    assert await executor.admit_next("early", _reservation()) is None
    clock[0] += timedelta(seconds=1)
    results = await asyncio.gather(
        *(executor.admit_next(owner, _reservation()) for owner in ("three", "four"))
    )
    probes = [value for value in results if value]
    assert len(probes) == 1
    await _success(session_factory, executor, probes[0])
    async with factory() as work:
        recovered = await work.quota.status(key)
        assert recovered.status == "unknown" and recovered.allows_attempt
        assert recovered.recovered_at == clock[0]
        assert recovered.observed_at == reset and recovered.revision == 2


@pytest.mark.integration
async def test_old_success_and_probe_success_do_not_clear_newer_exhaustion(
    session_factory, persisted_run
):
    clock = [datetime.now(UTC)]
    factory = _factory(session_factory, clock)
    await _seed(factory, persisted_run, ("p", "p", "p"))
    executor = SubscriptionDecisionExecutor(factory)
    old = await executor.admit_next("old", _reservation())
    key = QuotaPoolKey("p", "local", "subscription-allowance_only")
    reset = clock[0] + timedelta(seconds=30)
    await _report(factory, key, clock[0], reset=reset)
    await _success(session_factory, executor, old)
    async with factory() as work:
        assert (await work.quota.status(key)).status == "blocked"
    clock[0] = reset
    probe = await executor.admit_next("probe", _reservation())
    assert probe is not None
    later = reset + timedelta(minutes=5)
    await _report(factory, key, clock[0], reset=later)
    await _success(session_factory, executor, probe)
    async with factory() as work:
        status = await work.quota.status(key)
        assert status.status == "blocked" and status.next_eligible_at == later
        assert status.probe_attempt_id is None and status.revision == 2


@pytest.mark.integration
async def test_concurrent_reports_never_shorten_block_and_replay_once(session_factory):
    import asyncio

    from forge.persistence.models.subscription_quota import SubscriptionQuotaObservation

    now = datetime.now(UTC)
    factory = _factory(session_factory, [now])
    key = QuotaPoolKey("p", "account-a", "weekly")
    long_reset, short_reset = now + timedelta(hours=5), now + timedelta(minutes=5)
    await asyncio.gather(
        _report(factory, key, now, reset=long_reset),
        _report(factory, key, now + timedelta(seconds=1), reset=short_reset),
    )
    actor = uuid4()
    await _report(factory, key, now + timedelta(seconds=2), actor=actor, identity="replay")
    await _report(factory, key, now + timedelta(seconds=2), actor=actor, identity="replay")
    async with factory() as work:
        status = await work.quota.status(key)
        assert status.next_eligible_at == long_reset and status.reset_at == long_reset
        assert status.observed_at == now + timedelta(seconds=2) and status.revision == 3
        assert (
            await work.session.scalar(
                select(func.count()).select_from(SubscriptionQuotaObservation)
            )
            == 3
        )


@pytest.mark.integration
async def test_approved_fallback_preserves_task_and_actual_attempt_budget(
    session_factory, persisted_run, tmp_path
):
    from forge.application.ports.subscription_gateway import (
        SubscriptionFailure,
        SubscriptionInvocationResult,
    )
    from forge.domain.subscription import decode_subscription_record
    from forge.persistence.models.subscription import SubscriptionTask
    from test_subscription_usage import _known

    clock = [datetime.now(UTC)]
    factory = _factory(session_factory, clock)
    _, tasks = await _seed(factory, persisted_run, ("p",), fallbacks=(_route("q"),))
    executor = SubscriptionDecisionExecutor(factory)
    first = await executor.admit_next("preferred", _reservation())
    partial = tmp_path / "partial.txt"
    partial.write_text("preserved partial work", encoding="utf-8")
    result = SubscriptionInvocationResult(
        attempt=first.attempt,
        failure=SubscriptionFailure.QUOTA,
        telemetry=_known(),
        quota_exhaustion=QuotaExhaustion(clock[0], "codex_account_usage_exhausted"),
    )
    assert (await executor.settle(first, result)).disposition == "quota_deferred"
    fallback = await executor.admit_next("fallback", _reservation())
    assert fallback.task.task_id == tasks[0] and fallback.attempt.attempt_number == 2
    assert fallback.task.route.requested == first.task.route.requested
    assert (
        fallback.task.route.effective == _route("q") and fallback.task.budget == first.task.budget
    )
    assert fallback.task.owned_paths == first.task.owned_paths
    assert fallback.envelope == first.envelope and partial.read_text() == "preserved partial work"
    async with factory() as work:
        logical = await work.session.get(SubscriptionTask, tasks[0])
        assert decode_subscription_record(logical.payload).route == fallback.task.route
        usage = await work.subscription_budget.usage(persisted_run.id, tasks[0])
        assert usage.consumed.provider_attempts == 1 and usage.outstanding.provider_attempts == 1
        assert usage.consumed.repairs == 0


@pytest.mark.integration
async def test_quota_admission_keeps_original_account_when_worker_configuration_changes(
    session_factory, persisted_run
):
    from forge.application.ports.subscription_gateway import (
        SubscriptionFailure,
        SubscriptionInvocationResult,
    )
    from forge.domain.subscription_quota import QuotaPolicy, QuotaRoutePool
    from test_subscription_usage import _known

    clock = [datetime.now(UTC)]
    first_policy = QuotaPolicy(
        route_pools=(QuotaRoutePool("p", "p-client", "account-a", "weekly"),)
    )
    next_policy = QuotaPolicy(route_pools=(QuotaRoutePool("p", "p-client", "account-b", "weekly"),))
    first_factory = _factory(session_factory, clock, policy=first_policy)
    await _seed(first_factory, persisted_run, ("p", "p"))
    first = await SubscriptionDecisionExecutor(first_factory).admit_next(
        "account-a-worker", _reservation()
    )
    next_factory = _factory(session_factory, clock, policy=next_policy)
    executor = SubscriptionDecisionExecutor(next_factory)
    result = SubscriptionInvocationResult(
        attempt=first.attempt,
        failure=SubscriptionFailure.QUOTA,
        telemetry=_known(),
        quota_exhaustion=QuotaExhaustion(clock[0], "codex_account_usage_exhausted"),
    )
    await executor.settle(first, result)
    async with next_factory() as work:
        assert (await work.quota.status(first_policy.key_for(_route("p")))).status == "blocked"
        assert (await work.quota.status(next_policy.key_for(_route("p")))).status == "unknown"
    assert await executor.admit_next("account-b-worker", _reservation()) is not None


@pytest.mark.integration
async def test_expired_probe_cannot_be_replaced_until_client_stop_is_proved(
    session_factory, persisted_run
):
    from forge.domain.subscription_quota import QuotaPolicy
    from forge.persistence.models.scheduling import SubscriptionScheduledTask
    from forge.persistence.models.subscription import SubscriptionClientLaunch
    from subscription_launch_fixture import record_stopped_launch

    clock = [datetime.now(UTC)]
    factory = _factory(
        session_factory, clock, policy=QuotaPolicy(unknown_reset_cooldown_seconds=60)
    )
    await _seed(factory, persisted_run, ("p", "p"))
    key = QuotaPoolKey("p", "local", "subscription-allowance_only")
    await _report(factory, key, clock[0], reset=clock[0] + timedelta(seconds=1))
    clock[0] += timedelta(seconds=1)
    executor = SubscriptionDecisionExecutor(factory)
    probe = await executor.admit_next("probe-worker", _reservation())
    proof = await record_stopped_launch(session_factory, probe)
    async with factory() as work:
        launch = await work.session.scalar(
            select(SubscriptionClientLaunch).where(
                SubscriptionClientLaunch.attempt_id == probe.attempt.attempt_id
            )
        )
        launch.state = "uncertain"
        launch.terminal_payload = None
        await work.commit()
    clock[0] += timedelta(seconds=31)
    assert (
        await SubscriptionDecisionExecutor(factory).admit_next("after-restart", _reservation())
        is None
    )
    async with factory() as work:
        status = await work.quota.status(key)
        assert status.status == "blocked" and status.probe_attempt_id == probe.attempt.attempt_id
        original = await work.session.scalar(
            select(SubscriptionScheduledTask).where(
                SubscriptionScheduledTask.task_id == probe.task.task_id
            )
        )
        assert original.state == "reconciling"
        launch = await work.session.scalar(
            select(SubscriptionClientLaunch).where(
                SubscriptionClientLaunch.attempt_id == probe.attempt.attempt_id
            )
        )
        launch.state, launch.terminal_payload = "terminal", proof.model_dump(mode="json")
        await work.commit()
    assert await executor.admit_next("proof-now-stopped", _reservation()) is None
    async with factory() as work:
        status = await work.quota.status(key)
        assert status.probe_attempt_id is None
        assert status.retry_basis == "probe_cooldown"
        assert status.next_eligible_at == clock[0] + timedelta(seconds=60)
    clock[0] += timedelta(seconds=60)
    replacement = await executor.admit_next("next-probe", _reservation())
    assert replacement is not None and replacement.task.task_id != probe.task.task_id


@pytest.mark.integration
async def test_fenced_quota_response_records_exhaustion_without_fallback_or_repair(
    session_factory, persisted_run
):
    from forge.application.ports.subscription_gateway import (
        SubscriptionFailure,
        SubscriptionInvocationResult,
    )
    from forge.persistence.models.scheduling import (
        SubscriptionScheduledEffect,
        SubscriptionScheduledTask,
    )
    from test_subscription_usage import _known

    clock = [datetime.now(UTC)]
    factory = _factory(session_factory, clock)
    await _seed(factory, persisted_run, ("p", "q"), fallbacks=(_route("r"),))
    executor = SubscriptionDecisionExecutor(factory)
    admission = await executor.admit_next("writer", _reservation())
    async with factory() as work:
        work.session.add(
            SubscriptionScheduledEffect(
                id=uuid4(),
                run_id=admission.task.run_id,
                task_id=admission.task.task_id,
                lease_owner=admission.lease.owner,
                lease_generation=admission.lease.generation,
                candidate_epoch=admission.candidate_epoch,
                state="admitted",
            )
        )
        await work.commit()
    result = SubscriptionInvocationResult(
        attempt=admission.attempt,
        failure=SubscriptionFailure.UNCERTAIN,
        telemetry=_known(),
        quota_exhaustion=QuotaExhaustion(clock[0], "provider_usage_exhausted"),
    )
    assert (await executor.settle(admission, result)).disposition == "fenced"
    async with factory() as work:
        assert (
            await work.quota.status(QuotaPoolKey("p", "local", "subscription-allowance_only"))
        ).status == "blocked"
        row = await work.session.scalar(
            select(SubscriptionScheduledTask).where(
                SubscriptionScheduledTask.task_id == admission.task.task_id
            )
        )
        assert row.state == "reconciling" and row.provider == "p" and row.repairs == 0
    other = await executor.admit_next("other-route", _reservation())
    assert other is not None and other.task.route.effective.provider == "q"


@pytest.mark.integration
@pytest.mark.parametrize("primary,paid", [(True, False), (False, True)])
async def test_blocked_primary_or_unapproved_billing_transition_defers(
    session_factory, persisted_run, primary, paid
):
    from dataclasses import replace

    from forge.domain.scheduling import ScheduleTask
    from forge.domain.subscription import (
        BillingMode,
        SpecialistPurpose,
        decode_subscription_record,
        encode_subscription_record,
        is_read_only,
    )
    from forge.persistence.models.scheduling import SubscriptionScheduledTask
    from forge.persistence.models.subscription import SubscriptionEnvelope, SubscriptionTask

    clock = [datetime.now(UTC)]
    factory = _factory(session_factory, clock)
    await _seed(factory, persisted_run, ("p",), fallbacks=(_route("q"),))
    async with factory() as work:
        if primary:
            child = await work.session.scalar(select(SubscriptionScheduledTask))
            # Fixture schedules only the selected primary for this independent case.
            child.state = "terminal"
            logical = await work.session.scalar(
                select(SubscriptionTask).where(SubscriptionTask.parent_task_id.is_(None))
            )
            contract = decode_subscription_record(logical.payload)
            await work.scheduler.enqueue(
                ScheduleTask(
                    run_id=persisted_run.id,
                    task_id=logical.id,
                    worktree_id="primary-quota",
                    owned_paths=contract.owned_paths,
                    max_repairs=contract.max_repairs,
                    read_only=is_read_only(contract.purpose),
                )
            )
        else:
            envelope_row = await work.session.get(SubscriptionEnvelope, persisted_run.id)
            envelope = decode_subscription_record(envelope_row.payload)
            paid_route = replace(_route("q"), billing_mode=BillingMode.PAID_OPT_IN)
            envelope_row.payload = encode_subscription_record(
                replace(
                    envelope,
                    billing_mode=BillingMode.PAID_OPT_IN,
                    allowed_fallbacks=((SpecialistPurpose.ROUTINE_IMPLEMENTATION, (paid_route,)),),
                )
            )
        await work.commit()
    await _report(factory, QuotaPoolKey("p", "local", "subscription-allowance_only"), clock[0])
    assert await SubscriptionDecisionExecutor(factory).admit_next("blocked", _reservation()) is None
    async with factory() as work:
        assert await work.session.scalar(select(func.count()).select_from(SubscriptionAttempt)) == 0
        assert await work.scheduler._active_count() == 0
