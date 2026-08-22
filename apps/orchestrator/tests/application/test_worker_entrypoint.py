"""Unit coverage for worker startup ordering and bounded idle polling."""

from __future__ import annotations

import asyncio

import pytest
from forge.worker import main


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

    assert calls == ["recovery", "poll", "dispose"]
