"""Integration coverage for intent-before-effect and reconciliation."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from forge.application.services.recovery import RecoveryService
from forge.domain.operation import OperationOutcome, OperationStatus
from forge.persistence.repositories.operations import IdempotencyConflict, OperationLeaseError
from forge.persistence.repositories.runs import PersistenceDataError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

REQUEST_DIGEST = "a" * 64


def test_operation_executor_rejects_subsecond_execution_leases() -> None:
    from forge.application.services.recovery import OperationExecutor

    with pytest.raises(ValueError, match="at least 1 second"):
        OperationExecutor(object(), execution_lease_seconds=0.999)


@pytest.mark.integration
async def test_duplicate_operation_begin_returns_same_intent_and_conflicts_on_mismatch(
    operation_repository, persisted_run
) -> None:
    first = await operation_repository.begin(
        run_id=persisted_run.id,
        operation_type="github.create_pull_request",
        idempotency_key="run-1:publish-pr",
        request_digest=REQUEST_DIGEST,
        request_payload={"repository": "Clar17y/Parallel", "branch": "forge/run-1"},
    )
    second = await operation_repository.begin(
        run_id=persisted_run.id,
        operation_type="github.create_pull_request",
        idempotency_key="run-1:publish-pr",
        request_digest=REQUEST_DIGEST,
        request_payload={"branch": "forge/run-1", "repository": "Clar17y/Parallel"},
    )
    assert second.id == first.id
    assert second.request_payload == first.request_payload

    with pytest.raises(IdempotencyConflict):
        await operation_repository.begin(
            run_id=persisted_run.id,
            operation_type="github.create_pull_request",
            idempotency_key="run-1:publish-pr",
            request_digest="b" * 64,
            request_payload={"repository": "Clar17y/Parallel", "branch": "other"},
        )


@dataclass
class RecordingAdapter:
    invoke_calls: int = 0
    reconcile_calls: int = 0
    cancel_once: bool = False

    async def invoke(self, intent):
        self.invoke_calls += 1
        if self.cancel_once:
            self.cancel_once = False
            raise asyncio.CancelledError
        return OperationOutcome(
            remote_resource_id="pr:42", payload={"number": 42}, status=OperationStatus.SUCCEEDED
        )

    async def reconcile(self, intent):
        self.reconcile_calls += 1
        return OperationOutcome(
            remote_resource_id="pr:42", payload={"number": 42}, status=OperationStatus.SUCCEEDED
        )


@dataclass
class BlockedInvokeAdapter:
    """Keep the first effect open so a duplicate call must coordinate durably."""

    invoke_started: asyncio.Event
    release_invoke: asyncio.Event
    invoke_calls: int = 0
    reconcile_calls: int = 0
    reconciled_while_invoking: bool = False
    invoking: bool = False

    async def invoke(self, intent):
        self.invoke_calls += 1
        self.invoking = True
        self.invoke_started.set()
        await self.release_invoke.wait()
        self.invoking = False
        return OperationOutcome(
            remote_resource_id="pr:blocked",
            payload={"number": 43},
            status=OperationStatus.SUCCEEDED,
        )

    async def reconcile(self, intent):
        self.reconcile_calls += 1
        self.reconciled_while_invoking |= self.invoking
        return OperationOutcome(
            remote_resource_id="pr:blocked",
            payload={"number": 43},
            status=OperationStatus.SUCCEEDED,
        )


def _request(run_id):
    return {
        "run_id": run_id,
        "operation_type": "github.create_pull_request",
        "idempotency_key": f"operation:{run_id}",
        "request_digest": REQUEST_DIGEST,
        "request_payload": {"repository": "Clar17y/Parallel", "branch": "forge/run-1"},
    }


@pytest.mark.integration
async def test_operation_executor_commits_intent_before_invoke_and_short_circuits_success(
    operation_repository, persisted_run
) -> None:
    from forge.application.services.recovery import OperationExecutor

    adapter = RecordingAdapter()
    executor = OperationExecutor(operation_repository)
    outcome = await executor.execute(_request(persisted_run.id), adapter)
    assert outcome.remote_resource_id == "pr:42"
    assert adapter.invoke_calls == 1

    again = await executor.execute(_request(persisted_run.id), adapter)
    assert again == outcome
    assert adapter.invoke_calls == 1


@pytest.mark.integration
async def test_concurrent_duplicate_execute_waits_for_active_invoke(
    operation_repository, persisted_run
) -> None:
    from forge.application.services.recovery import OperationExecutor

    adapter = BlockedInvokeAdapter(asyncio.Event(), asyncio.Event())
    request = _request(persisted_run.id)
    first_executor = OperationExecutor(operation_repository)
    second_executor = OperationExecutor(operation_repository)

    first_task = asyncio.create_task(first_executor.execute(request, adapter))
    await adapter.invoke_started.wait()
    second_task = asyncio.create_task(second_executor.execute(request, adapter))

    await asyncio.sleep(0.1)
    second_finished_while_first_blocked = second_task.done()
    adapter.release_invoke.set()
    first, second = await asyncio.gather(first_task, second_task)

    assert not second_finished_while_first_blocked
    assert first == second
    assert adapter.invoke_calls == 1
    assert adapter.reconcile_calls == 0
    assert not adapter.reconciled_while_invoking


@pytest.mark.integration
async def test_cancelled_invoke_leaves_unresolved_intent_for_reconcile(
    operation_repository, persisted_run, session_factory
) -> None:
    from forge.application.services.recovery import OperationExecutor

    adapter = RecordingAdapter(cancel_once=True)
    executor = OperationExecutor(operation_repository)
    with pytest.raises(asyncio.CancelledError):
        await executor.execute(_request(persisted_run.id), adapter)

    unresolved = await operation_repository.list_unresolved()
    assert len(unresolved) == 1
    assert unresolved[0].status is OperationStatus.PENDING
    assert adapter.invoke_calls == 1

    async with session_factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE operation_intents "
                "SET execution_lease_expires_at = :expired_at "
                "WHERE id = :id"
            ),
            {
                "expired_at": datetime.now(UTC) - timedelta(minutes=1),
                "id": unresolved[0].id,
            },
        )

    recovery = RecoveryService(operation_repository)
    recovered = await recovery.reconcile(unresolved[0].id, adapter)
    assert recovered.remote_resource_id == "pr:42"
    assert recovered.status is OperationStatus.SUCCEEDED
    assert adapter.reconcile_calls == 1
    assert adapter.invoke_calls == 1


@pytest.mark.integration
async def test_recovery_reconciles_existing_intent_without_invoking(
    operation_repository, persisted_run
) -> None:
    intent = await operation_repository.begin(**_request(persisted_run.id))
    adapter = RecordingAdapter()
    recovered = await RecoveryService(operation_repository).reconcile(intent.id, adapter)
    assert recovered.remote_resource_id == "pr:42"
    assert adapter.reconcile_calls == 1
    assert adapter.invoke_calls == 0


@pytest.mark.integration
async def test_recovery_skips_an_active_execution_owner(
    operation_repository, persisted_run
) -> None:
    intent = await operation_repository.begin(
        **_request(persisted_run.id),
        execution_owner="active-worker",
        execution_lease_seconds=1,
    )
    adapter = RecordingAdapter()

    observed = await RecoveryService(operation_repository).reconcile(intent.id, adapter)

    assert observed.status is OperationStatus.PENDING
    assert observed.execution_owner == "active-worker"
    assert adapter.reconcile_calls == 0


@pytest.mark.integration
async def test_operation_outcome_requires_current_execution_owner(
    operation_repository, persisted_run
) -> None:
    intent = await operation_repository.begin(
        **_request(persisted_run.id),
        execution_owner="active-worker",
        execution_lease_seconds=1,
    )
    outcome = OperationOutcome(remote_resource_id="resource:1", payload={"ok": True})

    with pytest.raises(OperationLeaseError):
        await operation_repository.complete(intent.id, outcome, owner_id="stale-worker")


@pytest.mark.integration
async def test_expired_execution_lease_is_reconciled_without_reinvoking(
    operation_repository, persisted_run, session_factory
) -> None:
    intent = await operation_repository.begin(
        **_request(persisted_run.id),
        execution_owner="crashed-worker",
        execution_lease_seconds=1,
    )
    async with session_factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE operation_intents "
                "SET execution_lease_expires_at = :expired_at "
                "WHERE id = :id"
            ),
            {
                "expired_at": datetime.now(UTC) - timedelta(minutes=1),
                "id": intent.id,
            },
        )

    expired = await operation_repository.get(intent.id)
    assert expired.execution_lease_expires_at is not None
    assert expired.execution_lease_expires_at <= datetime.now(UTC)

    adapter = RecordingAdapter()
    recovered = await RecoveryService(operation_repository).reconcile(intent.id, adapter)

    assert recovered.status is OperationStatus.SUCCEEDED
    assert recovered.remote_resource_id == "pr:42"
    assert adapter.invoke_calls == 0
    assert adapter.reconcile_calls == 1


@pytest.mark.integration
async def test_operation_outcome_round_trip_preserves_redacted_snapshot(
    operation_repository, persisted_run
) -> None:
    request = _request(persisted_run.id)
    request["request_payload"] = {"repository": "Clar17y/Parallel"}
    intent = await operation_repository.begin(
        **request,
        execution_owner="roundtrip-worker",
        execution_lease_seconds=1,
    )
    outcome = OperationOutcome(remote_resource_id="resource:1", payload={"nested": {"ok": True}})
    stored = await operation_repository.complete(intent.id, outcome, owner_id="roundtrip-worker")
    loaded = await operation_repository.get(intent.id)
    assert stored == loaded
    assert loaded.remote_resource_id == "resource:1"
    assert loaded.outcome == {"nested": {"ok": True}}


@pytest.mark.integration
async def test_unknown_stored_operation_schema_fails_closed(
    operation_repository, persisted_run, session_factory
) -> None:
    intent = await operation_repository.begin(**_request(persisted_run.id))
    async with session_factory() as session, session.begin():
        await session.execute(
            text("UPDATE operation_intents SET request_schema_version = 2 WHERE id = :id"),
            {"id": intent.id},
        )
    with pytest.raises(PersistenceDataError):
        await operation_repository.get(intent.id)


@pytest.mark.integration
async def test_succeeded_intent_without_outcome_fails_closed(
    operation_repository, persisted_run, session_factory
) -> None:
    intent = await operation_repository.begin(**_request(persisted_run.id))
    with pytest.raises(DBAPIError):
        async with session_factory() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE operation_intents SET status = 'SUCCEEDED', completed_at = now() "
                    "WHERE id = :id"
                ),
                {"id": intent.id},
            )


@pytest.mark.integration
async def test_unresolved_intent_with_outcome_fails_closed(
    operation_repository, persisted_run, session_factory
) -> None:
    intent = await operation_repository.begin(**_request(persisted_run.id))
    with pytest.raises(DBAPIError):
        async with session_factory() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE operation_intents "
                    "SET outcome_payload = '{\"unexpected\": true}'::jsonb, "
                    "outcome_schema_version = 1 WHERE id = :id"
                ),
                {"id": intent.id},
            )


@pytest.mark.integration
async def test_operation_request_rejects_secret_assignments_inside_text(
    operation_repository, persisted_run
) -> None:
    request = _request(persisted_run.id)
    request["request_payload"] = {"description": "authorization: bearer-do-not-persist"}
    with pytest.raises(ValueError, match="raw credential"):
        await operation_repository.begin(**request)
