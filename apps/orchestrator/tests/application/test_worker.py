"""Integration coverage for one-tick worker command processing."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from forge.application.services.worker import TransientCommandError, Worker
from forge.domain.command import CommandStatus
from forge.persistence.unit_of_work import PostgresUnitOfWork


@pytest.mark.integration
async def test_worker_marks_complete_only_after_handler_uow_commit(
    command_repository, persisted_run, session_factory
) -> None:
    command = await command_repository.enqueue(
        run_id=persisted_run.id,
        command_type="start_planning",
        idempotency_key="worker:start-planning",
        payload={},
        expected_run_version=0,
        available_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    commits: list[str] = []

    async def handler(received, uow) -> None:
        assert received.id == command.id
        await uow.runs.transition(
            run_id=received.run_id,
            expected_version=received.expected_run_version,
            target="PLANNING",
            event_type="run.planning_started",
            event_payload={"command_id": str(received.id)},
        )
        commits.append("handler-returned")

    worker = Worker(
        command_repository,
        session_factory,
        handlers={"start_planning": handler},
        worker_id="worker-a",
        lease_seconds=30,
    )
    assert await worker.tick() is True
    assert commits == ["handler-returned"]

    stored = await command_repository.get(command.id)
    assert stored.status is CommandStatus.COMPLETED
    async with PostgresUnitOfWork(session_factory) as work:
        run = await work.runs.get(persisted_run.id)
        events = await work.events.list_after(persisted_run.id, 0)
    assert run.state.value == "PLANNING"
    assert [event.event_type for event in events] == ["command.started", "run.planning_started"]


@pytest.mark.integration
async def test_worker_transient_handler_failure_releases_command_for_retry(
    command_repository, persisted_run, session_factory
) -> None:
    command = await command_repository.enqueue(
        run_id=persisted_run.id,
        command_type="transient",
        idempotency_key="worker:transient",
        payload={},
        expected_run_version=0,
        available_at=datetime.now(UTC) - timedelta(seconds=1),
    )

    async def handler(_received, _uow) -> None:
        raise TransientCommandError("temporary provider failure")

    worker = Worker(
        command_repository,
        session_factory,
        handlers={"transient": handler},
        worker_id="worker-a",
        lease_seconds=30,
    )
    assert await worker.tick() is False
    stored = await command_repository.get(command.id)
    assert stored.status is CommandStatus.PENDING
    assert stored.attempt == 1
    assert stored.lease_owner is None


@pytest.mark.integration
async def test_unknown_command_fails_terminally_without_retry(
    command_repository, persisted_run, session_factory
) -> None:
    command = await command_repository.enqueue(
        run_id=persisted_run.id,
        command_type="not-registered",
        idempotency_key="worker:unknown",
        payload={},
        expected_run_version=0,
        available_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    worker = Worker(
        command_repository,
        session_factory,
        handlers={},
        worker_id="worker-a",
        lease_seconds=30,
    )
    assert await worker.tick() is False
    stored = await command_repository.get(command.id)
    assert stored.status is CommandStatus.FAILED
    assert stored.attempt == 1


@pytest.mark.integration
async def test_cancelled_handler_leaves_lease_for_expiry_reclaim(
    command_repository, persisted_run, session_factory
) -> None:
    command = await command_repository.enqueue(
        run_id=persisted_run.id,
        command_type="cancelled",
        idempotency_key="worker:cancelled",
        payload={},
        expected_run_version=0,
        available_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    started = asyncio.Event()

    async def handler(_received, _uow) -> None:
        started.set()
        await asyncio.sleep(60)

    worker = Worker(
        command_repository,
        session_factory,
        handlers={"cancelled": handler},
        worker_id="worker-a",
        lease_seconds=30,
    )
    task = asyncio.create_task(worker.tick())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    stored = await command_repository.get(command.id)
    assert stored.status is CommandStatus.LEASED
    assert stored.lease_owner == "worker-a"
    assert await command_repository.claim_next(worker_id="worker-b", lease_seconds=30) is None
