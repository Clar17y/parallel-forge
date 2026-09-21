"""Diagnostic failures and shutdown cannot acquire or abandon provider authority."""

import asyncio
from uuid import UUID

import pytest
from forge.domain.subscription import ReasoningEffort, RouteSpec
from forge.worker import subscription_status as status

PRIMARY = RouteSpec(
    provider="openai",
    client="codex_app_server",
    model="gpt-6-astra",
    effort=ReasoningEffort.LOW,
)


class Store:
    def __init__(self):
        self.calls = []
        self.reported = asyncio.Event()

    async def report(self, instance, routes):
        self.calls.append(("report", instance, routes))
        self.reported.set()
        return True

    async def stop(self, instance):
        self.calls.append(("stop", instance))


@pytest.mark.asyncio
async def test_reporter_reuses_instance_and_stop_wakes_idle_wait(monkeypatch):
    monkeypatch.setattr(status, "RUNTIME_REPORT_SECONDS", 0.01)
    store = Store()
    reporter = status.SubscriptionRuntimeReporter(store, ())
    await reporter.publish()
    store.reported.clear()
    stop = asyncio.Event()
    task = asyncio.create_task(reporter.run(stop))
    await asyncio.wait_for(store.reported.wait(), 1)
    stop.set()
    await asyncio.wait_for(task, 0.1)
    await reporter.close()
    assert [call[0] for call in store.calls] == ["report", "report", "stop"]
    assert all(call[1] == reporter.instance_id for call in store.calls)
    assert isinstance(reporter.instance_id, UUID)
    assert status.SubscriptionRuntimeReporter(store, ()).instance_id != reporter.instance_id


@pytest.mark.asyncio
async def test_report_errors_are_bounded_messages_and_cancellation_propagates(caplog, monkeypatch):
    # In-process Alembic disables existing loggers; standalone worker logging is enabled.
    monkeypatch.setattr(status.logger, "disabled", False)

    class Broken(Store):
        async def report(self, *_args):
            raise ValueError("password=fixture-secret")

        async def stop(self, *_args):
            raise ValueError("password=fixture-secret")

    reporter = status.SubscriptionRuntimeReporter(Broken(), ())
    await reporter.publish()
    await reporter.close()
    assert "unavailable" in caplog.text and "fixture-secret" not in caplog.text

    class Cancelled(Store):
        async def report(self, *_args):
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await status.SubscriptionRuntimeReporter(Cancelled(), ()).publish()


@pytest.mark.asyncio
async def test_snapshot_failure_or_timeout_reports_static_fail_closed_routes(caplog, monkeypatch):
    monkeypatch.setattr(status.logger, "disabled", False)
    monkeypatch.setattr(status, "_SNAPSHOT_SECONDS", 0.001)

    async def broken():
        raise ValueError("password=fixture-secret")

    async def blocked():
        await asyncio.Event().wait()
        return ()

    for supplier in (broken, blocked):
        store = Store()
        reporter = status.SubscriptionRuntimeReporter(
            store,
            (PRIMARY,),
            snapshot_supplier=supplier,
        )
        await reporter.publish()
        assert store.calls == [("report", reporter.instance_id, frozenset((PRIMARY,)))]

    assert "snapshot unavailable" in caplog.text
    assert "fixture-secret" not in caplog.text

    async def cancelled():
        raise asyncio.CancelledError

    store = Store()
    with pytest.raises(asyncio.CancelledError):
        await status.SubscriptionRuntimeReporter(
            store,
            (PRIMARY,),
            snapshot_supplier=cancelled,
        ).publish()
    assert store.calls == []
