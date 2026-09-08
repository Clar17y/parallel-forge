"""Startup uncertainty isolates a run without repeating effects or blocking its peers."""

import asyncio
from uuid import uuid4

import pytest
from forge.application.ports.commands import CommandLane
from forge.application.services.recovery import RecoveryService
from forge.domain.command import CommandStatus
from forge.domain.operation import canonical_digest
from forge.domain.run import RunSnapshot, RunState
from forge.persistence.repositories.recovery import PostgresRecoveryBarrier
from forge.persistence.unit_of_work import PostgresUnitOfWork
from forge.worker.startup import run_startup_recovery


@pytest.mark.integration
async def test_ambiguous_startup_isolates_run_and_preserves_operator_control(
    session_factory, persisted_run, command_repository, operation_repository
):
    from forge.worker.startup_intervention import StartupInterventionRecovery

    async with PostgresUnitOfWork(session_factory) as work:
        run = await work.runs.transition(
            persisted_run.id, 0, RunState.PLANNING, "test.planning", {}
        )
        other = RunSnapshot(
            id=uuid4(), project_id=run.project_id, task_id=run.task_id, policy_version=1
        )
        await work.runs.create(other)
        await work.commit()
    pending = await command_repository.enqueue(
        run_id=run.id,
        command_type="start_planning",
        idempotency_key="ambiguous-source",
        expected_run_version=run.version,
        actor_id=uuid4(),
        payload={},
    )
    ready = await command_repository.enqueue(
        run_id=other.id,
        command_type="start_planning",
        idempotency_key="unrelated-ready",
        actor_id=uuid4(),
        payload={},
    )
    request = {"resource": "unknown-effect"}
    intent = await operation_repository.begin(
        run_id=run.id,
        operation_type="unsupported_effect",
        idempotency_key="unknown-intent",
        request_digest=canonical_digest(request),
        request_payload=request,
    )
    recovery = StartupInterventionRecovery(session_factory)

    async def reconcile():
        await recovery.wait_for_owners()
        await RecoveryService(operation_repository).reconcile_all({}, allow_unresolved=True)
        await recovery.quarantine()

    assert await run_startup_recovery(
        PostgresRecoveryBarrier(session_factory), reconcile, asyncio.Event()
    )
    async with PostgresUnitOfWork(session_factory) as work:
        isolated = await work.runs.get(run.id)
        assert isolated.state is RunState.AWAITING_HUMAN_INTERVENTION
        events = [
            e
            for e in await work.events.list_after(run.id, 0)
            if e.event_type == "run.recovery_intervention"
        ]
        assert len(events) == 1
        assert events[0].payload["operation_ids"] == (str(intent.id),)
        assert (await work.commands.get(pending.id)).status is CommandStatus.PENDING
    assert (await operation_repository.list_unresolved())[0].id == intent.id
    assert (
        await command_repository.claim_next(worker_id="normal", lease_seconds=30)
    ).id == ready.id
    control = await command_repository.enqueue(
        run_id=run.id,
        command_type="cancel",
        idempotency_key="operator-cancel",
        expected_run_version=isolated.version,
        actor_id=uuid4(),
        payload={},
    )
    assert (
        await command_repository.claim_next(
            worker_id="control", lease_seconds=30, lane=CommandLane.CONTROL
        )
    ).id == control.id
    # A second startup observes the same uncertainty without duplicating its event.
    await command_repository.complete(ready.id, worker_id="normal")
    await command_repository.complete(control.id, worker_id="control")
    assert await run_startup_recovery(
        PostgresRecoveryBarrier(session_factory), reconcile, asyncio.Event()
    )
    async with PostgresUnitOfWork(session_factory) as work:
        events = [
            e
            for e in await work.events.list_after(run.id, 0)
            if e.event_type == "run.recovery_intervention"
        ]
        assert len(events) == 1


@pytest.mark.integration
async def test_worker_startup_quarantines_unknown_effect_before_dispatch(
    session_factory, persisted_run, command_repository, operation_repository, monkeypatch
):
    from types import SimpleNamespace

    from forge.worker import main

    async with PostgresUnitOfWork(session_factory) as work:
        run = await work.runs.transition(
            persisted_run.id, 0, RunState.PLANNING, "test.planning", {}
        )
        other = RunSnapshot(
            id=uuid4(), project_id=run.project_id, task_id=run.task_id, policy_version=1
        )
        await work.runs.create(other)
        await work.commit()
    for candidate in (run, other):
        await command_repository.enqueue(
            run_id=candidate.id,
            command_type="start_planning",
            idempotency_key=str(candidate.id),
            expected_run_version=candidate.version,
            actor_id=uuid4(),
            payload={},
        )
    request = {"resource": "unknown"}
    await operation_repository.begin(
        run_id=run.id,
        operation_type="unknown_effect",
        idempotency_key="unknown",
        request_payload=request,
        request_digest=canonical_digest(request),
    )
    stop = asyncio.Event()
    dispatched = []

    class Engine:
        async def dispose(self):
            pass

    async def handler(command, work):
        dispatched.append(command.run_id)
        stop.set()

    monkeypatch.setattr(main, "create_engine", lambda _: Engine())
    monkeypatch.setattr(main, "create_session_factory", lambda _: session_factory)
    await asyncio.wait_for(
        main.run_worker(
            SimpleNamespace(database_url="unused"),
            handlers={"start_planning": handler},
            stop_event=stop,
            poll_interval=0.01,
        ),
        timeout=10,
    )
    assert dispatched == [other.id]
    async with PostgresUnitOfWork(session_factory) as work:
        assert (await work.runs.get(run.id)).state is RunState.AWAITING_HUMAN_INTERVENTION


@pytest.mark.integration
async def test_intervention_waits_for_live_command_owner(
    session_factory, persisted_run, command_repository
):
    from forge.worker.startup_intervention import StartupInterventionRecovery

    command = await command_repository.enqueue(
        run_id=persisted_run.id,
        command_type="start_planning",
        idempotency_key="live-before-startup",
        payload={},
    )
    claimed = await command_repository.claim_next(worker_id="existing", lease_seconds=30)
    assert claimed.id == command.id
    barrier = PostgresRecoveryBarrier(session_factory)
    lease = await barrier.acquire(owner_id=uuid4(), lease_seconds=30)
    assert lease is not None
    recovery = StartupInterventionRecovery(session_factory)
    waiting = asyncio.create_task(recovery.wait_for_owners())
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(waiting), 0.1)
        assert (await command_repository.get(command.id)).status is CommandStatus.LEASED
        await command_repository.complete(command.id, worker_id="existing")
        await asyncio.wait_for(waiting, 2)
        assert await recovery.quarantine() == ()
        await barrier.finish(lease)
    finally:
        if not waiting.done():
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)


@pytest.mark.integration
async def test_intervention_rejects_replaced_barrier_owner(session_factory):
    from forge.persistence.repositories.recovery import RecoveryBarrierLost
    from forge.worker.startup_intervention import StartupInterventionRecovery

    barrier = PostgresRecoveryBarrier(session_factory)
    original = await barrier.acquire(owner_id=uuid4(), lease_seconds=30)
    assert original is not None
    recovery = StartupInterventionRecovery(session_factory)
    await recovery.wait_for_owners()
    await barrier.abandon(original)
    replacement = await barrier.acquire(owner_id=uuid4(), lease_seconds=30)
    assert replacement is not None
    with pytest.raises(RecoveryBarrierLost):
        await recovery.quarantine()
    await barrier.finish(replacement)


@pytest.mark.integration
@pytest.mark.parametrize("state", [RunState.PAUSED, RunState.CANCELLED, RunState.FAILED])
async def test_startup_preserves_suspended_or_terminal_business_state(
    session_factory, persisted_run, operation_repository, command_repository, state
):
    from forge.worker.startup_intervention import StartupInterventionRecovery

    async with PostgresUnitOfWork(session_factory) as work:
        run = await work.runs.transition(
            persisted_run.id, 0, RunState.PLANNING, "test.planning", {}
        )
        if state is RunState.PAUSED:
            preserved = await work.runs.pause(run.id, run.version, "test.paused", {})
        else:
            preserved = await work.runs.transition(run.id, run.version, state, "test.terminal", {})
        await work.commit()
    request = {"resource": "uncertain-before-control"}
    await operation_repository.begin(
        run_id=run.id,
        operation_type="unknown_effect",
        idempotency_key="uncertain",
        request_payload=request,
        request_digest=canonical_digest(request),
    )
    # Resume/teardown ordinarily have special eligibility for these states.
    await command_repository.enqueue(
        run_id=run.id,
        command_type="resume" if state is RunState.PAUSED else "teardown",
        idempotency_key="unsafe-followup",
        expected_run_version=preserved.version,
        payload={},
    )
    recovery = StartupInterventionRecovery(session_factory)

    async def reconcile():
        await recovery.wait_for_owners()
        await recovery.quarantine()

    assert await run_startup_recovery(
        PostgresRecoveryBarrier(session_factory), reconcile, asyncio.Event()
    )
    async with PostgresUnitOfWork(session_factory) as work:
        assert await work.runs.get(run.id) == preserved
        events = await work.events.list_after(run.id, 0)
        assert len([e for e in events if e.event_type == "run.recovery_intervention"]) == 1
    assert await command_repository.claim_next(worker_id="normal", lease_seconds=30) is None
