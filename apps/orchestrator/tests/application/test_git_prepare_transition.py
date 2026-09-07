from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from forge.application.services.recovery import OperationExecutor, RecoveryError
from forge.domain.operation import (
    OperationIntent,
    OperationOutcome,
    OperationStatus,
    canonical_digest,
)
from forge.persistence.repositories.operations import OperationLeaseError
from sqlalchemy import text


class _Operations:
    def __init__(self, refreshed_intent: OperationIntent | None = None) -> None:
        self.completed = 0
        self.failed = 0
        self.refreshed_intent = refreshed_intent
        self.periodic_renewed = asyncio.Event()
        self.renew_started = asyncio.Event()
        self.renew_release: asyncio.Event | None = None
        self.renewals = 0
        self.renew_error: Exception | None = None

    async def complete(self, *args: object, **kwargs: object) -> None:
        self.completed += 1

    async def fail(self, *args: object, **kwargs: object) -> None:
        self.failed += 1

    async def renew_execution(self, *args: object, **kwargs: object) -> OperationIntent:
        del args, kwargs
        self.renew_started.set()
        if self.renew_release is not None:
            await self.renew_release.wait()
        if self.renew_error is not None:
            raise self.renew_error
        self.renewals += 1
        if self.renewals > 1:
            self.periodic_renewed.set()
        assert self.refreshed_intent is not None
        return self.refreshed_intent


class _Adapter:
    def __init__(self, release: asyncio.Event | None = None) -> None:
        self.calls = 0
        self.started = asyncio.Event()
        self.release = release

    async def invoke(self, intent: OperationIntent) -> OperationOutcome:
        self.calls += 1
        self.started.set()
        if self.release is not None:
            await self.release.wait()
        return OperationOutcome(payload={"receipt": "caller-owned"})

    async def reconcile(self, intent: OperationIntent) -> OperationOutcome:
        raise AssertionError("invoke_admitted must never reconcile")


def _intent(
    *, is_new: bool = True, owner: str | None = "owner", live: bool = True
) -> OperationIntent:
    payload = {"binding": "value"}
    return OperationIntent(
        run_id=uuid4(),
        kind="git.commit.prepare.v1",
        idempotency_key=f"prepare:{uuid4()}",
        request_digest=canonical_digest(payload),
        request_payload=payload,
        execution_owner=owner,
        execution_lease_expires_at=(
            datetime.now(UTC) + timedelta(seconds=10 if live else -10) if owner else None
        ),
        is_new=is_new,
    )


@pytest.mark.asyncio
async def test_invoke_admitted_returns_outcome_without_complete_or_fail() -> None:
    intent = _intent()
    operations = _Operations(intent)
    outcome = await OperationExecutor(operations).invoke_admitted(intent, _Adapter())

    assert outcome.payload == {"receipt": "caller-owned"}
    assert operations.completed == 0
    assert operations.failed == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "intent", [_intent(is_new=False), _intent(live=False), _intent(owner=None)]
)
async def test_invoke_admitted_rejects_non_new_expired_or_unowned_before_effect(
    intent: OperationIntent,
) -> None:
    adapter = _Adapter()

    with pytest.raises(RecoveryError):
        await OperationExecutor(_Operations()).invoke_admitted(intent, adapter)

    assert adapter.calls == 0


@pytest.mark.asyncio
async def test_invoke_admitted_renews_lease_while_adapter_is_blocked() -> None:
    intent = _intent()
    operations = _Operations(intent)
    release = asyncio.Event()
    adapter = _Adapter(release)
    executor = OperationExecutor(operations, execution_lease_seconds=1)
    task = asyncio.create_task(executor.invoke_admitted(intent, adapter))
    await adapter.started.wait()
    await asyncio.wait_for(operations.periodic_renewed.wait(), timeout=2)
    release.set()

    assert (await task).status is OperationStatus.SUCCEEDED
    assert operations.completed == 0
    assert operations.failed == 0


@pytest.mark.asyncio
async def test_invoke_admitted_does_not_effect_when_initial_renewal_raises() -> None:
    intent = _intent()
    operations = _Operations(intent)
    operations.renew_error = RuntimeError("lease unavailable")
    adapter = _Adapter()

    with pytest.raises(RecoveryError, match="admitted operation lease renewal failed"):
        await OperationExecutor(operations).invoke_admitted(intent, adapter)

    assert adapter.calls == 0
    assert operations.completed == 0
    assert operations.failed == 0


@pytest.mark.asyncio
async def test_invoke_admitted_does_not_effect_when_renewed_lease_is_already_expired() -> None:
    intent = _intent()
    operations = _Operations(_intent(live=False))
    adapter = _Adapter()

    with pytest.raises(RecoveryError, match="admitted operation lease renewal failed"):
        await OperationExecutor(operations).invoke_admitted(intent, adapter)

    assert adapter.calls == 0
    assert operations.completed == 0
    assert operations.failed == 0


@pytest.mark.asyncio
async def test_invoke_admitted_cancellation_before_initial_renewal_completes_has_no_effect() -> (
    None
):
    intent = _intent()
    operations = _Operations(intent)
    operations.renew_release = asyncio.Event()
    adapter = _Adapter()
    task = asyncio.create_task(OperationExecutor(operations).invoke_admitted(intent, adapter))
    await operations.renew_started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert adapter.calls == 0
    assert operations.completed == 0
    assert operations.failed == 0


@pytest.mark.asyncio
@pytest.mark.integration
async def test_invoke_admitted_does_not_effect_when_durable_lease_was_reclaimed(
    operation_repository, persisted_run, session_factory
) -> None:
    stale_intent = await operation_repository.begin(
        **_request(
            persisted_run.id,
            kind="git.commit.publish.v1",
            key=f"publish:{uuid4()}",
        ),
        execution_owner="stale-owner",
        execution_lease_seconds=30,
    )
    async with session_factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE operation_intents "
                "SET execution_lease_expires_at = :expired_at "
                "WHERE id = :id"
            ),
            {"expired_at": datetime.now(UTC) - timedelta(minutes=1), "id": stale_intent.id},
        )
    reclaimed = await operation_repository.claim_for_recovery(
        stale_intent.id, owner_id="current-owner", lease_seconds=30
    )
    assert reclaimed.acquired

    adapter = _Adapter()
    with pytest.raises(RecoveryError, match="admitted operation lease renewal failed"):
        await OperationExecutor(operation_repository).invoke_admitted(stale_intent, adapter)

    durable = await operation_repository.get(stale_intent.id)
    assert adapter.calls == 0
    assert durable.status is OperationStatus.PENDING
    assert durable.execution_owner == "current-owner"


def _request(run_id, *, kind: str, key: str) -> dict[str, object]:
    payload = {"phase": kind}
    return {
        "run_id": run_id,
        "operation_type": kind,
        "idempotency_key": key,
        "request_digest": canonical_digest(payload),
        "request_payload": payload,
    }


@pytest.mark.asyncio
@pytest.mark.integration
async def test_caller_uow_commits_preparation_receipt_and_publication_intent_together(
    operation_repository,
    persisted_run,
    uow,
) -> None:
    preparation = await operation_repository.begin(
        **_request(persisted_run.id, kind="git.commit.prepare.v1", key=f"prepare:{uuid4()}"),
        execution_owner="prepare-owner",
        execution_lease_seconds=30,
    )
    publication_key = f"publish:{uuid4()}"
    async with uow:
        completed = await uow.operations.complete(
            preparation.id, OperationOutcome(payload={"prepared": True}), owner_id="prepare-owner"
        )
        publication = await uow.operations.begin(
            **_request(persisted_run.id, kind="git.commit.publish.v1", key=publication_key),
            execution_owner="publish-owner",
            execution_lease_seconds=30,
        )
        await uow.commit()

    assert (await operation_repository.get(preparation.id)).status is OperationStatus.SUCCEEDED
    assert (await operation_repository.get_by_idempotency_key(publication_key)).id == publication.id
    assert completed.status is OperationStatus.SUCCEEDED


@pytest.mark.asyncio
@pytest.mark.integration
async def test_caller_uow_rollback_leaves_neither_preparation_receipt_nor_publication(
    operation_repository,
    persisted_run,
    uow,
) -> None:
    preparation = await operation_repository.begin(
        **_request(persisted_run.id, kind="git.commit.prepare.v1", key=f"prepare:{uuid4()}"),
        execution_owner="prepare-owner",
        execution_lease_seconds=30,
    )
    publication_key = f"publish:{uuid4()}"
    async with uow:
        await uow.operations.complete(
            preparation.id, OperationOutcome(payload={"prepared": True}), owner_id="prepare-owner"
        )
        await uow.operations.begin(
            **_request(persisted_run.id, kind="git.commit.publish.v1", key=publication_key),
            execution_owner="publish-owner",
            execution_lease_seconds=30,
        )

    assert (await operation_repository.get(preparation.id)).status is OperationStatus.PENDING
    assert await operation_repository.get_by_idempotency_key(publication_key) is None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_wrong_preparation_owner_cannot_authorize_publication(
    operation_repository,
    persisted_run,
    uow,
) -> None:
    preparation = await operation_repository.begin(
        **_request(persisted_run.id, kind="git.commit.prepare.v1", key=f"prepare:{uuid4()}"),
        execution_owner="prepare-owner",
        execution_lease_seconds=30,
    )
    publication_key = f"publish:{uuid4()}"
    async with uow:
        with pytest.raises(OperationLeaseError):
            await uow.operations.complete(
                preparation.id, OperationOutcome(payload={"prepared": True}), owner_id="lost-owner"
            )

    assert (await operation_repository.get(preparation.id)).status is OperationStatus.PENDING
    assert await operation_repository.get_by_idempotency_key(publication_key) is None
