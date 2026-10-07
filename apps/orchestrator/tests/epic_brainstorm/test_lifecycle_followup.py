import asyncio
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from forge.agents.client_process import ClientLaunchSpec, ClientProcessSupervisor, _process_token
from forge.application.ports.epic_brainstorm import BrainstormGatewayResult
from forge.domain.subscription import TaskBudget, UnknownTelemetryPolicy, encode_subscription_record
from forge.persistence.models.epic_brainstorm import (
    BrainstormAttemptRow,
    BrainstormBudgetLedger,
    BrainstormJobRow,
)
from forge.persistence.models.subscription_quota import SubscriptionQuotaPool
from forge.persistence.repositories.epic_brainstorm import PostgresBrainstormRepository
from forge.worker.epic_brainstorm import (
    DurableBrainstormProcessLifecycle,
    EpicBrainstormWorker,
    worker_host_scope,
)
from sqlalchemy import select


@pytest.mark.asyncio
async def test_authoring_claim_waiting_on_epic_ledger_does_not_hold_quota_pool(
    brainstorm_session_factory,
):
    _, epic, project, _, receipt = await prepared(brainstorm_session_factory)
    async with brainstorm_session_factory() as session, session.begin():
        job = await session.get(BrainstormJobRow, receipt.job_id)
        assert job is not None
        repository = PostgresBrainstormRepository(session)
        pool = await repository.quota_pool(repository.decode_snapshot(job))
        pool_key = (pool.provider, pool.account, pool.pool)
        session.add(
            BrainstormBudgetLedger(
                epic_id=epic,
                project_id=project,
                ceiling=encode_subscription_record(TaskBudget()),
            )
        )
    entered = asyncio.Event()

    async def claimant():
        async with brainstorm_session_factory() as session, session.begin():
            entered.set()
            return await PostgresBrainstormRepository(session).claim("ledger-wait")

    async with brainstorm_session_factory() as holder:
        async with holder.begin():
            await holder.get(BrainstormBudgetLedger, epic, with_for_update=True)
            task = asyncio.create_task(claimant())
            await entered.wait()
            await asyncio.sleep(0.1)
            assert not task.done()
            async with brainstorm_session_factory() as observer, observer.begin():
                # NOWAIT distinguishes the old pool→ledger inversion from the
                # new ledger→pool order while the claim is stalled on ledger.
                locked_pool = await observer.scalar(
                    select(SubscriptionQuotaPool)
                    .where(
                        SubscriptionQuotaPool.provider == pool_key[0],
                        SubscriptionQuotaPool.account == pool_key[1],
                        SubscriptionQuotaPool.pool == pool_key[2],
                    )
                    .with_for_update(nowait=True)
                )
                assert locked_pool is not None
        admitted = await asyncio.wait_for(task, 5)
    assert admitted is not None and admitted[0].id == receipt.job_id


from apps.orchestrator.tests.epic_brainstorm.test_brainstorm_worker import prepared


async def second_job(service, epic, project, actor):
    conversation, version = await service.create(
        epic_id=epic, project_id=project, actor=actor, key="second-conversation", text="Next"
    )
    turn = (await service.turns(epic_id=epic, project_id=project, conversation_id=conversation))[0]
    return await service.submit(
        epic_id=epic,
        project_id=project,
        conversation_id=conversation,
        prompt_turn_id=turn.turn_id,
        expected_epic_version=1,
        expected_conversation_version=version,
        actor=actor,
        key="second-job",
    )


@pytest.mark.asyncio
async def test_claim_rejects_snapshot_history_from_another_conversation(brainstorm_session_factory):
    service, epic, project, actor, first = await prepared(brainstorm_session_factory)
    second = await second_job(service, epic, project, actor)
    async with brainstorm_session_factory() as session, session.begin():
        first_row = await session.get(BrainstormJobRow, first.job_id)
        second_row = await session.get(BrainstormJobRow, second.job_id)
        first_row.snapshot = {
            **second_row.snapshot,
            "job_id": str(first.job_id),
            "reservation_id": first_row.snapshot["reservation_id"],
        }
    async with brainstorm_session_factory() as session, session.begin():
        claimed = await PostgresBrainstormRepository(session).claim("history-scope")
        assert claimed is not None and claimed[0].id == second.job_id
        rejected = await session.get(BrainstormJobRow, first.job_id)
        assert rejected.state == "failed" and rejected.failure == "input_conflict"
        assert rejected.current_attempt_id is None


@pytest.mark.asyncio
async def test_operator_cancel_interrupts_slow_cleanup_once(brainstorm_session_factory):
    service, epic, project, actor, receipt = await prepared(brainstorm_session_factory)
    entered = asyncio.Event()
    cancellations = []

    class Gateway:
        async def execute(self, job, turns, reader, *, cancelled, lifecycle):
            entered.set()
            while True:
                try:
                    await asyncio.sleep(30)
                except asyncio.CancelledError:
                    cancellations.append(1)
                    try:
                        await asyncio.sleep(2.3)
                    except asyncio.CancelledError:
                        cancellations.append(1)
                    return BrainstormGatewayResult(
                        proposal=None, telemetry=None, failure="cancelled"
                    )

    worker = EpicBrainstormWorker(
        brainstorm_session_factory,
        owner="once",
        gateway_factory=lambda _: Gateway(),
        reader_factory=lambda _: object(),
    )
    task = asyncio.create_task(worker.run_once())
    await asyncio.wait_for(entered.wait(), 5)
    current = await service.observe(epic_id=epic, project_id=project, job_id=receipt.job_id)
    await service.cancel(
        epic_id=epic,
        project_id=project,
        job_id=receipt.job_id,
        expected_job_version=current.job_version,
        actor=actor,
        key="slow-cancel",
    )
    assert await asyncio.wait_for(task, 5) == receipt.job_id
    assert len(cancellations) == 1
    outcome = await service.observe(epic_id=epic, project_id=project, job_id=receipt.job_id)
    assert outcome.state == "cancelled"


@pytest.mark.asyncio
async def test_repeated_caller_cancel_does_not_interrupt_slow_cleanup(brainstorm_session_factory):
    service, epic, project, _, receipt = await prepared(brainstorm_session_factory)
    entered = asyncio.Event()
    cancellations = []

    class Gateway:
        async def execute(self, job, turns, reader, *, cancelled, lifecycle):
            entered.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancellations.append(1)
                try:
                    await asyncio.sleep(2.3)
                except asyncio.CancelledError:
                    cancellations.append(1)
                return BrainstormGatewayResult(proposal=None, telemetry=None, failure="cancelled")

    worker = EpicBrainstormWorker(
        brainstorm_session_factory,
        owner="twice",
        gateway_factory=lambda _: Gateway(),
        reader_factory=lambda _: object(),
    )
    task = asyncio.create_task(worker.run_once())
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    await asyncio.sleep(1.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    assert len(cancellations) == 1
    outcome = await service.observe(epic_id=epic, project_id=project, job_id=receipt.job_id)
    assert outcome.state == "failed" and outcome.failure == "interrupted"


@pytest.mark.asyncio
async def test_caller_cancel_during_exception_cleanup_propagates(brainstorm_session_factory):
    service, epic, project, _, receipt = await prepared(brainstorm_session_factory)
    cleanup_started = asyncio.Event()
    cancellations = []

    class Gateway:
        async def execute(self, job, turns, reader, *, cancelled, lifecycle):
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancellations.append(1)
                cleanup_started.set()
                try:
                    await asyncio.sleep(1.6)
                except asyncio.CancelledError:
                    cancellations.append(1)
                return BrainstormGatewayResult(proposal=None, telemetry=None, failure="cancelled")

    worker = EpicBrainstormWorker(
        brainstorm_session_factory,
        owner="cleanup-exception",
        gateway_factory=lambda _: Gateway(),
        reader_factory=lambda _: object(),
    )

    async def broken_renew(*args):
        raise RuntimeError("simulated renewal failure")

    worker._renew = broken_renew
    task = asyncio.create_task(worker.run_once())
    await asyncio.wait_for(cleanup_started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    assert len(cancellations) == 1
    outcome = await service.observe(epic_id=epic, project_id=project, job_id=receipt.job_id)
    assert outcome.state == "failed" and outcome.failure == "interrupted"


@pytest.mark.asyncio
async def test_caller_cancel_during_exception_finalization_propagates(brainstorm_session_factory):
    service, epic, project, _, receipt = await prepared(brainstorm_session_factory)
    applying = asyncio.Event()

    class Gateway:
        async def execute(self, job, turns, reader, *, cancelled, lifecycle):
            await asyncio.Event().wait()

    worker = EpicBrainstormWorker(
        brainstorm_session_factory,
        owner="finalize-exception",
        gateway_factory=lambda _: Gateway(),
        reader_factory=lambda _: object(),
    )

    async def broken_renew(*args):
        raise RuntimeError("simulated renewal failure")

    original_apply = worker._apply

    async def delayed_apply(*args):
        applying.set()
        await asyncio.sleep(1.2)
        return await original_apply(*args)

    worker._renew = broken_renew
    worker._apply = delayed_apply
    task = asyncio.create_task(worker.run_once())
    await asyncio.wait_for(applying.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    outcome = await service.observe(epic_id=epic, project_id=project, job_id=receipt.job_id)
    assert outcome.state == "failed" and outcome.failure == "interrupted"


@pytest.mark.asyncio
async def test_cancellation_resistant_gateway_returns_bounded_reconciling(
    brainstorm_session_factory,
):
    service, epic, project, actor, receipt = await prepared(brainstorm_session_factory)
    entered = asyncio.Event()
    release = asyncio.Event()
    gateway_task = None

    class Gateway:
        async def execute(self, job, turns, reader, *, cancelled, lifecycle):
            nonlocal gateway_task
            gateway_task = asyncio.current_task()
            await lifecycle.launch_intent(str(uuid4()))
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            return BrainstormGatewayResult(proposal=None, telemetry=None, failure="cancelled")

    worker = EpicBrainstormWorker(
        brainstorm_session_factory,
        owner="resistant",
        gateway_factory=lambda _: Gateway(),
        reader_factory=lambda _: object(),
    )
    task = asyncio.create_task(worker.run_once())
    await asyncio.wait_for(entered.wait(), 5)
    current = await service.observe(epic_id=epic, project_id=project, job_id=receipt.job_id)
    await service.cancel(
        epic_id=epic,
        project_id=project,
        job_id=receipt.job_id,
        expected_job_version=current.job_version,
        actor=actor,
        key="resistant-cancel",
    )
    assert await asyncio.wait_for(task, 13) == receipt.job_id
    outcome = await service.observe(epic_id=epic, project_id=project, job_id=receipt.job_id)
    assert outcome.state == "reconciling" and outcome.failure == "cancelled"
    assert not outcome.process_settled and outcome.held_reservations.duration_ms > 0
    release.set()
    await asyncio.wait_for(gateway_task, 2)


@pytest.mark.asyncio
async def test_expired_prelaunch_duration_is_unknown_not_outage_time(brainstorm_session_factory):
    service, epic, project, _, receipt = await prepared(brainstorm_session_factory)
    async with brainstorm_session_factory() as session, session.begin():
        repository = PostgresBrainstormRepository(session)
        claimed = await repository.claim("dead-worker", 5)
        assert claimed is not None
        _, attempt = claimed
        attempt.tool_calls_used = 1
        attempt.lease_expires_at = datetime.now(UTC) - timedelta(hours=2)
        attempt.created_at = datetime.now(UTC) - timedelta(hours=2)
    worker = EpicBrainstormWorker(
        brainstorm_session_factory,
        owner="recoverer",
        gateway_factory=lambda _: None,
        reader_factory=lambda _: object(),
    )
    assert await worker.reconcile_settled() == receipt.job_id
    outcome = await service.observe(epic_id=epic, project_id=project, job_id=receipt.job_id)
    assert outcome.state == "failed" and outcome.process_settled
    assert outcome.usage.duration_ms is None
    assert "duration_ms" in outcome.unknown_usage_fields
    assert outcome.usage.tool_call_count == 1
    assert outcome.cumulative_usage.tool_call_count == 1
    assert outcome.held_reservations.duration_ms > 0
    assert outcome.usage.input_tokens == outcome.usage.output_tokens == 0


@pytest.mark.asyncio
async def test_real_subprocess_read_crash_and_postgres_recovery(
    brainstorm_session_factory, migrated_database_url
):
    service, epic, project, _, receipt = await prepared(brainstorm_session_factory)
    child = Path(__file__).with_name("recovery_child.py")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
    env["FORGE_TEST_DSN"] = migrated_database_url
    env["FORGE_RECOVERY_MODE"] = "read"
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(child),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        assert process.stdout is not None
        assert (await asyncio.wait_for(process.stdout.readline(), 8)).strip() == b"READ_DURABLE"
        async with brainstorm_session_factory() as session:
            row = await session.get(BrainstormJobRow, receipt.job_id)
            attempt = await session.get(BrainstormAttemptRow, row.current_attempt_id)
            assert attempt.tool_calls_used == 1 and not attempt.launch_intent
            deadline = attempt.lease_expires_at
    finally:
        if process.returncode is None:
            process.kill()
        await asyncio.wait_for(process.wait(), 5)
    await asyncio.sleep(max(0, (deadline - datetime.now(UTC)).total_seconds()) + 0.1)
    env["FORGE_RECOVERY_MODE"] = "recover"
    restarted = await asyncio.create_subprocess_exec(
        sys.executable,
        str(child),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await asyncio.wait_for(restarted.communicate(), 10)
    assert restarted.returncode == 0 and stdout.strip() == b"RECOVERED"
    outcome = await service.observe(epic_id=epic, project_id=project, job_id=receipt.job_id)
    assert outcome.state == "failed" and outcome.failure == "lost_result"
    assert outcome.usage.tool_call_count == outcome.cumulative_usage.tool_call_count == 1
    assert outcome.usage.duration_ms is None and outcome.usage.duration_lower_bound_ms == 0
    assert outcome.usage.input_tokens == outcome.usage.output_tokens == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("measured_seconds", [1, 3])
@pytest.mark.parametrize("durable_callback", [False, True])
async def test_recovery_preserves_measured_duration_floor_without_outage_charge(
    brainstorm_session_factory, measured_seconds, durable_callback
):
    service, epic, project, actor, receipt = await prepared(
        brainstorm_session_factory,
        budget=TaskBudget(
            max_duration_seconds=2,
            max_provider_attempts=3,
            unknown_telemetry_policy=UnknownTelemetryPolicy(max_uncertain_attempts=2),
        ),
    )
    peer = None
    try:
        async with brainstorm_session_factory() as session, session.begin():
            repository = PostgresBrainstormRepository(session)
            claimed = await repository.claim("duration-worker", 5)
            assert claimed is not None
            row, attempt = claimed
            snapshot = repository.decode_snapshot(row)
            attempt_id, fence = attempt.id, attempt.fence
        lifecycle = DurableBrainstormProcessLifecycle(
            brainstorm_session_factory, attempt_id, fence, "duration-worker"
        )

        class Lifecycle:
            async def launch_intent(self, launch_id):
                await lifecycle.launch_intent(launch_id)

            async def started(self, process_receipt):
                await lifecycle.started(process_receipt)

            async def finished(self, process_receipt, result):
                if durable_callback:
                    await lifecycle.finished(process_receipt, result)

        peer = await ClientProcessSupervisor().start(
            ClientLaunchSpec(
                argv=(sys.executable, "-c", "import time; time.sleep(30)"),
                cwd=".",
                environment={},
                duration_seconds=40,
            ),
            lifecycle=Lifecycle(),
        )
        async with brainstorm_session_factory() as session, session.begin():
            attempt = await session.get(BrainstormAttemptRow, attempt_id)
            attempt.created_at = datetime.now(UTC) - timedelta(seconds=measured_seconds)
        worker = EpicBrainstormWorker(
            brainstorm_session_factory,
            owner="duration-worker",
            gateway_factory=lambda _: None,
            reader_factory=lambda _: object(),
        )
        await worker._apply(snapshot, attempt_id, fence, None, "timeout", asyncio.Event())
        live = await service.observe(epic_id=epic, project_id=project, job_id=receipt.job_id)
        floor = live.usage.duration_lower_bound_ms
        assert live.usage.duration_ms is None and floor >= measured_seconds * 1000
        assert live.cumulative_usage.duration_ms >= floor
        assert live.held_reservations.duration_ms == max(2000 - floor, 0)
        async with brainstorm_session_factory() as session, session.begin():
            attempt = await session.get(BrainstormAttemptRow, attempt_id)
            attempt.created_at = datetime.now(UTC) - timedelta(hours=2)
        assert await worker.reconcile_settled() is None
        ambiguous = await service.observe(epic_id=epic, project_id=project, job_id=receipt.job_id)
        assert ambiguous.usage.duration_lower_bound_ms == floor
        assert ambiguous.cumulative_usage.duration_ms == live.cumulative_usage.duration_ms
        async with brainstorm_session_factory() as session, session.begin():
            row = await session.get(BrainstormJobRow, receipt.job_id)
            row.next_eligible_at = datetime.now(UTC) - timedelta(seconds=1)
        assert await worker.reconcile_settled() is None
        repeated = await service.observe(epic_id=epic, project_id=project, job_id=receipt.job_id)
        assert repeated.usage.duration_lower_bound_ms == floor
        await peer.close()
        if durable_callback:
            before_recovery = await service.observe(
                epic_id=epic, project_id=project, job_id=receipt.job_id
            )
            assert before_recovery.process_settled
            assert before_recovery.usage.duration_ms is None
            assert before_recovery.usage.duration_lower_bound_ms == floor
            assert before_recovery.held_reservations.duration_ms == max(2000 - floor, 0)
            waiting = await second_job(service, epic, project, actor)
            async with brainstorm_session_factory() as session, session.begin():
                assert await PostgresBrainstormRepository(session).claim("late-callback") is None
            not_admitted = await service.observe(
                epic_id=epic, project_id=project, job_id=waiting.job_id
            )
            assert not_admitted.state != "running" and not_admitted.reservation is None
        async with brainstorm_session_factory() as session, session.begin():
            row = await session.get(BrainstormJobRow, receipt.job_id)
            row.next_eligible_at = datetime.now(UTC) - timedelta(seconds=1)
        assert await worker.reconcile_settled() == receipt.job_id
        settled = await service.observe(epic_id=epic, project_id=project, job_id=receipt.job_id)
        assert settled.usage.duration_ms is None
        assert settled.usage.duration_lower_bound_ms == floor
        assert settled.cumulative_usage.duration_ms == live.cumulative_usage.duration_ms
        assert settled.held_reservations.duration_ms == max(2000 - floor, 0)
        if measured_seconds == 3:
            second = await second_job(service, epic, project, actor)
            assert await worker.run_once() is None
            blocked = await service.observe(epic_id=epic, project_id=project, job_id=second.job_id)
            assert blocked.state == "failed" and blocked.failure == "budget_exhausted"
    finally:
        if peer is not None:
            await peer.close()


@pytest.mark.asyncio
async def test_cancel_tombstone_survives_unsettled_apply_and_gone_recovery(
    brainstorm_session_factory,
):
    service, epic, project, actor, receipt = await prepared(brainstorm_session_factory)
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time; time.sleep(30)"
    )
    try:
        async with brainstorm_session_factory() as session, session.begin():
            repository = PostgresBrainstormRepository(session)
            claimed = await repository.claim("cancel-worker", 5)
            assert claimed is not None
            row, attempt = claimed
            snapshot = repository.decode_snapshot(row)
            attempt.launch_intent = True
            attempt.launch_id = str(uuid4())
            attempt.origin_host = worker_host_scope()
            attempt.process_started = True
            attempt.process_pid = process.pid
            attempt.process_identity = _process_token(process.pid)
            attempt.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            attempt_id, fence = attempt.id, attempt.fence
        current = await service.observe(epic_id=epic, project_id=project, job_id=receipt.job_id)
        await service.cancel(
            epic_id=epic,
            project_id=project,
            job_id=receipt.job_id,
            expected_job_version=current.job_version,
            actor=actor,
            key="cancel-unsettled",
        )
        worker = EpicBrainstormWorker(
            brainstorm_session_factory,
            owner="cancel-worker",
            gateway_factory=lambda _: None,
            reader_factory=lambda _: object(),
        )
        await worker._apply(snapshot, attempt_id, fence, None, "interrupted", asyncio.Event())
        intermediate = await service.observe(
            epic_id=epic, project_id=project, job_id=receipt.job_id
        )
        assert intermediate.state == "reconciling" and intermediate.failure == "cancelled"
        assert not intermediate.process_settled
        process.terminate()
        await asyncio.wait_for(process.wait(), 5)
        assert await worker.reconcile_settled() == receipt.job_id
        terminal = await service.observe(epic_id=epic, project_id=project, job_id=receipt.job_id)
        assert terminal.state == "cancelled" and terminal.failure == "cancelled"
        await worker._apply(snapshot, attempt_id, fence, None, "unavailable", asyncio.Event())
        assert (
            await service.observe(epic_id=epic, project_id=project, job_id=receipt.job_id)
        ).state == "cancelled"
    finally:
        if process.returncode is None:
            process.terminate()
            await asyncio.wait_for(process.wait(), 5)


@pytest.mark.asyncio
async def test_capacity_settlement_skips_locked_waiter_without_deadlock(
    brainstorm_session_factory,
):
    service, epic, project, actor, first = await prepared(brainstorm_session_factory)
    second = await second_job(service, epic, project, actor)
    async with brainstorm_session_factory() as session, session.begin():
        repository = PostgresBrainstormRepository(session)
        admitted = await repository.claim("first")
        assert admitted is not None and admitted[0].id == first.job_id
        assert await repository.claim("second") is None
    waiting = await service.observe(epic_id=epic, project_id=project, job_id=second.job_id)
    assert waiting.state == "capacity_wait"
    async with brainstorm_session_factory() as session, session.begin():
        row = await session.get(BrainstormJobRow, second.job_id)
        row.next_eligible_at = datetime.now(UTC) - timedelta(seconds=1)

    waiter_locked = asyncio.Event()
    pool_locked = asyncio.Event()

    async def claimant():
        async with brainstorm_session_factory() as session, session.begin():
            row = await session.scalar(
                select(BrainstormJobRow)
                .where(BrainstormJobRow.id == second.job_id)
                .with_for_update()
            )
            assert row is not None
            waiter_locked.set()
            await pool_locked.wait()
            claimed = await PostgresBrainstormRepository(session).claim("second")
            return claimed[0].id if claimed else None

    async def settler():
        await waiter_locked.wait()
        async with brainstorm_session_factory() as session, session.begin():
            repository = PostgresBrainstormRepository(session)
            row = await session.get(BrainstormJobRow, first.job_id, with_for_update=True)
            attempt = await session.get(
                BrainstormAttemptRow, row.current_attempt_id, with_for_update=True
            )
            assert attempt is not None
            pool = await repository.quota_pool(repository.decode_snapshot(row))
            assert pool is not None
            pool_locked.set()
            await asyncio.sleep(0.15)
            attempt.process_settled = True
            attempt.usage_known = True
            attempt.usage = {
                "duration_ms": 1000,
                "tool_call_count": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "estimated_api_cost_minor": 0,
            }
            row.state = "failed"
            await repository.quota_settle(row, attempt, exhausted=False, reset_at=None)

    claimant_task = asyncio.create_task(claimant())
    settler_task = asyncio.create_task(settler())
    claimed_id, _ = await asyncio.wait_for(asyncio.gather(claimant_task, settler_task), 5)
    assert claimed_id == second.job_id


@pytest.mark.asyncio
async def test_opposing_pool_claims_never_hold_two_resource_scopes(brainstorm_session_factory):
    a_service, a_epic, a_project, a_actor, a_first = await prepared(
        brainstorm_session_factory, provider="provider-a", repository="example/scope-a"
    )
    b_service, b_epic, b_project, b_actor, b_first = await prepared(
        brainstorm_session_factory, provider="provider-b", repository="example/scope-b"
    )
    a_second = await second_job(a_service, a_epic, a_project, a_actor)
    b_second = await second_job(b_service, b_epic, b_project, b_actor)
    async with brainstorm_session_factory() as session, session.begin():
        repository = PostgresBrainstormRepository(session)
        for job_id in (a_first.job_id, b_first.job_id):
            row = await session.get(BrainstormJobRow, job_id)
            pool = await repository.quota_pool(repository.decode_snapshot(row))
            pool.blocked = True
            pool.probe_attempt_id = uuid4()
            pool.next_eligible_at = datetime.now(UTC) - timedelta(seconds=1)

    a_locked, b_locked = asyncio.Event(), asyncio.Event()
    allow_a, allow_b = asyncio.Event(), asyncio.Event()

    class PausingRepository(PostgresBrainstormRepository):
        def __init__(self, session, locked, release):
            super().__init__(session)
            self.locked, self.release, self.pool_calls = locked, release, 0
            self.scopes = set()

        async def quota_pool(self, snapshot):
            key = self.quota_policy.key_for(snapshot.route.effective)
            self.scopes.add((snapshot.epic_id, key.provider, key.account, key.pool))
            pool = await super().quota_pool(snapshot)
            self.pool_calls += 1
            if self.pool_calls == 1:
                self.locked.set()
                await self.release.wait()
            return pool

    async def claim(locked, release):
        async with brainstorm_session_factory() as session, session.begin():
            repository = PausingRepository(session, locked, release)
            result = await repository.claim("opposing", 5)
            return result, repository.scopes

    a_task = asyncio.create_task(claim(a_locked, allow_a))
    await asyncio.wait_for(a_locked.wait(), 5)
    b_task = asyncio.create_task(claim(b_locked, allow_b))
    await asyncio.wait_for(b_locked.wait(), 5)
    allow_b.set()
    await asyncio.sleep(0.15)
    allow_a.set()
    (a_result, a_scopes), (b_result, b_scopes) = await asyncio.wait_for(
        asyncio.gather(a_task, b_task), 5
    )
    assert a_result is None and b_result is None
    assert len(a_scopes) == len(b_scopes) == 1
    async with brainstorm_session_factory() as session, session.begin():
        repository = PostgresBrainstormRepository(session)
        for job_id in (a_first.job_id, b_first.job_id):
            row = await session.get(BrainstormJobRow, job_id)
            pool = await repository.quota_pool(repository.decode_snapshot(row))
            pool.blocked = False
            pool.probe_attempt_id = None
        for job_id in (a_first.job_id, b_first.job_id, a_second.job_id, b_second.job_id):
            row = await session.get(BrainstormJobRow, job_id)
            row.next_eligible_at = datetime.now(UTC) - timedelta(seconds=1)
    async with brainstorm_session_factory() as session, session.begin():
        admitted = await PostgresBrainstormRepository(session).claim("next-a")
        assert admitted is not None and admitted[0].id == a_first.job_id
    async with brainstorm_session_factory() as session, session.begin():
        admitted = await PostgresBrainstormRepository(session).claim("next-b")
        assert admitted is not None and admitted[0].id == b_first.job_id
    assert (
        await a_service.observe(epic_id=a_epic, project_id=a_project, job_id=a_first.job_id)
    ).state == "running"
    assert (
        await b_service.observe(epic_id=b_epic, project_id=b_project, job_id=b_first.job_id)
    ).state == "running"


@pytest.mark.asyncio
async def test_claim_bounds_invalid_job_scan_and_reaches_later_scope(brainstorm_session_factory):
    for number in range(50):
        _, _, _, _, receipt = await prepared(
            brainstorm_session_factory, repository=f"example/invalid-claim-{number}"
        )
        async with brainstorm_session_factory() as session, session.begin():
            row = await session.get(BrainstormJobRow, receipt.job_id)
            row.snapshot = {**row.snapshot, "prompt_turn_id": str(uuid4())}
    _, _, _, _, tail = await prepared(
        brainstorm_session_factory, repository="example/valid-claim-tail"
    )
    async with brainstorm_session_factory() as session, session.begin():
        assert await PostgresBrainstormRepository(session).claim("first-batch") is None
    async with brainstorm_session_factory() as session, session.begin():
        claimed = await PostgresBrainstormRepository(session).claim("next-batch")
        assert claimed is not None and claimed[0].id == tail.job_id


@pytest.mark.asyncio
@pytest.mark.parametrize("probe", [False, True])
async def test_wait_rechecks_refresh_deadline_without_version_churn(
    brainstorm_session_factory, probe
):
    service, epic, project, actor, first = await prepared(brainstorm_session_factory)
    second = await second_job(service, epic, project, actor)
    async with brainstorm_session_factory() as session, session.begin():
        repository = PostgresBrainstormRepository(session)
        admitted = await repository.claim("first")
        assert admitted is not None and admitted[0].id == first.job_id
        if probe:
            pool = await repository.quota_pool(repository.decode_snapshot(admitted[0]))
            pool.blocked = True
            pool.probe_attempt_id = admitted[1].id
            pool.next_eligible_at = datetime.now(UTC) - timedelta(seconds=1)
        assert await repository.claim("other") is None
    initial = await service.observe(epic_id=epic, project_id=project, job_id=second.job_id)
    assert initial.state == ("quota_wait" if probe else "capacity_wait")
    for _ in range(2):
        async with brainstorm_session_factory() as session, session.begin():
            row = await session.get(BrainstormJobRow, second.job_id)
            row.next_eligible_at = datetime.now(UTC) - timedelta(seconds=1)
        async with brainstorm_session_factory() as session, session.begin():
            assert await PostgresBrainstormRepository(session).claim("other") is None
        current = await service.observe(epic_id=epic, project_id=project, job_id=second.job_id)
        assert current.job_version == initial.job_version
        async with brainstorm_session_factory() as session:
            row = await session.get(BrainstormJobRow, second.job_id)
            assert row.next_eligible_at > datetime.now(UTC)
    await service.cancel(
        epic_id=epic,
        project_id=project,
        job_id=second.job_id,
        expected_job_version=initial.job_version,
        actor=actor,
        key="stable-version-cancel",
    )
    if probe:
        async with brainstorm_session_factory() as session, session.begin():
            repository = PostgresBrainstormRepository(session)
            row = await session.get(BrainstormJobRow, first.job_id)
            pool = await repository.quota_pool(repository.decode_snapshot(row))
            pool.blocked = False
            pool.probe_attempt_id = None


@pytest.mark.asyncio
async def test_recovery_scans_past_fifty_ambiguous_older_orphans(brainstorm_session_factory):
    held = []
    for number in range(50):
        service, epic, project, _, receipt = await prepared(
            brainstorm_session_factory, repository=f"example/held-{number}"
        )
        async with brainstorm_session_factory() as session, session.begin():
            claimed = await PostgresBrainstormRepository(session).claim("dead-worker", 5)
            assert claimed is not None and claimed[0].id == receipt.job_id
            attempt = claimed[1]
            attempt.launch_intent = True
            attempt.launch_id = str(uuid4())
            attempt.origin_host = "foreign-host"
            attempt.process_started = True
            attempt.process_pid = 12345
            attempt.process_identity = "unobservable"
            attempt.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        held.append((service, epic, project, receipt.job_id))
    service, epic, project, _, tail = await prepared(
        brainstorm_session_factory, repository="example/gone-tail"
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time; time.sleep(30)"
    )
    try:
        token = _process_token(process.pid)
        assert token
        async with brainstorm_session_factory() as session, session.begin():
            claimed = await PostgresBrainstormRepository(session).claim("dead-worker", 5)
            assert claimed is not None and claimed[0].id == tail.job_id
            attempt = claimed[1]
            attempt.launch_intent = True
            attempt.launch_id = str(uuid4())
            attempt.origin_host = worker_host_scope()
            attempt.process_started = True
            attempt.process_pid = process.pid
            attempt.process_identity = token
            attempt.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        process.terminate()
        await asyncio.wait_for(process.wait(), 5)
        worker = EpicBrainstormWorker(
            brainstorm_session_factory,
            owner="recoverer",
            gateway_factory=lambda _: None,
            reader_factory=lambda _: object(),
        )
        assert await worker.reconcile_settled() is None
        oldest_service, oldest_epic, oldest_project, oldest_id = held[0]
        first_poll = await oldest_service.observe(
            epic_id=oldest_epic, project_id=oldest_project, job_id=oldest_id
        )
        assert first_poll.state == "reconciling" and not first_poll.process_settled
        # Simulate a delayed next poll: the old batch is eligible again.
        async with brainstorm_session_factory() as session, session.begin():
            for _, _, _, job_id in held:
                row = await session.get(BrainstormJobRow, job_id)
                row.next_eligible_at = datetime.now(UTC) - timedelta(seconds=1)
        assert await worker.reconcile_settled() == tail.job_id
        tail_outcome = await service.observe(epic_id=epic, project_id=project, job_id=tail.job_id)
        assert tail_outcome.state == "failed" and tail_outcome.process_settled
        oldest = await oldest_service.observe(
            epic_id=oldest_epic, project_id=oldest_project, job_id=oldest_id
        )
        assert oldest.state == "reconciling" and not oldest.process_settled
        assert oldest.job_version == first_poll.job_version
        assert oldest.held_reservations.duration_ms > 0
    finally:
        if process.returncode is None:
            process.terminate()
            await asyncio.wait_for(process.wait(), 5)
