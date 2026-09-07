"""Unit coverage for worker startup ordering and bounded idle polling."""

from __future__ import annotations

import asyncio

import pytest
from forge.worker import main


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

        async def reconcile_all(self, _adapters):
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
    assert calls == ["handler_finished", "dispose"]


@pytest.mark.asyncio
async def test_worker_startup_recovers_before_first_poll(monkeypatch) -> None:
    calls: list[str] = []

    class FakeSettings:
        database_url = "postgresql+asyncpg://unused/forge"

    class FakeEngine:
        async def dispose(self) -> None:
            calls.append("dispose")

    class FakeRecovery:
        def __init__(self, _operations) -> None:
            pass

        async def reconcile_all(self, _adapters) -> tuple[object, ...]:
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

    await main.run_worker(FakeSettings(), adapters={}, handlers={}, stop_event=stop)

    assert calls == ["recovery", "poll", "drain", "dispose"]


@pytest.mark.asyncio
async def test_worker_default_handlers_none_constructs_real_handlers(monkeypatch) -> None:
    calls: list[str] = []
    constructed_handlers = {
        "start_planning": object(),
        "approve_plan": object(),
        "request_plan_revision": object(),
    }

    class FakeSettings:
        database_url = "postgresql+asyncpg://unused/forge"

    class FakeEngine:
        async def dispose(self) -> None:
            calls.append("dispose")

    class FakeRecovery:
        def __init__(self, _operations) -> None:
            pass

        async def reconcile_all(self, _adapters) -> tuple[object, ...]:
            calls.append("recovery")
            return ()

    class FakeWorker:
        def __init__(self, _commands, _factory, *, handlers, **_kwargs) -> None:
            assert handlers == constructed_handlers
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

    await main.run_worker(FakeSettings(), adapters={}, handlers=None, stop_event=stop)

    assert calls == ["recovery", "worker_init", "poll", "drain", "dispose"]


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

        async def reconcile_all(self, _adapters) -> tuple[object, ...]:
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

    assert calls == ["recovery", "compose_failed", "dispose"]
