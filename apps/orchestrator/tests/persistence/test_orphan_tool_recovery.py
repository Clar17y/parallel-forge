"""Durable orphan controlled-tool settlement coverage."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from forge.application.services.tool_recovery import ToolRecoveryDisposition, ToolRecoveryService
from forge.application.services.tools import ToolInvocationError
from forge.domain.artifact import canonical_storage_pointer
from forge.domain.operation import canonical_digest
from forge.domain.tool import ToolCallStatus, ToolName, ToolRequest
from forge.persistence.models import OperationIntent as OperationRow
from forge.persistence.unit_of_work import PostgresUnitOfWork
from sqlalchemy.orm.attributes import flag_modified

from apps.orchestrator.tests.persistence.test_tool_write_invocation import (
    _ControlledArtifactStore,
    _ControlledWriter,
    _seed_test_database,
    _setup_service,
)


async def _orphan_case(session_factory, tmp_path):
    project, run, step, execution, base, branch, repo, path, _ = await _seed_test_database(
        session_factory, tmp_path
    )
    writer = _ControlledWriter(Path(path))
    store = _ControlledArtifactStore(tmp_path / "artifacts")
    service, context, _ = _setup_service(
        session_factory, project, run, branch, base, repo, path, writer, store
    )
    context = replace(context, step_id=step, agent_execution_id=execution)
    request = ToolRequest(
        name=ToolName.REPOSITORY_WRITE_FILE,
        arguments={"path": "recovery.txt", "content": "durable effect\n"},
    )
    store.verify_returns = False
    with pytest.raises(ToolInvocationError):
        await service.invoke(context, request)
    store.verify_returns = True
    recovery = ToolRecoveryService(lambda: PostgresUnitOfWork(session_factory), store)
    return SimpleNamespace(
        recovery=recovery,
        context=context,
        service=service,
        request=request,
        writer=writer,
        store=store,
        run=run,
    )


async def test_completed_write_recovers_canonical_receipt_once(session_factory, tmp_path):
    case = await _orphan_case(session_factory, tmp_path)
    recovery, context, service, request, writer, run = (
        case.recovery,
        case.context,
        case.service,
        case.request,
        case.writer,
        case.run,
    )
    result = await recovery.recover_one(context.invocation_id)
    assert result.disposition is ToolRecoveryDisposition.SETTLED
    assert (
        await recovery.recover_one(context.invocation_id)
    ).disposition is ToolRecoveryDisposition.TERMINAL
    replay = await service.invoke(context, request)
    assert replay.status is ToolCallStatus.SUCCEEDED
    assert writer.call_count == 1
    async with PostgresUnitOfWork(session_factory) as work:
        record = await work.tool_calls.get(context.invocation_id)
        assert record.artifact_digests == replay.artifact_digests
        events = await work.events.list_after(run, 0)
        terminal = [
            event for event in events if event.payload.get("tool_call_id") == str(record.id)
        ]
        assert len(terminal) == 1
        assert tuple(terminal[0].payload["artifact_digests"]) == record.artifact_digests
    assert await recovery.recover_page(None, 10) == ()


async def test_startup_finalizes_orphan_tool_receipts_before_dispatch(session_factory, tmp_path):
    case = await _orphan_case(session_factory, tmp_path)
    assert await case.recovery.recover_all() == 1
    assert await case.recovery.recover_all() == 0
    async with PostgresUnitOfWork(session_factory) as work:
        call = await work.tool_calls.get(case.context.invocation_id)
        assert call.status is ToolCallStatus.SUCCEEDED
        assert len(call.artifact_digests) == 1
    assert case.writer.call_count == 1


async def test_startup_tool_recovery_refuses_invalid_evidence(session_factory, tmp_path):
    from forge.application.services.recovery import RecoveryError

    case = await _orphan_case(session_factory, tmp_path)
    async with PostgresUnitOfWork(session_factory) as work:
        call = await work.tool_calls.get(case.context.invocation_id)
    async with session_factory() as session, session.begin():
        intent = await session.get(OperationRow, call.operation_intent_id)
        intent.outcome_payload = {}
    with pytest.raises(RecoveryError, match="unresolved evidence"):
        await case.recovery.recover_all()
    async with PostgresUnitOfWork(session_factory) as work:
        assert (await work.tool_calls.get(call.id)).status is ToolCallStatus.RUNNING
    assert case.writer.call_count == 1


async def test_write_recovery_adapter_needs_only_digest_and_cannot_write(session_factory, tmp_path):
    from forge.application.services.tools import (
        _RepositoryWriteOperationAdapter,
        _RepositoryWriteOperationError,
    )

    case = await _orphan_case(session_factory, tmp_path)
    async with PostgresUnitOfWork(session_factory) as work:
        call = await work.tool_calls.get(case.context.invocation_id)
        intent = await work.operations.get(call.operation_intent_id)
        run = await work.runs.get(case.run)
        assert ToolRecoveryService.valid_write_request(call, intent, run)
    adapter = _RepositoryWriteOperationAdapter.for_recovery(
        case.writer,
        path=intent.request_payload["path"],
        content_digest=intent.request_payload["content_digest"],
        byte_count=intent.request_payload["content_byte_count"],
    )
    with pytest.raises(_RepositoryWriteOperationError):
        await adapter.invoke(intent)
    result = await adapter.reconcile(intent)
    assert result.payload["output_digest"] == intent.request_payload["content_digest"]
    assert case.writer.call_count == 1


async def test_startup_write_registry_binds_tool_authority_before_inspection(
    session_factory, tmp_path, monkeypatch
):
    from forge.application.ports.worktrees import ManagedWorktree
    from forge.application.services.recovery import RecoveryError
    from forge.domain.policy import ProjectPolicy
    from forge.domain.resource import WorktreeIdentity
    from forge.worker import recovery_adapters

    case = await _orphan_case(session_factory, tmp_path)
    async with PostgresUnitOfWork(session_factory) as work:
        run = await work.runs.get(case.run)
        policy = ProjectPolicy.model_validate(
            (await work.projects.get_policy(run.project_id, run.policy_version)).document
        )
        call = await work.tool_calls.get(case.context.invocation_id)
        intent = await work.operations.get(call.operation_intent_id)
    tree = ManagedWorktree(
        identity=WorktreeIdentity.for_run(
            run.project_id, run.id, run.branch_name, policy.database.enabled
        ),
        path=Path(run.worktree_path),
        base_sha=run.base_sha,
    )

    # This fixture predates plan approvals. The separate real-plan startup
    # integration covers that loader; here exercise the persisted tool binding.
    async def approved(*_args):
        return SimpleNamespace(run=run, policy=policy)

    monkeypatch.setattr(recovery_adapters.ApprovedPlanLoader, "load", approved)
    inspected = []

    class Git:
        def inspect_worktree(self, identity, base_sha):
            assert identity == tree.identity and base_sha == tree.base_sha
            inspected.append("git")
            return tree

    def writer(*_args):
        inspected.append("writer")
        return case.writer

    adapter = recovery_adapters.local_recovery_adapters(
        session_factory, case.store, lambda _: Git(), writer
    )[ToolName.REPOSITORY_WRITE_FILE.value]
    changed = dict(intent.request_payload) | {"policy_version": policy.version + 1}
    with pytest.raises(RecoveryError, match="write authority"):
        await adapter.reconcile(
            replace(intent, request_payload=changed, request_digest=canonical_digest(changed))
        )
    assert inspected == []
    result = await adapter.reconcile(intent)
    assert result.payload["output_digest"] == intent.request_payload["content_digest"]
    assert inspected == ["git", "writer"] and case.writer.call_count == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("agent_execution_id", str(uuid4())),
        ("step_id", str(uuid4())),
        ("run_id", str(uuid4())),
        ("project_id", str(uuid4())),
        ("policy_version", True),
        ("policy_version", 2),
    ],
)
async def test_recovery_rejects_cross_scope_operation(session_factory, tmp_path, field, value):
    case = await _orphan_case(session_factory, tmp_path)
    async with PostgresUnitOfWork(session_factory) as work:
        call = await work.tool_calls.get(case.context.invocation_id)
    async with session_factory() as session, session.begin():
        row = await session.get(OperationRow, call.operation_intent_id)
        payload = dict(row.request_payload)
        payload[field] = value
        row.request_payload = payload
        row.request_digest = canonical_digest(payload)
    result = await case.recovery.recover_one(call.id)
    assert result.disposition is ToolRecoveryDisposition.INTERVENTION
    async with PostgresUnitOfWork(session_factory) as work:
        current = await work.tool_calls.get(call.id)
        assert current == call
        assert (
            await work.artifacts.get_by_producer(
                run_id=case.run, producer_type="controlled_tool", producer_id=call.id
            )
            == ()
        )
    assert case.writer.call_count == 1


async def test_concurrent_recovery_settles_once(session_factory, tmp_path):
    case = await _orphan_case(session_factory, tmp_path)
    outcomes = await asyncio.gather(
        *(case.recovery.recover_one(case.context.invocation_id) for _ in range(3))
    )
    assert sorted(outcome.disposition for outcome in outcomes) == sorted(
        [
            ToolRecoveryDisposition.SETTLED,
            ToolRecoveryDisposition.TERMINAL,
            ToolRecoveryDisposition.TERMINAL,
        ]
    )
    async with PostgresUnitOfWork(session_factory) as work:
        events = await work.events.list_after(case.run, 0)
        assert (
            sum(
                event.payload.get("tool_call_id") == str(case.context.invocation_id)
                for event in events
            )
            == 1
        )
    assert case.writer.call_count == 1


@pytest.mark.parametrize(
    "mutation",
    [
        "kind",
        "digest",
        "extra_request",
        "outcome_digest",
        "outcome_count",
        "outcome_created",
        "outcome_extra",
        "reconciled_marker",
    ],
)
async def test_recovery_rejects_malformed_effect_proof(session_factory, tmp_path, mutation):
    case = await _orphan_case(session_factory, tmp_path)
    async with PostgresUnitOfWork(session_factory) as work:
        call = await work.tool_calls.get(case.context.invocation_id)
    async with session_factory() as session, session.begin():
        row = await session.get(OperationRow, call.operation_intent_id)
        if mutation == "kind":
            row.operation_kind = "different.effect"
        elif mutation == "digest":
            row.request_digest = "0" * 64
        elif mutation == "extra_request":
            row.request_payload = {**row.request_payload, "extra": "unbound"}
            row.request_digest = canonical_digest(row.request_payload)
        else:
            change = {
                "outcome_digest": {"output_digest": "0" * 64},
                "outcome_count": {"byte_count": True},
                "outcome_created": {"created": False},
                "outcome_extra": {"extra": "unbound"},
                "reconciled_marker": {"reconciled": 0},
            }[mutation]
            row.outcome_payload = {**row.outcome_payload, **change}
            # Python considers False == 0; force the malformed JSON type into SQL.
            flag_modified(row, "outcome_payload")
    assert (
        await case.recovery.recover_one(call.id)
    ).disposition is ToolRecoveryDisposition.INTERVENTION
    async with PostgresUnitOfWork(session_factory) as work:
        assert await work.tool_calls.get(call.id) == call
    assert case.writer.call_count == 1


@pytest.mark.parametrize("terminal_status", [None, "CANCELLED", "FAILED", "SUCCEEDED"])
async def test_recovery_preserves_terminal_history_on_cancelled_run(
    session_factory, tmp_path, terminal_status
):
    from datetime import UTC, datetime

    from forge.persistence.models import Run, ToolCall

    case = await _orphan_case(session_factory, tmp_path)
    async with session_factory() as session, session.begin():
        run = await session.get(Run, case.run)
        run.state = "CANCELLED"
        if terminal_status is not None:
            call = await session.get(ToolCall, case.context.invocation_id)
            call.status = terminal_status
            call.completed_at = datetime.now(UTC)
    async with PostgresUnitOfWork(session_factory) as work:
        before = await work.tool_calls.get(case.context.invocation_id)
    result = await case.recovery.recover_one(case.context.invocation_id)
    expected = (
        ToolRecoveryDisposition.SETTLED
        if terminal_status is None
        else ToolRecoveryDisposition.TERMINAL
    )
    assert result.disposition is expected
    async with PostgresUnitOfWork(session_factory) as work:
        after = await work.tool_calls.get(case.context.invocation_id)
        if terminal_status is not None:
            assert after == before
        else:
            assert after.status is ToolCallStatus.SUCCEEDED
    assert case.writer.call_count == 1


@pytest.mark.parametrize("boundary", ["before_lineage", "after_lineage", "after_audit"])
async def test_recovery_retry_after_settlement_interruption(session_factory, tmp_path, boundary):
    case = await _orphan_case(session_factory, tmp_path)

    class InterruptedWork(PostgresUnitOfWork):
        async def __aenter__(self):
            await super().__aenter__()
            target = self.events if boundary == "after_audit" else self.artifacts
            name = "append" if boundary == "after_audit" else "record"
            original = getattr(target, name)

            async def interrupted(*args, **kwargs):
                if boundary != "before_lineage":
                    await original(*args, **kwargs)
                raise RuntimeError("injected settlement interruption")

            setattr(target, name, interrupted)
            return self

    interrupted = ToolRecoveryService(lambda: InterruptedWork(session_factory), case.store)
    with pytest.raises(RuntimeError, match="injected settlement interruption"):
        await interrupted.recover_one(case.context.invocation_id)
    async with PostgresUnitOfWork(session_factory) as work:
        call = await work.tool_calls.get(case.context.invocation_id)
        assert call.status is ToolCallStatus.RUNNING
        assert (
            await work.artifacts.get_by_producer(
                run_id=case.run, producer_type="controlled_tool", producer_id=call.id
            )
            == ()
        )
        events = await work.events.list_after(case.run, 0)
        assert not any(event.payload.get("tool_call_id") == str(call.id) for event in events)
    assert (await case.recovery.recover_one(call.id)).disposition is ToolRecoveryDisposition.SETTLED
    assert (
        await case.service.invoke(case.context, case.request)
    ).status is ToolCallStatus.SUCCEEDED
    assert case.writer.call_count == 1


@pytest.mark.parametrize("corruption", ["blob", "descriptor_count", "descriptor_digest"])
async def test_recovery_rejects_artifact_store_substitution(session_factory, tmp_path, corruption):
    case = await _orphan_case(session_factory, tmp_path)
    original_put = case.store.put_bytes
    original_open = case.store.open_bytes

    async def substituted_put(*args, **kwargs):
        descriptor = await original_put(*args, **kwargs)
        if corruption == "descriptor_count":
            return replace(
                descriptor,
                byte_count=descriptor.byte_count + 1,
                original_byte_count=descriptor.byte_count + 1,
            )
        if corruption == "descriptor_digest":
            return replace(
                descriptor, digest="0" * 64, storage_path=Path(canonical_storage_pointer("0" * 64))
            )
        return descriptor

    async def substituted_open(digest):
        return b"substituted receipt" if corruption == "blob" else await original_open(digest)

    case.store.put_bytes = substituted_put
    case.store.open_bytes = substituted_open
    with pytest.raises(RuntimeError, match="artifact verification failed"):
        await case.recovery.recover_one(case.context.invocation_id)
    async with PostgresUnitOfWork(session_factory) as work:
        call = await work.tool_calls.get(case.context.invocation_id)
        assert call.status is ToolCallStatus.RUNNING
        assert call.artifact_digests == ()
        events = await work.events.list_after(case.run, 0)
        assert not any(event.payload.get("tool_call_id") == str(call.id) for event in events)
    assert case.writer.call_count == 1


async def test_recovery_retains_reconciled_write_receipt_without_inventing_creation(
    session_factory, tmp_path
):
    from forge.application.services.tools import _file_write

    case = await _orphan_case(session_factory, tmp_path)
    async with PostgresUnitOfWork(session_factory) as work:
        call = await work.tool_calls.get(case.context.invocation_id)
        operation = await work.operations.get(call.operation_intent_id)
    observed = case.writer.inspect_file(
        operation.request_payload["path"], operation.request_payload["content_digest"]
    )
    assert observed is not None
    async with session_factory() as session, session.begin():
        row = await session.get(OperationRow, call.operation_intent_id)
        row.outcome_payload = _file_write(observed, reconciled=True)
    page = await case.recovery.recover_page(None, 1)
    assert len(page) == 1
    assert page[0].call_id == call.id
    assert page[0].disposition is ToolRecoveryDisposition.SETTLED
    replay = await case.service.invoke(case.context, case.request)
    assert replay.metadata["reconciled"] is True
    assert "created" not in replay.metadata
    assert "previous_digest" not in replay.metadata
    assert case.writer.call_count == 1
    assert await case.recovery.recover_page(call.id, 1) == ()


async def test_recovery_uses_registered_redactor_for_same_live_receipt_bytes(
    session_factory, tmp_path
):
    from forge.application.services.tools import _safe_metadata, _write_result_artifact_bytes
    from forge.observability.redaction import Redactor

    case = await _orphan_case(session_factory, tmp_path)
    redactor = Redactor(secrets=("recovery",))
    async with PostgresUnitOfWork(session_factory) as work:
        call = await work.tool_calls.get(case.context.invocation_id)
        operation = await work.operations.get(call.operation_intent_id)
    expected = _write_result_artifact_bytes(
        operation.id,
        call.id,
        call.request_digest,
        call.resource_id,
        _safe_metadata(operation.outcome, redactor=redactor),
    )
    recovery = ToolRecoveryService(
        lambda: PostgresUnitOfWork(session_factory), case.store, redactor=redactor
    )
    assert (await recovery.recover_one(call.id)).disposition is ToolRecoveryDisposition.SETTLED
    async with PostgresUnitOfWork(session_factory) as work:
        final = await work.tool_calls.get(call.id)
        data = await case.store.open_bytes(final.artifact_digests[0])
        assert data == expected
        assert b"recovery" not in data
        assert final.result_metadata["path"] == "[REDACTED].txt"
    assert case.writer.call_count == 1


async def test_startup_tool_scan_advances_past_unresolved_page(
    session_factory, tmp_path, monkeypatch
):
    case = await _orphan_case(session_factory, tmp_path)
    second_context = replace(case.context, invocation_id=uuid4())
    case.store.verify_returns = False
    with pytest.raises(ToolInvocationError):
        await case.service.invoke(
            second_context,
            ToolRequest(
                name=ToolName.REPOSITORY_WRITE_FILE,
                arguments={"path": "later.txt", "content": "second durable effect\n"},
            ),
        )
    case.store.verify_returns = True
    first_id, second_id = sorted([case.context.invocation_id, second_context.invocation_id])
    async with PostgresUnitOfWork(session_factory) as work:
        call = await work.tool_calls.get(first_id)
    async with session_factory() as session, session.begin():
        intent = await session.get(OperationRow, call.operation_intent_id)
        intent.outcome_payload = {}

    # Force a real database page boundary: the first orphan must remain running
    # while the later orphan is finalized exactly once.
    recovery = case.recovery
    original_page = recovery.recover_page
    cursors = []

    async def small_page(after_id, limit):
        cursors.append(after_id)
        assert len(cursors) <= 3, "unresolved orphan caused a repeated startup scan"
        return await original_page(after_id, 1)

    monkeypatch.setattr(recovery, "recover_page", small_page)
    assert await recovery.recover_all(allow_unresolved=True) == 1
    assert cursors == [None, first_id, second_id]
    async with PostgresUnitOfWork(session_factory) as work:
        assert (await work.tool_calls.get(first_id)).status is ToolCallStatus.RUNNING
        assert (await work.tool_calls.get(second_id)).status is ToolCallStatus.SUCCEEDED
    assert case.writer.call_count == 2


async def test_orphan_receipt_can_settle_after_startup_intervention(session_factory, tmp_path):
    from forge.domain.run import RunState
    from forge.persistence.repositories.recovery import PostgresRecoveryBarrier
    from forge.worker.startup import run_startup_recovery
    from forge.worker.startup_intervention import StartupInterventionRecovery

    case = await _orphan_case(session_factory, tmp_path)
    recovery = StartupInterventionRecovery(session_factory)

    async def reconcile():
        await recovery.wait_for_owners()
        await recovery.quarantine()

    assert await run_startup_recovery(
        PostgresRecoveryBarrier(session_factory), reconcile, asyncio.Event()
    )
    async with PostgresUnitOfWork(session_factory) as work:
        run = await work.runs.get(case.run)
        assert run.state is RunState.AWAITING_HUMAN_INTERVENTION
        event = next(
            e
            for e in await work.events.list_after(case.run, 0)
            if e.event_type == "run.recovery_intervention"
        )
        assert event.payload["execution_ids"] == (str(case.context.agent_execution_id),)
        assert event.payload["step_ids"] == (str(case.context.step_id),)
        assert event.payload["tool_call_ids"] == (str(case.context.invocation_id),)
    assert await case.recovery.recover_all(allow_unresolved=True) == 1
    assert await case.recovery.recover_all(allow_unresolved=True) == 0
    async with PostgresUnitOfWork(session_factory) as work:
        assert (await work.runs.get(case.run)).state is RunState.AWAITING_HUMAN_INTERVENTION
        assert (
            await work.tool_calls.get(case.context.invocation_id)
        ).status is ToolCallStatus.SUCCEEDED
    assert case.writer.call_count == 1
