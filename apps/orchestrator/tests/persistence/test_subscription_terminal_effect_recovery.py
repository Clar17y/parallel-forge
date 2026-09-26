"""Recovery of terminal named-check and commit effects after receipt loss."""

import asyncio
import hashlib
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from forge.application.services.subscription_effect_recovery import SubscriptionEffectRecovery
from forge.application.services.tool_recovery import ToolRecoveryService
from forge.domain.operation import canonical_digest
from forge.domain.tool import ToolCallStatus, ToolName
from forge.persistence.models import (
    Artifact,
    ArtifactLineage,
    ArtifactLineageParent,
    OperationIntent,
    ToolCall,
)
from forge.persistence.models.scheduling import (
    SubscriptionScheduledEffect,
    SubscriptionScheduledTask,
)
from forge.persistence.models.subscription import SubscriptionOperationBinding
from forge.persistence.unit_of_work import PostgresUnitOfWork
from sqlalchemy import delete, select, update
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_git_commit import _case as _git_case
from test_subscription_named_check import _subscription_named_case


@pytest.mark.integration
async def test_terminal_artifact_io_does_not_hold_run_lock(session_factory, tmp_path):
    case, broker, _, _ = await _subscription_named_case(session_factory, tmp_path)
    receipt = await broker.invoke(
        token="named-token",
        provider_call_key="named",
        tool_name=ToolName.BUILD_RUN_NAMED_CHECK,
        arguments={"command_name": "unit"},
    )
    await _lose_broker_receipt(session_factory, receipt.operation_id)
    async with PostgresUnitOfWork(session_factory) as work:
        run_id = (await work.tool_calls.get(receipt.operation_id)).run_id
    entered, release = asyncio.Event(), asyncio.Event()
    active_verifier_uows = 0

    class TrackedVerifierUow:
        def __init__(self):
            self.inner = PostgresUnitOfWork(session_factory)

        async def __aenter__(self):
            nonlocal active_verifier_uows
            entered_uow = await self.inner.__aenter__()
            active_verifier_uows += 1
            return entered_uow

        async def __aexit__(self, *exc_info):
            nonlocal active_verifier_uows
            try:
                return await self.inner.__aexit__(*exc_info)
            finally:
                active_verifier_uows -= 1

    class BlockingStore:
        async def verify(self, digest):
            assert active_verifier_uows == 0, "artifact verification ran inside a UoW"
            entered.set()
            await release.wait()
            return await case.store.verify(digest)

        async def open_bytes(self, digest, *, max_bytes=None):
            assert active_verifier_uows == 0, "artifact reading ran inside a UoW"
            entered.set()
            await release.wait()
            return await case.store.open_bytes(digest, max_bytes=max_bytes)

        async def put_bytes(self, *args, **kwargs):
            raise AssertionError("verification must not write artifacts")

    recovery = SubscriptionEffectRecovery(
        lambda: PostgresUnitOfWork(session_factory),
        terminal_verifier=ToolRecoveryService(
            TrackedVerifierUow,
            BlockingStore(),
        ),
    )
    pending = asyncio.create_task(recovery.reconcile_all())
    try:
        await asyncio.wait_for(entered.wait(), 5)

        async def acquire_run():
            async with PostgresUnitOfWork(session_factory) as work:
                await work.runs.get_for_update(run_id)

        await asyncio.wait_for(acquire_run(), 2)
    finally:
        release.set()
        await pending
    assert pending.result() == 1
    assert await recovery.reconcile_all() == 0
    assert case.factory.calls == 1
    await _assert_rejected_receipt(session_factory, receipt.operation_id)


@pytest.mark.integration
@pytest.mark.parametrize("boundary", ["descriptor_size", "descriptor_count", "aggregate_size"])
async def test_terminal_artifact_graph_bounds_fail_before_external_io(
    session_factory, tmp_path, boundary
):
    case, broker, _, _ = await _subscription_named_case(session_factory, tmp_path)
    receipt = await broker.invoke(
        token="named-token",
        provider_call_key="named",
        tool_name=ToolName.BUILD_RUN_NAMED_CHECK,
        arguments={"command_name": "unit"},
    )
    await _lose_broker_receipt(session_factory, receipt.operation_id)
    run_id, artifact_id, _ = await _terminal_artifact(session_factory, receipt.operation_id)
    if boundary == "descriptor_size":
        async with session_factory() as session, session.begin():
            artifact = await session.get(Artifact, artifact_id)
            assert artifact is not None
            artifact.size_bytes = 8 * 1024 * 1024 + 1
    elif boundary == "descriptor_count":
        await _attach_artifact_parents(
            session_factory, run_id=run_id, child_id=artifact_id, count=20, byte_count=0
        )
    else:
        await _attach_artifact_parents(
            session_factory,
            run_id=run_id,
            child_id=artifact_id,
            count=4,
            byte_count=8 * 1024 * 1024,
        )

    class NoIoStore:
        calls = 0

        async def verify(self, digest):
            self.calls += 1
            raise AssertionError("bounded descriptor graph must fail before artifact IO")

        async def open_bytes(self, digest, *, max_bytes=None):
            self.calls += 1
            raise AssertionError("bounded descriptor graph must fail before artifact IO")

        async def put_bytes(self, *args, **kwargs):
            raise AssertionError("verification must not write artifacts")

    store = NoIoStore()
    recovery = SubscriptionEffectRecovery(
        lambda: PostgresUnitOfWork(session_factory),
        terminal_verifier=ToolRecoveryService(
            lambda: PostgresUnitOfWork(session_factory), store
        ),
    )
    assert await recovery.reconcile_all() == 0
    assert store.calls == 0 and case.factory.calls == 1
    await _assert_fenced(session_factory, receipt.operation_id)


@pytest.mark.integration
async def test_terminal_descriptor_change_after_prefetch_invalidates_proof(
    session_factory, tmp_path
):
    case, broker, _, _ = await _subscription_named_case(session_factory, tmp_path)
    receipt = await broker.invoke(
        token="named-token",
        provider_call_key="named",
        tool_name=ToolName.BUILD_RUN_NAMED_CHECK,
        arguments={"command_name": "unit"},
    )
    await _lose_broker_receipt(session_factory, receipt.operation_id)
    _, artifact_id, _ = await _terminal_artifact(session_factory, receipt.operation_id)

    class RacingRecovery(ToolRecoveryService):
        async def _load_terminal_artifact_bytes(self, effect_id):
            loaded = await super()._load_terminal_artifact_bytes(effect_id)
            assert loaded is not None
            async with session_factory() as session, session.begin():
                artifact = await session.get(Artifact, artifact_id)
                assert artifact is not None
                artifact.size_bytes += 1
            return loaded

    recovery = SubscriptionEffectRecovery(
        lambda: PostgresUnitOfWork(session_factory),
        terminal_verifier=RacingRecovery(
            lambda: PostgresUnitOfWork(session_factory), case.store
        ),
    )
    assert await recovery.reconcile_all() == 0
    assert case.factory.calls == 1
    await _assert_fenced(session_factory, receipt.operation_id)


def _head(repository) -> str:
    result = subprocess.run(
        [shutil.which("git") or "git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        shell=False,
        text=True,
    )
    return result.stdout.strip()


async def _lose_broker_receipt(session_factory, effect_id, *, state="admitted"):
    async with session_factory() as session, session.begin():
        call = await session.get(ToolCall, effect_id)
        assert call is not None and call.status == ToolCallStatus.SUCCEEDED.value.upper()
        await session.execute(
            update(SubscriptionOperationBinding)
            .where(SubscriptionOperationBinding.durable_operation_id == effect_id)
            .values(receipt_payload=None)
        )
        await session.execute(
            update(SubscriptionScheduledEffect)
            .where(SubscriptionScheduledEffect.id == effect_id)
            .values(state=state)
        )
        await session.execute(
            update(SubscriptionScheduledTask)
            .where(SubscriptionScheduledTask.task_id == call.subscription_task_id)
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )


async def _assert_rejected_receipt(session_factory, effect_id):
    async with session_factory() as session:
        effect = await session.get(SubscriptionScheduledEffect, effect_id)
        binding = await session.scalar(
            select(SubscriptionOperationBinding).where(
                SubscriptionOperationBinding.durable_operation_id == effect_id
            )
        )
        call = await session.get(ToolCall, effect_id)
        assert effect is not None and effect.state == "rejected"
        assert call is not None and call.status == ToolCallStatus.SUCCEEDED.value.upper()
        assert binding is not None and binding.receipt_payload is not None
        assert binding.receipt_payload["accepted"] is False


async def _assert_fenced(session_factory, effect_id):
    async with session_factory() as session:
        effect = await session.get(SubscriptionScheduledEffect, effect_id)
        binding = await session.scalar(
            select(SubscriptionOperationBinding).where(
                SubscriptionOperationBinding.durable_operation_id == effect_id
            )
        )
        assert effect is not None and effect.state == "admitted"
        assert binding is not None and binding.receipt_payload is None


async def _terminal_artifact(session_factory, effect_id):
    async with session_factory() as session:
        call = await session.get(ToolCall, effect_id)
        assert call is not None and call.result_metadata is not None
        digests = call.result_metadata["artifact_digests"]
        assert isinstance(digests, list) and digests
        artifact = await session.scalar(select(Artifact).where(Artifact.digest == digests[0]))
        assert artifact is not None
        return call.run_id, artifact.id, artifact.digest


async def _attach_artifact_parents(
    session_factory, *, run_id, child_id, count, byte_count
):
    async with session_factory() as session, session.begin():
        for index in range(count):
            digest = hashlib.sha256(f"extra-{index}".encode()).hexdigest()
            artifact = Artifact(
                digest=digest,
                media_type="application/octet-stream",
                storage_pointer=f"sha256/{digest[:2]}/{digest[2:]}.blob",
                size_bytes=byte_count,
                metadata_schema_version=1,
                artifact_metadata={},
            )
            session.add(artifact)
            await session.flush()
            session.add(
                ArtifactLineage(
                    artifact_id=artifact.id,
                    run_id=run_id,
                    producer_kind="test-bound-parent",
                    producer_id=None,
                )
            )
            await session.flush()
            session.add(
                ArtifactLineageParent(
                    artifact_id=child_id,
                    run_id=run_id,
                    parent_artifact_id=artifact.id,
                )
            )


@pytest.mark.integration
@pytest.mark.parametrize("state", ["admitted", "reconciling"])
async def test_actual_named_check_terminal_proof_recovers_receipt_once(
    session_factory, tmp_path, state
):
    case, broker, _, _ = await _subscription_named_case(session_factory, tmp_path)
    receipt = await broker.invoke(
        token="named-token",
        provider_call_key="named",
        tool_name=ToolName.BUILD_RUN_NAMED_CHECK,
        arguments={"command_name": "unit"},
    )
    assert receipt.accepted and case.factory.calls == 1
    await _lose_broker_receipt(session_factory, receipt.operation_id, state=state)
    recovery = SubscriptionEffectRecovery(
        lambda: PostgresUnitOfWork(session_factory),
        terminal_verifier=ToolRecoveryService(
            lambda: PostgresUnitOfWork(session_factory), case.store
        ),
    )
    assert await recovery.reconcile_all() == 1
    assert await recovery.reconcile_all() == 0
    assert case.factory.calls == 1
    await _assert_rejected_receipt(session_factory, receipt.operation_id)


@pytest.mark.integration
async def test_actual_git_commit_terminal_proof_recovers_receipt_without_second_commit(
    session_factory, tmp_path
):
    broker, _, _, store, worktree = await _git_case(session_factory, tmp_path)
    receipt = await broker.invoke(
        token="commit-token",
        provider_call_key="commit",
        tool_name=ToolName.GIT_COMMIT,
        arguments={"message": "feat: recover terminal commit"},
    )
    assert receipt.accepted
    head = _head(worktree.path)
    await _lose_broker_receipt(session_factory, receipt.operation_id)
    recovery = SubscriptionEffectRecovery(
        lambda: PostgresUnitOfWork(session_factory),
        terminal_verifier=ToolRecoveryService(lambda: PostgresUnitOfWork(session_factory), store),
    )
    assert await recovery.reconcile_all() == 1
    assert await recovery.reconcile_all() == 0
    assert _head(worktree.path) == head
    await _assert_rejected_receipt(session_factory, receipt.operation_id)


@pytest.mark.integration
async def test_terminal_tool_without_verifier_remains_fenced(session_factory, tmp_path):
    case, broker, _, _ = await _subscription_named_case(session_factory, tmp_path)
    receipt = await broker.invoke(
        token="named-token",
        provider_call_key="named",
        tool_name=ToolName.BUILD_RUN_NAMED_CHECK,
        arguments={"command_name": "unit"},
    )
    await _lose_broker_receipt(session_factory, receipt.operation_id)
    recovery = SubscriptionEffectRecovery(lambda: PostgresUnitOfWork(session_factory))
    assert await recovery.reconcile_all() == 0
    assert case.factory.calls == 1
    await _assert_fenced(session_factory, receipt.operation_id)


@pytest.mark.integration
@pytest.mark.parametrize(
    "corruption", ["operation_lineage", "operation_outcome", "artifact_bytes", "missing_terminal"]
)
async def test_named_check_unproved_terminal_evidence_remains_fenced(
    session_factory, tmp_path, corruption
):
    case, broker, _, _ = await _subscription_named_case(session_factory, tmp_path)
    receipt = await broker.invoke(
        token="named-token",
        provider_call_key="named",
        tool_name=ToolName.BUILD_RUN_NAMED_CHECK,
        arguments={"command_name": "unit"},
    )
    await _lose_broker_receipt(session_factory, receipt.operation_id)
    async with session_factory() as session, session.begin():
        call = await session.get(ToolCall, receipt.operation_id)
        operation = await session.get(OperationIntent, receipt.operation_id)
        assert call is not None and operation is not None
        if corruption == "operation_lineage":
            payload = dict(operation.request_payload)
            payload["subscription_attempt_id"] = str(uuid4())
            operation.request_payload = payload
            operation.request_digest = canonical_digest(payload)
        elif corruption == "operation_outcome":
            outcome = dict(operation.outcome_payload or {})
            outcome["exit_code"] = 77
            operation.outcome_payload = outcome
        elif corruption == "artifact_bytes":
            assert call.result_metadata is not None
            digests = call.result_metadata["artifact_digests"]
            assert isinstance(digests, list) and digests
            case.store.stored[digests[0]] = b"tampered"
        else:
            await session.execute(delete(ToolCall).where(ToolCall.id == receipt.operation_id))
    recovery = SubscriptionEffectRecovery(
        lambda: PostgresUnitOfWork(session_factory),
        terminal_verifier=ToolRecoveryService(
            lambda: PostgresUnitOfWork(session_factory), case.store
        ),
    )
    assert await recovery.reconcile_all() == 0
    assert case.factory.calls == 1
    await _assert_fenced(session_factory, receipt.operation_id)


@pytest.mark.integration
async def test_terminal_proof_is_rechecked_against_locked_settlement_snapshot(
    session_factory, tmp_path
):
    case, broker, _, _ = await _subscription_named_case(session_factory, tmp_path)
    receipt = await broker.invoke(
        token="named-token",
        provider_call_key="named",
        tool_name=ToolName.BUILD_RUN_NAMED_CHECK,
        arguments={"command_name": "unit"},
    )
    await _lose_broker_receipt(session_factory, receipt.operation_id)
    verifier = ToolRecoveryService(lambda: PostgresUnitOfWork(session_factory), case.store)

    class RacingVerifier:
        async def verify_terminal_effect(self, effect_id):
            proof = await verifier.verify_terminal_effect(effect_id)
            assert proof is not None
            async with session_factory() as session, session.begin():
                operation = await session.get(OperationIntent, effect_id)
                assert operation is not None
                outcome = dict(operation.outcome_payload or {})
                outcome["exit_code"] = 78
                operation.outcome_payload = outcome
            return proof

    recovery = SubscriptionEffectRecovery(
        lambda: PostgresUnitOfWork(session_factory),
        terminal_verifier=RacingVerifier(),
    )
    assert await recovery.reconcile_all() == 0
    assert case.factory.calls == 1
    await _assert_fenced(session_factory, receipt.operation_id)
