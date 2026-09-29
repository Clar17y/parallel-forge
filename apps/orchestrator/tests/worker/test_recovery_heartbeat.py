"""Compatibility reporting is best effort, bounded, and cancellation aware."""

import asyncio

import pytest
from forge.worker import main
from sqlalchemy.exc import OperationalError


@pytest.mark.parametrize("stage", ["connect", "execute", "commit"])
async def test_recovery_publication_failure_is_best_effort(stage, caplog, monkeypatch):
    monkeypatch.setattr(main.logger, "disabled", False)
    closed = []

    def fail(where):
        if stage == where:
            raise OperationalError("heartbeat", {}, OSError("password=fixture-secret"))

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            closed.append(True)

        async def execute(self, _statement):
            fail("execute")

        async def commit(self):
            fail("commit")

    def factory():
        fail("connect")
        return Session()

    await main._publish_recovery_worker(factory, "test-worker")
    assert closed == ([] if stage == "connect" else [True])
    assert "Recovery worker compatibility report unavailable" in caplog.text
    assert "fixture-secret" not in caplog.text


async def test_heartbeat_retries_failure_without_stopping_peer_polling(monkeypatch):
    monkeypatch.setattr(main, "_RECOVERY_HEARTBEAT_SECONDS", 0.001)
    stop = asyncio.Event()
    attempts, closed, peer_exits = [], [], []

    class Session:
        async def __aenter__(self):
            attempts.append(True)
            return self

        async def __aexit__(self, *_args):
            closed.append(True)

        async def execute(self, _statement):
            if len(attempts) == 1:
                raise OperationalError("heartbeat", {}, OSError("temporary disconnect"))

        async def commit(self):
            stop.set()

    async def peer():
        await stop.wait()
        peer_exits.append("stopped normally")

    tasks = [
        asyncio.create_task(main._recovery_worker_heartbeat(Session, "test-worker", stop)),
        asyncio.create_task(peer()),
    ]
    try:
        await asyncio.wait_for(asyncio.gather(*tasks), 1)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert len(attempts) == len(closed) == 2
    assert peer_exits == ["stopped normally"]


@pytest.mark.parametrize("cancel", [False, True])
async def test_publication_is_bounded_and_preserves_cancellation(cancel, caplog, monkeypatch):
    monkeypatch.setattr(main.logger, "disabled", False)
    monkeypatch.setattr(main, "_RECOVERY_PERSIST_SECONDS", 10 if cancel else 0.01)
    entered, cleaned = asyncio.Event(), asyncio.Event()

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            cleaned.set()

        async def execute(self, _statement):
            pass

        async def commit(self):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(main._publish_recovery_worker(Session, "test-worker"))
    await asyncio.wait_for(entered.wait(), 1)
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not caplog.records
    else:
        await asyncio.wait_for(task, 1)
        assert "compatibility report unavailable" in caplog.text
    assert cleaned.is_set()


async def test_stop_wakes_heartbeat_without_another_report(monkeypatch):
    stop = asyncio.Event()

    async def forbidden(*_args):
        pytest.fail("stopped heartbeat must not publish")

    monkeypatch.setattr(main, "_publish_recovery_worker", forbidden)
    task = asyncio.create_task(main._recovery_worker_heartbeat(None, "test-worker", stop))
    await asyncio.sleep(0)
    stop.set()
    await asyncio.wait_for(task, 0.1)
