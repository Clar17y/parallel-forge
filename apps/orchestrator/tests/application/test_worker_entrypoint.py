"""Unit coverage for worker startup ordering and bounded idle polling."""

from __future__ import annotations

import asyncio

import pytest
from forge.application.ports.commands import CommandLane
from forge.worker import main


@pytest.fixture(autouse=True)
def terminal_recovery_stub(monkeypatch):
    from uuid import uuid4

    from forge.persistence.repositories.recovery import RecoveryLease

    class Barrier:
        def __init__(self, _factory):
            pass

        async def acquire(self, **_kwargs):
            return RecoveryLease(uuid4(), 1)

        async def renew(self, lease, **_kwargs):
            return lease

        async def finish(self, _lease):
            pass

        async def abandon(self, _lease):
            pass

    monkeypatch.setattr(main, "PostgresRecoveryBarrier", Barrier)

    class TerminalRecovery:
        def __init__(self, _factory):
            pass

        async def reconcile_all(self):
            return ()

    monkeypatch.setattr(main, "TerminalMergeRecovery", TerminalRecovery)

    class InterventionRecovery:
        def __init__(self, _factory):
            pass

        async def wait_for_owners(self):
            pass

        async def quarantine(self):
            return ()

    monkeypatch.setattr(main, "StartupInterventionRecovery", InterventionRecovery)


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_reason", ["stop", "cancel", "error"])
async def test_worker_stop_preserves_resources_until_retained_handler_finishes(
    monkeypatch, exit_reason
):
    """A stop after lease loss must not abandon the UoW-owning late handler."""
    stop, draining, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = []

    class FakeSettings:
        database_url = "postgresql+asyncpg://unused/forge"

    class FakeEngine:
        async def dispose(self):
            calls.append("dispose")

    class FakeRecovery:
        def __init__(self, _operations):
            pass

        async def reconcile_all(self, _adapters, *, allow_unresolved=False):
            return ()

    class RetainedWorker:
        def __init__(self, *_args, **_kwargs):
            pass

        async def tick(self):
            if exit_reason == "cancel":
                raise asyncio.CancelledError
            if exit_reason == "error":
                raise RuntimeError("injected tick failure")
            stop.set()
            return False

        async def drain(self):
            draining.set()
            await release.wait()
            calls.append("handler_finished")

    monkeypatch.setattr(main, "create_engine", lambda _url: FakeEngine())
    monkeypatch.setattr(main, "create_session_factory", lambda _engine: object())
    monkeypatch.setattr(main, "PostgresCommandRepository", lambda _factory: object())
    monkeypatch.setattr(main, "PostgresOperationRepository", lambda _factory: object())
    monkeypatch.setattr(main, "RecoveryService", FakeRecovery)
    monkeypatch.setattr(main, "Worker", RetainedWorker)
    running = asyncio.create_task(main.run_worker(FakeSettings(), handlers={}, stop_event=stop))
    try:
        await asyncio.wait_for(draining.wait(), 1)
        assert not running.done() and not calls
    finally:
        release.set()
        if exit_reason == "stop":
            await running
        else:
            expected = asyncio.CancelledError if exit_reason == "cancel" else RuntimeError
            with pytest.raises(expected):
                await running
    assert calls == ["handler_finished", "handler_finished", "dispose"]


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_failure", [False, True])
async def test_worker_startup_recovers_before_first_poll(monkeypatch, terminal_failure) -> None:
    calls: list[str] = []

    class TerminalRecovery:
        def __init__(self, _factory):
            pass

        async def reconcile_all(self):
            calls.append("terminal_recovery")
            if terminal_failure:
                raise main.CommandRecoveryRequired("terminal receipt differs")
            return ()

    monkeypatch.setattr(main, "TerminalMergeRecovery", TerminalRecovery)

    class FakeSettings:
        database_url = "postgresql+asyncpg://unused/forge"

    class FakeEngine:
        async def dispose(self) -> None:
            calls.append("dispose")

    class FakeRecovery:
        def __init__(self, _operations) -> None:
            pass

        async def reconcile_all(self, _adapters, *, allow_unresolved=False) -> tuple[object, ...]:
            calls.append("recovery")
            return ()

    class FakeWorker:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def drain(self) -> None:
            calls.append("drain")

        async def tick(self) -> None:
            calls.append("poll")
            stop.set()

    stop = asyncio.Event()
    monkeypatch.setattr(main, "create_engine", lambda _url: FakeEngine())
    monkeypatch.setattr(main, "create_session_factory", lambda _engine: object())
    monkeypatch.setattr(main, "PostgresCommandRepository", lambda _factory: object())
    monkeypatch.setattr(main, "PostgresOperationRepository", lambda _factory: object())
    monkeypatch.setattr(main, "RecoveryService", FakeRecovery)
    monkeypatch.setattr(main, "Worker", FakeWorker)

    if terminal_failure:
        with pytest.raises(main.CommandRecoveryRequired, match="terminal receipt"):
            await main.run_worker(FakeSettings(), adapters={}, handlers={}, stop_event=stop)
        assert calls == ["recovery", "terminal_recovery", "dispose"]
        return
    await main.run_worker(FakeSettings(), adapters={}, handlers={}, stop_event=stop)

    assert calls == ["recovery", "terminal_recovery", "poll", "drain", "drain", "dispose"]


@pytest.mark.asyncio
@pytest.mark.parametrize("recovery_failure", ["none", "operation", "tool"])
async def test_worker_default_handlers_none_constructs_real_handlers(
    monkeypatch, recovery_failure
) -> None:
    calls: list[str] = []
    lanes: list[tuple[CommandLane, str]] = []
    from forge.worker.composition import WorkerHandlers

    constructed_handlers = WorkerHandlers(
        {
            "start_planning": object(),
            "approve_plan": object(),
            "request_plan_revision": object(),
        }
    )
    recovery_adapter = object()
    constructed_handlers.recovery_adapters["controller_named_check"] = recovery_adapter

    class Tools:
        async def recover_all(self, *, allow_unresolved=False):
            calls.append("tools")
            if recovery_failure == "tool":
                raise main.RecoveryError("reconciliation failed")

    constructed_handlers.tool_recovery = Tools()

    async def close() -> None:
        calls.append("close")

    constructed_handlers.resources.push_async_callback(close)

    class FakeSettings:
        database_url = "postgresql+asyncpg://unused/forge"

    class FakeEngine:
        async def dispose(self) -> None:
            calls.append("dispose")

    class FakeRecovery:
        def __init__(self, _operations) -> None:
            pass

        async def reconcile_all(self, _adapters, *, allow_unresolved=False) -> tuple[object, ...]:
            assert _adapters == {"controller_named_check": recovery_adapter}
            calls.append("recovery")
            if recovery_failure == "operation":
                raise main.RecoveryError("reconciliation failed")
            return ()

    class FakeWorker:
        def __init__(self, _commands, _factory, *, handlers, **_kwargs) -> None:
            assert handlers == constructed_handlers
            lanes.append((_kwargs["lane"], _kwargs["worker_id"]))
            calls.append("worker_init")

        async def drain(self) -> None:
            calls.append("drain")

        async def tick(self) -> None:
            calls.append("poll")
            stop.set()

    stop = asyncio.Event()
    monkeypatch.setattr(main, "create_engine", lambda _url: FakeEngine())
    monkeypatch.setattr(main, "create_session_factory", lambda _engine: object())
    monkeypatch.setattr(main, "PostgresCommandRepository", lambda _factory: object())
    monkeypatch.setattr(main, "PostgresOperationRepository", lambda _factory: object())
    monkeypatch.setattr(main, "RecoveryService", FakeRecovery)
    monkeypatch.setattr(main, "Worker", FakeWorker)
    monkeypatch.setattr(
        main, "compose_worker_handlers", lambda _settings, _factory: constructed_handlers
    )

    if recovery_failure != "none":
        with pytest.raises(main.RecoveryError, match="reconciliation failed"):
            await main.run_worker(FakeSettings(), adapters={}, handlers=None, stop_event=stop)
        assert calls == ["recovery"] + (["tools"] if recovery_failure == "tool" else []) + [
            "close",
            "dispose",
        ]
        return
    await main.run_worker(FakeSettings(), adapters={}, handlers=None, stop_event=stop)

    assert calls == [
        "recovery",
        "tools",
        "worker_init",
        "worker_init",
        "poll",
        "drain",
        "drain",
        "close",
        "dispose",
    ]
    assert [lane for lane, _ in lanes] == [CommandLane.NORMAL, CommandLane.CONTROL]
    assert lanes[1][1] == f"{lanes[0][1]}-control"


@pytest.mark.asyncio
async def test_worker_engine_disposal_on_composition_failure(monkeypatch) -> None:
    calls: list[str] = []

    class FakeSettings:
        database_url = "postgresql+asyncpg://unused/forge"

    class FakeEngine:
        async def dispose(self) -> None:
            calls.append("dispose")

    class FakeRecovery:
        def __init__(self, _operations) -> None:
            pass

        async def reconcile_all(self, _adapters, *, allow_unresolved=False) -> tuple[object, ...]:
            calls.append("recovery")
            return ()

    def fail_compose(_settings, _factory):
        calls.append("compose_failed")
        from forge.worker.composition import WorkerCompositionError

        raise WorkerCompositionError("config missing")

    monkeypatch.setattr(main, "create_engine", lambda _url: FakeEngine())
    monkeypatch.setattr(main, "create_session_factory", lambda _engine: object())
    monkeypatch.setattr(main, "PostgresCommandRepository", lambda _factory: object())
    monkeypatch.setattr(main, "PostgresOperationRepository", lambda _factory: object())
    monkeypatch.setattr(main, "RecoveryService", FakeRecovery)
    monkeypatch.setattr(main, "compose_worker_handlers", fail_compose)

    from forge.worker.composition import WorkerCompositionError

    with pytest.raises(WorkerCompositionError, match="config missing"):
        await main.run_worker(FakeSettings(), adapters={}, handlers=None)

    assert calls == ["compose_failed", "dispose"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["close", "drain1", "drain2", None])
@pytest.mark.parametrize("caller_owned", [False, True])
async def test_handler_cleanup_ownership_and_failure_disposal(
    monkeypatch, failure, caller_owned
) -> None:
    calls: list[str] = []
    from forge.worker.composition import WorkerHandlers

    owned = WorkerHandlers({})

    async def fail_close() -> None:
        calls.append("close")
        if failure == "close":
            raise RuntimeError("close failed")

    owned.aclose = fail_close  # type: ignore[method-assign]

    class Engine:
        async def dispose(self):
            calls.append("dispose")

    class Recovery:
        def __init__(self, _):
            pass

        async def reconcile_all(self, _, *, allow_unresolved=False):
            return ()

    class Worker:
        def __init__(self, *args, **kwargs):
            pass

        async def drain(self):
            calls.append("drain")
            if failure == f"drain{calls.count('drain')}":
                raise RuntimeError(f"{failure} failed")

        async def tick(self):
            stop.set()

    class Settings:
        database_url = "unused"

    stop = asyncio.Event()
    monkeypatch.setattr(main, "create_engine", lambda _: Engine())
    monkeypatch.setattr(main, "create_session_factory", lambda _: object())
    monkeypatch.setattr(main, "PostgresCommandRepository", lambda _: object())
    monkeypatch.setattr(main, "PostgresOperationRepository", lambda _: object())
    monkeypatch.setattr(main, "RecoveryService", Recovery)
    monkeypatch.setattr(main, "Worker", Worker)
    monkeypatch.setattr(main, "compose_worker_handlers", lambda *_: owned)

    async def run():
        await main.run_worker(Settings(), handlers=owned if caller_owned else None, stop_event=stop)

    if failure and not (failure == "close" and caller_owned):
        with pytest.raises(RuntimeError, match=f"{failure} failed"):
            await run()
    else:
        await run()
    assert calls == ["drain", "drain"] + ([] if caller_owned else ["close"]) + ["dispose"]
