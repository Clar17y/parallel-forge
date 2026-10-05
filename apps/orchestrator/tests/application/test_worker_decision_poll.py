"""Decision retries contain outages, retain cleanup ownership, and expose defects."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from forge.application.ports.subscription_decisions import (
    PendingDecisionKind,
    PendingSubscriptionDecision,
)
from forge.application.services.subscription_decision_recovery import (
    SubscriptionDecisionRecovery,
    SubscriptionDecisionRecoveryReport,
)
from forge.domain.run import RunState
from forge.worker import main
from sqlalchemy.exc import (
    DataError,
    DBAPIError,
    DisconnectionError,
    IntegrityError,
    OperationalError,
    ProgrammingError,
)
from sqlalchemy.exc import TimeoutError as PoolTimeoutError


@pytest.fixture(autouse=True)
def _enable_worker_logger(monkeypatch):
    # In-process Alembic tests disable existing loggers through fileConfig.
    # Recreate the standalone worker condition where main.logger is enabled.
    monkeypatch.setattr(main.logger, "disabled", False)


async def test_decision_poll_retries_deferred_source_without_overlap():
    stop = asyncio.Event()
    calls, active = 0, 0

    async def reconcile():
        nonlocal calls, active
        active += 1
        assert active == 1
        calls += 1
        await asyncio.sleep(0)
        active -= 1
        if calls == 2:
            stop.set()
            return SubscriptionDecisionRecoveryReport(applied=1)
        return SubscriptionDecisionRecoveryReport(deferred=1)

    await asyncio.wait_for(
        main._poll_decisions(SimpleNamespace(reconcile_all=reconcile), stop, 0.01), 1
    )
    assert calls == 2


async def test_decision_poll_stops_without_another_scan():
    stop = asyncio.Event()

    async def forbidden():
        pytest.fail("stopped polling must not scan")

    polling = asyncio.create_task(
        main._poll_decisions(SimpleNamespace(reconcile_all=forbidden), stop, 60)
    )
    await asyncio.sleep(0)
    stop.set()
    await asyncio.wait_for(polling, 1)


@pytest.mark.parametrize("error", [
    RuntimeError("unexpected defect"),
    ProgrammingError("statement", {}, RuntimeError("schema differs")),
    IntegrityError("statement", {}, RuntimeError("constraint violated")),
    DataError("statement", {}, RuntimeError("invalid data")),
    DBAPIError("statement", {}, RuntimeError("unclassified database failure")),
])
async def test_decision_scan_failure_propagates(error):
    async def fail():
        raise error

    with pytest.raises(type(error)) as raised:
        await asyncio.wait_for(
            main._poll_decisions(SimpleNamespace(reconcile_all=fail), asyncio.Event(), 0.001),
            0.1,
        )
    assert raised.value is error


class PostgresFailure(Exception):
    def __init__(self, sqlstate):
        super().__init__("private connection details")
        self.sqlstate = sqlstate


@pytest.mark.parametrize("error", [
    OSError("private connection details"),
    TimeoutError("private connection details"),
    OperationalError("private statement", {}, OSError("private connection details")),
    PoolTimeoutError("private connection details"),
    DisconnectionError("private connection details"),
    DBAPIError("private statement", {}, RuntimeError("private connection details"), connection_invalidated=True),
    *(DBAPIError("private statement", {}, PostgresFailure(state)) for state in ("40001", "40P01", "55P03")),
])
async def test_decision_poll_retries_database_and_io_failures(error, caplog):
    stop = asyncio.Event()
    calls = 0

    async def reconcile():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise error
        stop.set()
        return SubscriptionDecisionRecoveryReport()

    await asyncio.wait_for(
        main._poll_decisions(SimpleNamespace(reconcile_all=reconcile), stop, 0.001), 1
    )
    assert calls == 2
    assert "Subscription decision retry unavailable; will retry" in caplog.text
    assert "private" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.parametrize(
    "failure_stage",
    [
        "pending_applications",
        "due_version",
        "role_violation",
        "diagnostic_open",
        "attempt_run_id",
        "run",
        "record_failure",
        "commit",
        "record_success",
    ],
)
async def test_decision_poll_contains_recovery_database_failure_and_retries_source(failure_stage):
    stop = asyncio.Event()
    attempt_id = uuid4()
    candidate = PendingSubscriptionDecision(attempt_id, PendingDecisionKind.WAIT)
    scans, active, closed, peer_ticks = 0, 0, 0, 0
    applying = False

    def fail_at(stage):
        if scans == 1 and failure_stage == stage:
            raise OperationalError("private statement", {}, OSError("database unavailable"))

    def operation(stage, result=None):
        async def call(*_args, **_kwargs):
            fail_at(stage)
            return result
        return AsyncMock(side_effect=call)

    async def pending(cursor, _page_size):
        fail_at("pending_applications")
        return (candidate,) if cursor is None else ()

    diagnostics = SimpleNamespace(
        due_version=operation("due_version", 1),
        role_violation=operation("role_violation"),
        attempt_run_id=operation("attempt_run_id", uuid4()),
        record_failure=operation("record_failure"),
        record_success=operation("record_success"),
    )

    @asynccontextmanager
    async def factory():
        nonlocal closed
        if applying:
            fail_at("diagnostic_open")
        try:
            yield SimpleNamespace(
                subscription_decisions=SimpleNamespace(pending_applications=pending),
                subscription_recovery=diagnostics,
                runs=SimpleNamespace(get=operation("run", SimpleNamespace(state=RunState.IMPLEMENTING))),
                rollback=AsyncMock(), commit=operation("commit"),
            )
        finally:
            closed += 1

    recovery = SubscriptionDecisionRecovery(factory, object())

    async def apply(_attempt_id):
        nonlocal applying
        applying = True
        if scans == 1 and failure_stage != "record_success":
            raise OSError("initial application outage")

    application = AsyncMock(side_effect=apply)
    recovery._decisions.apply_wait = application

    async def reconcile():
        nonlocal scans, active, applying
        scans += 1
        applying = False
        active += 1
        assert active == 1
        try:
            report = await recovery.reconcile_all()
            assert report.applied == 1
            stop.set()
            return report
        finally:
            active -= 1

    async def peer():
        nonlocal peer_ticks
        while not stop.is_set():
            peer_ticks += 1
            await asyncio.sleep(0)

    polling = asyncio.create_task(
        main._poll_decisions(SimpleNamespace(reconcile_all=reconcile), stop, 0.001)
    )
    peer_polling = asyncio.create_task(peer())
    try:
        await asyncio.wait_for(asyncio.gather(polling, peer_polling), 1)
    finally:
        stop.set()
        polling.cancel()
        peer_polling.cancel()
        await asyncio.gather(polling, peer_polling, return_exceptions=True)
    assert scans == 2 and active == 0 and closed >= 4 and peer_ticks > 1
    expected_applications = (
        1 if failure_stage in {"pending_applications", "due_version", "role_violation"} else 2
    )
    assert application.await_count == expected_applications
    assert all(call.args == (attempt_id,) for call in application.await_args_list)
    assert diagnostics.record_success.await_args.args == (attempt_id,)


async def test_decision_poll_cancellation_awaits_scan_cleanup():
    entered, cleaned = asyncio.Event(), asyncio.Event()

    async def reconcile():
        try:
            entered.set()
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    polling = asyncio.create_task(
        main._poll_decisions(SimpleNamespace(reconcile_all=reconcile), asyncio.Event(), 0.001)
    )
    await asyncio.wait_for(entered.wait(), 1)
    polling.cancel()
    with pytest.raises(asyncio.CancelledError):
        await polling
    assert cleaned.is_set()


@pytest.mark.parametrize("interval", [0, -1, float("nan"), float("inf"), True, 61])
async def test_decision_poll_interval_is_finite_and_bounded(interval):
    with pytest.raises(ValueError, match="decision retry"):
        await main._poll_decisions(object(), asyncio.Event(), interval)


async def test_stop_during_decision_scan_cancels_and_awaits_cleanup():
    stop, entered, cleaned = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def reconcile():
        try:
            entered.set()
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    polling = asyncio.create_task(
        main._poll_decisions(SimpleNamespace(reconcile_all=reconcile), stop, 0.001)
    )
    await asyncio.wait_for(entered.wait(), 1)
    stop.set()
    try:
        await asyncio.wait_for(asyncio.shield(polling), 0.1)
    finally:
        polling.cancel()
        await asyncio.gather(polling, return_exceptions=True)
    assert cleaned.is_set()


async def test_repeated_cancellation_preserves_scan_cleanup_owner():
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def reconcile():
        try:
            entered.set()
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()

    polling = asyncio.create_task(
        main._poll_decisions(SimpleNamespace(reconcile_all=reconcile), asyncio.Event(), 0.001)
    )
    await asyncio.wait_for(entered.wait(), 1)
    polling.cancel()
    await asyncio.wait_for(cleaning.wait(), 1)
    polling.cancel()
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(polling), 0.02)
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await polling
