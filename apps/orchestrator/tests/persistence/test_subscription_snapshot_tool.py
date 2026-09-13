"""Snapshot manifests retain actual broker/attempt lineage in PostgreSQL."""

import asyncio
import json
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from forge.application.services.subscription_broker import (
    BrokerDenied,
    ControlledSubscriptionEffect,
    SubscriptionToolBroker,
)
from forge.domain.tool import ToolName
from forge.persistence.unit_of_work import PostgresUnitOfWork
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_git_commit import _case


@pytest.mark.integration
async def test_recovery_wins_against_original_blocked_snapshot_reader(
    session_factory, tmp_path, monkeypatch
):
    """A late original callback cannot overwrite a recovered terminal receipt."""
    from forge.application.services.subscription_effect_recovery import SubscriptionEffectRecovery
    from forge.application.services.tool_recovery import ToolRecoveryService
    from forge.domain.tool import ToolCallStatus
    from forge.persistence.models.scheduling import SubscriptionScheduledTask
    from sqlalchemy import update

    broker, git, attempt, store, _, authority = await _snapshot_case(session_factory, tmp_path)
    entered, release = threading.Event(), threading.Event()
    reads = 0
    original = git.working_tree_snapshot

    def blocked(*args, **kwargs):
        nonlocal reads
        reads += 1
        entered.set()
        assert release.wait(10), "test did not release the original snapshot reader"
        return original(*args, **kwargs)

    monkeypatch.setattr(git, "working_tree_snapshot", blocked)
    invocation = asyncio.create_task(
        broker.invoke(
            token="commit-token",
            provider_call_key="recovery-wins",
            tool_name=ToolName.GIT_DIFF,
            arguments={"scope": "snapshot"},
        )
    )
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        async with PostgresUnitOfWork(session_factory) as work:
            records = await work.tool_calls.list_for_run(authority.run_id)
            running = next(item for item in records if item.subscription_attempt_id == attempt)
            assert running.status is ToolCallStatus.RUNNING
            await work.session.execute(
                update(SubscriptionScheduledTask)
                .where(SubscriptionScheduledTask.task_id == authority.task_id)
                .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
            await work.commit()
        factory = lambda: PostgresUnitOfWork(session_factory)
        assert await ToolRecoveryService(factory, store).recover_all() == 1
        assert await SubscriptionEffectRecovery(factory).reconcile_all() == 1
        async with factory() as work:
            recovered = await work.tool_calls.get(running.id)
            assert recovered.status is ToolCallStatus.CANCELLED
            assert not recovered.artifact_digests
    finally:
        release.set()

    with pytest.raises(BrokerDenied):
        await invocation
    assert reads == 1
    async with PostgresUnitOfWork(session_factory) as work:
        terminal = await work.tool_calls.get(running.id)
        evidence = await work.artifacts.get_by_producer(
            run_id=authority.run_id,
            producer_type="subscription_working_tree_snapshot",
            producer_id=running.id,
        )
        assert terminal.status is ToolCallStatus.CANCELLED
        assert not terminal.artifact_digests and not evidence


@pytest.mark.integration
@pytest.mark.parametrize("failure", [False, True])
async def test_snapshot_terminal_call_replay_matches_original_receipt(
    session_factory, tmp_path, monkeypatch, failure
):
    from forge.persistence.models.scheduling import SubscriptionScheduledEffect
    from forge.persistence.models.subscription import SubscriptionOperationBinding
    from sqlalchemy import update

    broker, git, _, _, _, _ = await _snapshot_case(session_factory, tmp_path)
    if failure:
        from forge.application.ports.worktrees import SnapshotFailureReason, SnapshotReadError

        def rejected(*args, **kwargs):
            raise SnapshotReadError(SnapshotFailureReason.PATH_REJECTED)

        monkeypatch.setattr(git, "working_tree_snapshot", rejected)
    first = await broker.invoke(
        token="commit-token",
        provider_call_key="snapshot-replay",
        tool_name=ToolName.GIT_DIFF,
        arguments={"scope": "snapshot"},
    )
    if failure:
        assert first.result["status"] == "failed"
        assert first.result["metadata"] == {"snapshot_failure_reason": "path_rejected"}
    async with PostgresUnitOfWork(session_factory) as work:
        await work.session.execute(
            update(SubscriptionOperationBinding)
            .where(
                SubscriptionOperationBinding.durable_operation_id == first.operation_id,
            )
            .values(receipt_payload=None)
        )
        await work.session.execute(
            update(SubscriptionScheduledEffect)
            .where(
                SubscriptionScheduledEffect.id == first.operation_id,
            )
            .values(state="admitted")
        )
        await work.commit()

    def no_read(*args, **kwargs):
        raise AssertionError("terminal replay must not read again")

    monkeypatch.setattr(git, "working_tree_snapshot", no_read)
    replay = await broker.invoke(
        token="commit-token",
        provider_call_key="snapshot-replay",
        tool_name=ToolName.GIT_DIFF,
        arguments={"scope": "snapshot"},
    )
    assert replay == first


@pytest.mark.integration
async def test_snapshot_denial_retains_exact_invocation_key(session_factory, tmp_path):
    from uuid import uuid4

    from forge.domain.tool import ToolCallStatus, ToolRequest

    broker, _, _, _, _, authority = await _snapshot_case(session_factory, tmp_path)
    invocation = uuid4()
    context = replace(broker._effect.context, invocation_id=invocation, permitted_tools=frozenset())
    request = ToolRequest(name=ToolName.GIT_DIFF, arguments={"scope": "snapshot"})
    service = broker._effect.service
    first = await service.invoke(context, request)
    second = await service.invoke(context, request)
    assert first.status is second.status is ToolCallStatus.DENIED
    assert first.tool_call_id == second.tool_call_id == invocation
    async with PostgresUnitOfWork(session_factory) as work:
        calls = await work.tool_calls.list_for_run(authority.run_id)
        assert len(calls) == 1
        assert calls[0].request_digest is not None and calls[0].invocation_schema_version == 1


@pytest.mark.integration
@pytest.mark.parametrize("method", ["_resolve_policy", "_complete_subscription_snapshot"])
async def test_snapshot_service_normalizes_internal_failure(
    session_factory, tmp_path, monkeypatch, method
):
    from forge.application.services.subscription_broker import BrokerDenied
    from forge.application.services.tools import ToolInvocationError

    broker, _, _, _, _, _ = await _snapshot_case(session_factory, tmp_path)
    service = broker._effect.service

    async def fail(*args, **kwargs):
        raise RuntimeError("internal persistence diagnostic")

    monkeypatch.setattr(service, method, fail)
    original = service.invoke
    seen = []

    async def observe(*args, **kwargs):
        try:
            return await original(*args, **kwargs)
        except Exception as error:
            seen.append(error)
            raise

    monkeypatch.setattr(service, "invoke", observe)
    with pytest.raises(BrokerDenied):
        await broker.invoke(
            token="commit-token",
            provider_call_key="failed-snapshot",
            tool_name=ToolName.GIT_DIFF,
            arguments={"scope": "snapshot"},
        )
    assert len(seen) == 1 and isinstance(seen[0], ToolInvocationError)


@pytest.mark.integration
async def test_interrupted_snapshot_reservation_is_recovered_without_rereading(
    session_factory,
    tmp_path,
    monkeypatch,
):
    from datetime import UTC, datetime, timedelta

    from forge.application.services.subscription_broker import BrokerDenied
    from forge.application.services.subscription_effect_recovery import SubscriptionEffectRecovery
    from forge.application.services.tool_recovery import ToolRecoveryService
    from forge.domain.tool import ToolCallStatus
    from forge.persistence.models.scheduling import SubscriptionScheduledTask
    from sqlalchemy import update

    broker, git, attempt, store, _, authority = await _snapshot_case(session_factory, tmp_path)

    async def crash(*args, **kwargs):
        raise RuntimeError("simulated process loss after reservation")

    monkeypatch.setattr(broker._effect.service, "_complete_subscription_snapshot", crash)
    with pytest.raises(BrokerDenied):
        await broker.invoke(
            token="commit-token",
            provider_call_key="lost-snapshot",
            tool_name=ToolName.GIT_DIFF,
            arguments={"scope": "snapshot"},
        )
    async with PostgresUnitOfWork(session_factory) as work:
        records = await work.tool_calls.list_for_run(authority.run_id)
        call = next(item for item in records if item.subscription_attempt_id == attempt)
        assert call.status is ToolCallStatus.RUNNING
        await work.session.execute(
            update(SubscriptionScheduledTask)
            .where(
                SubscriptionScheduledTask.task_id == authority.task_id,
            )
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await work.commit()

    def no_read(*args, **kwargs):
        raise AssertionError("recovery must not rerun a snapshot")

    monkeypatch.setattr(git, "working_tree_snapshot", no_read)
    factory = lambda: PostgresUnitOfWork(session_factory)
    assert await ToolRecoveryService(factory, store).recover_all() == 1
    assert await SubscriptionEffectRecovery(factory).reconcile_all() == 1
    assert await ToolRecoveryService(factory, store).recover_all() == 0
    async with factory() as work:
        terminal = await work.tool_calls.get(call.id)
        assert terminal.status is ToolCallStatus.CANCELLED
        assert terminal.operation_intent_id is None and not terminal.artifact_digests


@pytest.mark.integration
@pytest.mark.parametrize("real_store", [False, True])
async def test_snapshot_broker_retains_verified_manifest_and_exact_replay(
    session_factory, tmp_path, real_store
):
    broker, git, attempt, store, worktree, authority = await _snapshot_case(
        session_factory, tmp_path
    )
    if real_store:
        from forge.artifacts.filesystem import FilesystemArtifactStore

        store = FilesystemArtifactStore(tmp_path / "snapshot-artifacts")
        broker._effect.service._artifact_store = store
    (worktree.path / "new.bin").write_bytes(bytes(range(256)))
    receipt = await broker.invoke(
        token="commit-token",
        provider_call_key="snapshot",
        tool_name=ToolName.GIT_DIFF,
        arguments={"scope": "snapshot"},
    )
    assert receipt.accepted and receipt.result["status"] == "succeeded"
    artifact_digest = receipt.result["artifact_digests"][0]
    manifest = json.loads(await store.open_bytes(artifact_digest))
    assert manifest["candidate_tree_digest"] == receipt.result["metadata"]["candidate_tree_digest"]
    assert manifest["changed_paths"] == ["README.md", "new.bin"]
    assert manifest["attempt_id"] == str(attempt)
    assert manifest["tool_call_id"] == str(receipt.operation_id)
    assert manifest["run_id"] == str(authority.run_id)
    (worktree.path / "new.bin").write_bytes(b"later")
    assert (
        await broker.invoke(
            token="commit-token",
            provider_call_key="snapshot",
            tool_name=ToolName.GIT_DIFF,
            arguments={"scope": "snapshot"},
        )
        == receipt
    )
    async with PostgresUnitOfWork(session_factory) as work:
        record = await work.tool_calls.get(receipt.operation_id)
        assert record.subscription_attempt_id == attempt
        assert record.artifact_digests == (artifact_digest,)
        descriptor = await work.artifacts.get_by_digest(artifact_digest, run_id=authority.run_id)
        assert descriptor.producer_id == receipt.operation_id
        assert descriptor.producer_type == "subscription_working_tree_snapshot"
    assert git.head_sha(worktree) == worktree.base_sha


async def _snapshot_case(session_factory, tmp_path):
    old, git, attempt, store, worktree = await _case(session_factory, tmp_path)
    authority = replace(old._authority, permitted_tools=frozenset({ToolName.GIT_DIFF}))
    context = replace(old._effect.context, permitted_tools=authority.permitted_tools)
    broker = SubscriptionToolBroker(
        lambda: PostgresUnitOfWork(session_factory),
        lease=old._lease,
        authority=authority,
        effect=ControlledSubscriptionEffect(old._effect.service, context),
    )
    return broker, git, attempt, store, worktree, authority


@pytest.mark.integration
@pytest.mark.parametrize("stop", ["operator", "caller"])
async def test_snapshot_read_releases_db_locks_and_drains_before_cancellation(
    session_factory, tmp_path, monkeypatch, stop
):
    import asyncio
    import threading

    from forge.domain.tool import ToolCallStatus

    broker, git, attempt, _, _, authority = await _snapshot_case(session_factory, tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = git.working_tree_snapshot

    def held(*args, **kwargs):
        entered.set()
        assert release.wait(10), "test did not release owned snapshot reader"
        return original(*args, **kwargs)

    monkeypatch.setattr(git, "working_tree_snapshot", held)
    call = asyncio.create_task(
        broker.invoke(
            token="commit-token",
            provider_call_key="snapshot-stop",
            tool_name=ToolName.GIT_DIFF,
            arguments={"scope": "snapshot"},
        )
    )
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        if stop == "operator":

            async def cancel_task():
                async with PostgresUnitOfWork(session_factory) as work:
                    await work.runs.get_for_update(authority.run_id)
                    await work.scheduler.request_stop(
                        authority.run_id, authority.task_id, cancel=True
                    )
                    await work.commit()

            await asyncio.wait_for(cancel_task(), 2)
        else:
            call.cancel()
            await asyncio.sleep(0)
            assert not call.done()
    finally:
        release.set()
    if stop == "caller":
        with pytest.raises(asyncio.CancelledError):
            await call
    else:
        receipt = await call
        assert not receipt.accepted and receipt.result["status"] == "cancelled"
    async with PostgresUnitOfWork(session_factory) as work:
        records = await work.tool_calls.list_for_run(authority.run_id)
        record = next(item for item in records if item.subscription_attempt_id == attempt)
        assert record.status is ToolCallStatus.CANCELLED
        assert not record.artifact_digests


@pytest.mark.integration
async def test_snapshot_cannot_succeed_with_unverified_artifact(session_factory, tmp_path):
    broker, _, attempt, store, _, authority = await _snapshot_case(session_factory, tmp_path)
    store.verify_returns = False
    receipt = await broker.invoke(
        token="commit-token",
        provider_call_key="bad-artifact",
        tool_name=ToolName.GIT_DIFF,
        arguments={"scope": "snapshot"},
    )
    assert receipt.result["status"] == "failed" and not receipt.result["artifact_digests"]
    async with PostgresUnitOfWork(session_factory) as work:
        records = await work.tool_calls.list_for_run(authority.run_id)
        assert len([item for item in records if item.subscription_attempt_id == attempt]) == 1


@pytest.mark.integration
async def test_operation_evidence_lookup_is_read_only_and_requires_exact_lineage(
    session_factory, tmp_path
):
    from uuid import uuid4

    broker, _, attempt_id, _, _, authority = await _snapshot_case(session_factory, tmp_path)
    receipt = await broker.invoke(
        token="commit-token",
        provider_call_key="evidence-query",
        tool_name=ToolName.GIT_DIFF,
        arguments={"scope": "snapshot"},
    )
    async with PostgresUnitOfWork(session_factory) as work:
        identity = {
            "run_id": authority.run_id,
            "task_id": authority.task_id,
            "attempt_id": attempt_id,
        }
        evidence = await work.subscription.operation_evidence(receipt.operation_id, **identity)
        assert evidence is not None
        binding, payload = evidence
        assert binding.durable_operation_id == receipt.operation_id and payload["accepted"] is True
        before = await work.tool_calls.list_for_run(authority.run_id)
        for field in identity:
            assert (
                await work.subscription.operation_evidence(
                    receipt.operation_id, **(identity | {field: uuid4()})
                )
                is None
            )
        assert await work.subscription.operation_evidence(uuid4(), **identity) is None
        assert await work.tool_calls.list_for_run(authority.run_id) == before
        await work.rollback()


@pytest.mark.integration
@pytest.mark.parametrize("role", ["primary", "independent_review"])
async def test_closed_candidate_controlled_snapshot_retains_manifest(
    session_factory, tmp_path, role
):
    from forge.domain.subscription import SpecialistPurpose
    from forge.persistence.models.scheduling import SubscriptionSchedulerRun

    broker, git, attempt, store, worktree = await _case(
        session_factory, tmp_path, closed_reader=SpecialistPurpose(role)
    )
    receipt = await broker.invoke(
        token="commit-token",
        provider_call_key="closed-snapshot",
        tool_name=ToolName.GIT_DIFF,
        arguments={"scope": "snapshot"},
    )
    assert receipt.accepted and receipt.result["status"] == "succeeded"
    digest = receipt.result["artifact_digests"][0]
    manifest = json.loads(await store.open_bytes(digest))
    assert manifest["changed_paths"] == ["README.md"]
    assert manifest["attempt_id"] == str(attempt)
    assert manifest["candidate_tree_digest"] == receipt.result["metadata"]["candidate_tree_digest"]
    assert (
        await broker.invoke(
            token="commit-token",
            provider_call_key="closed-snapshot",
            tool_name=ToolName.GIT_DIFF,
            arguments={"scope": "snapshot"},
        )
        == receipt
    )
    async with PostgresUnitOfWork(session_factory) as work:
        scheduler = await work.session.get(SubscriptionSchedulerRun, broker._authority.run_id)
        assert scheduler.candidate_state == "closed" and scheduler.candidate_epoch == 1
        call = await work.tool_calls.get(receipt.operation_id)
        assert call.artifact_digests == (digest,)
    assert git.head_sha(worktree) == worktree.base_sha
