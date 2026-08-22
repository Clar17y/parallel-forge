"""Integration coverage for durable command idempotency and leases."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from forge.domain.command import CommandStatus
from forge.persistence.repositories.commands import (
    CommandLeaseError,
    IdempotencyConflict,
    PersistenceDataError,
)
from sqlalchemy import text


@pytest.mark.integration
async def test_duplicate_enqueue_returns_same_command_without_mutation(
    command_repository, persisted_run
) -> None:
    available_at = datetime.now(UTC) + timedelta(seconds=30)
    first = await command_repository.enqueue(
        run_id=persisted_run.id,
        command_type="start_planning",
        idempotency_key="run-1:start-planning",
        payload={"source": "operator"},
        expected_run_version=0,
        available_at=available_at,
    )
    second = await command_repository.enqueue(
        run_id=persisted_run.id,
        command_type="start_planning",
        idempotency_key="run-1:start-planning",
        payload={"source": "operator"},
        expected_run_version=0,
        available_at=available_at,
    )

    assert second.id == first.id
    assert second.payload == first.payload

    with pytest.raises(IdempotencyConflict):
        await command_repository.enqueue(
            run_id=persisted_run.id,
            command_type="start_planning",
            idempotency_key="run-1:start-planning",
            payload={"source": "different"},
            expected_run_version=0,
            available_at=available_at,
        )


@pytest.mark.integration
async def test_duplicate_enqueue_without_requested_availability_is_idempotent(
    command_repository, persisted_run
) -> None:
    first = await command_repository.enqueue(
        run_id=persisted_run.id,
        command_type="default-availability",
        idempotency_key="default-availability",
        payload={},
    )
    second = await command_repository.enqueue(
        run_id=persisted_run.id,
        command_type="default-availability",
        idempotency_key="default-availability",
        payload={},
    )
    assert second.id == first.id


@pytest.mark.integration
async def test_claim_is_exclusive_per_run_but_parallel_across_runs(
    command_repository, persisted_run, session_factory
) -> None:
    from forge.domain.run import RunSnapshot
    from forge.persistence.unit_of_work import PostgresUnitOfWork

    second_run = RunSnapshot(
        id=uuid4(), project_id=persisted_run.project_id, task_id=persisted_run.task_id
    )
    async with PostgresUnitOfWork(session_factory) as work:
        await work.runs.create(second_run)
        await work.commit()

    for index in range(2):
        await command_repository.enqueue(
            run_id=persisted_run.id,
            command_type=f"same-run-{index}",
            idempotency_key=f"same-run-{index}",
            payload={},
            expected_run_version=0,
        )
    await command_repository.enqueue(
        run_id=second_run.id,
        command_type="other-run",
        idempotency_key="other-run",
        payload={},
        expected_run_version=0,
    )

    first, second = await asyncio.gather(
        command_repository.claim_next(worker_id="worker-a", lease_seconds=30),
        command_repository.claim_next(worker_id="worker-b", lease_seconds=30),
    )
    assert first is not None
    assert second is not None
    assert len({first.run_id, second.run_id}) == 2
    assert {first.run_id, second.run_id} == {persisted_run.id, second_run.id}


@pytest.mark.integration
async def test_expired_lease_reclaims_and_unexpired_lease_is_skipped(
    command_repository, persisted_run, session_factory
) -> None:
    expired = await command_repository.enqueue(
        run_id=persisted_run.id,
        command_type="expired",
        idempotency_key="expired",
        payload={},
        expected_run_version=0,
        available_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    first = await command_repository.claim_next(worker_id="worker-a", lease_seconds=30)
    assert first is not None
    assert first.id == expired.id
    assert first.status is CommandStatus.LEASED

    assert await command_repository.claim_next(worker_id="worker-b", lease_seconds=30) is None

    await command_repository.complete(first.id, worker_id="worker-a")
    next_command = await command_repository.enqueue(
        run_id=persisted_run.id,
        command_type="reclaimable",
        idempotency_key="reclaimable",
        payload={},
        expected_run_version=0,
        available_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    leased_reclaimable = await command_repository.claim_next(worker_id="worker-c", lease_seconds=30)
    assert leased_reclaimable is not None
    assert leased_reclaimable.id == next_command.id
    async with session_factory() as session, session.begin():
        await session.execute(
            text("UPDATE run_commands SET lease_expires_at = :expired WHERE id = :id"),
            {"expired": datetime.now(UTC) - timedelta(seconds=1), "id": next_command.id},
        )
    reclaimed = await command_repository.claim_next(worker_id="worker-c", lease_seconds=30)
    assert reclaimed is not None
    assert reclaimed.id == next_command.id


@pytest.mark.integration
async def test_non_owner_cannot_renew_complete_or_fail(command_repository, persisted_run) -> None:
    command = await command_repository.enqueue(
        run_id=persisted_run.id,
        command_type="owned",
        idempotency_key="owned",
        payload={},
        expected_run_version=0,
    )
    leased = await command_repository.claim_next(worker_id="worker-a", lease_seconds=30)
    assert leased is not None

    with pytest.raises(CommandLeaseError):
        await command_repository.renew(command.id, worker_id="worker-b", lease_seconds=30)
    with pytest.raises(CommandLeaseError):
        await command_repository.complete(command.id, worker_id="worker-b")
    with pytest.raises(CommandLeaseError):
        await command_repository.fail(command.id, worker_id="worker-b", error="no")


@pytest.mark.integration
async def test_terminal_and_transient_failures_have_safe_state_transitions(
    command_repository, persisted_run
) -> None:
    transient = await command_repository.enqueue(
        run_id=persisted_run.id,
        command_type="retry",
        idempotency_key="retry",
        payload={},
        expected_run_version=0,
    )
    leased = await command_repository.claim_next(worker_id="worker-a", lease_seconds=30)
    assert leased is not None
    retried = await command_repository.fail(
        transient.id, worker_id="worker-a", error="temporary", transient=True
    )
    assert retried.status is CommandStatus.PENDING
    assert retried.attempt == 1
    assert retried.lease_owner is None

    terminal = await command_repository.enqueue(
        run_id=persisted_run.id,
        command_type="terminal",
        idempotency_key="terminal",
        payload={},
        expected_run_version=0,
        available_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    claimed = await command_repository.claim_next(worker_id="worker-a", lease_seconds=30)
    assert claimed is not None
    assert claimed.id == terminal.id
    failed = await command_repository.fail(
        terminal.id, worker_id="worker-a", error="permanent", transient=False
    )
    assert failed.status is CommandStatus.FAILED
    assert (
        await command_repository.fail(
            terminal.id, worker_id="worker-a", error="permanent", transient=False
        )
        == failed
    )


@pytest.mark.integration
async def test_unknown_stored_payload_schema_fails_closed(
    command_repository, persisted_run, session_factory
) -> None:
    command = await command_repository.enqueue(
        run_id=persisted_run.id,
        command_type="unknown-schema",
        idempotency_key="unknown-schema",
        payload={},
    )
    async with session_factory() as session, session.begin():
        await session.execute(
            text("UPDATE run_commands SET payload_schema_version = 2 WHERE id = :id"),
            {"id": command.id},
        )
    with pytest.raises(PersistenceDataError):
        await command_repository.get(command.id)


@pytest.mark.integration
async def test_command_payload_rejects_secret_assignments_inside_text(
    command_repository, persisted_run
) -> None:
    with pytest.raises(ValueError, match="raw credential"):
        await command_repository.enqueue(
            run_id=persisted_run.id,
            command_type="secret-text",
            idempotency_key="secret-text",
            payload={"description": "token=do-not-persist"},
        )


@pytest.mark.integration
async def test_claim_rejects_subsecond_leases(command_repository, persisted_run) -> None:
    with pytest.raises(ValueError, match="at least 1 second"):
        await command_repository.claim_next(worker_id="worker-a", lease_seconds=0.999)


@pytest.mark.integration
async def test_command_failure_redacts_bearer_github_token_before_persisting(
    command_repository, persisted_run
) -> None:
    command = await command_repository.enqueue(
        run_id=persisted_run.id,
        command_type="provider-failure",
        idempotency_key="provider-failure-redaction",
        payload={},
    )
    claimed = await command_repository.claim_next(worker_id="worker-a", lease_seconds=1)
    assert claimed is not None
    secret = "ghp_0123456789abcdefghijklmnopqrstuv"

    stored = await command_repository.fail(
        command.id,
        worker_id="worker-a",
        error=f"Authorization: Bearer {secret}",
        transient=False,
    )

    assert stored.error_summary is not None
    assert secret not in stored.error_summary
    assert "[REDACTED]" in stored.error_summary
    assert len(stored.error_summary) <= 1024


@pytest.mark.integration
async def test_command_cancel_redacts_credentialed_database_url_before_persisting(
    command_repository, persisted_run
) -> None:
    command = await command_repository.enqueue(
        run_id=persisted_run.id,
        command_type="cancel-with-secret",
        idempotency_key="cancel-redaction",
        payload={},
    )
    claimed = await command_repository.claim_next(worker_id="worker-a", lease_seconds=1)
    assert claimed is not None
    secret = "super-secret-password"

    stored = await command_repository.cancel(
        command.id,
        worker_id="worker-a",
        reason=f"database unavailable at postgresql://forge:{secret}@db.internal/forge",
    )

    assert stored.error_summary is not None
    assert secret not in stored.error_summary
    assert "[REDACTED]" in stored.error_summary
    assert len(stored.error_summary) <= 1024
