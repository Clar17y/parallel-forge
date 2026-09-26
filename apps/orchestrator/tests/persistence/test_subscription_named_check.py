"""Subscription named checks retain broker lineage through the normal runner."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from uuid import uuid4, uuid5

import pytest
from forge.application.services.subscription_broker import (
    ControlledSubscriptionEffect,
    SubscriptionToolBroker,
)
from forge.application.services.tool_recovery import ToolRecoveryDisposition, ToolRecoveryService
from forge.domain.operation import canonical_digest
from forge.domain.subscription import (
    AttemptIdentity,
    BrokerAuthorizationBinding,
    RouteBinding,
    SpecialistPurpose,
)
from forge.domain.tool import SubscriptionToolAuthorizationContext, ToolCallStatus, ToolName
from forge.persistence.models import AgentExecution, OperationIntent, Step, ToolCall
from forge.persistence.models.scheduling import SubscriptionScheduledTask
from forge.persistence.models.subscription import SubscriptionAttempt, SubscriptionTask
from forge.persistence.unit_of_work import PostgresUnitOfWork
from sqlalchemy import delete, select, update
from test_scheduler_acceptance import (  # noqa: F401
    _admit_run,
    _enqueue,
    _remove_disposable_subscription_rows,
    _route,
)

from apps.orchestrator.tests.application.test_tool_named_check import _named_case


async def _subscription_named_case(session_factory, tmp_path):
    case = await _named_case(session_factory, tmp_path)
    context = case.context
    async with PostgresUnitOfWork(session_factory) as work:
        run = await work.runs.get(context.run_id)
        await work.session.execute(
            delete(AgentExecution).where(AgentExecution.id == context.agent_execution_id)
        )
        await work.session.execute(delete(Step).where(Step.id == context.step_id))
        primary = await _admit_run(work, run, (_route("p"), _route("p")))
        task_id = await _enqueue(
            work,
            run_id=run.id,
            provider="p",
            worktree=context.worktree_id,
            parent_id=primary,
            paths=("apps",),
        )
        attempt_id = uuid4()
        await work.subscription.create_attempt(
            AttemptIdentity(run_id=run.id, task_id=task_id, attempt_id=attempt_id),
            route_payload=RouteBinding(requested=_route("p"), effective=_route("p")),
            idempotency_key="named-attempt",
        )
        await work.session.execute(
            update(SubscriptionTask).where(SubscriptionTask.id == task_id).values(state="running")
        )
        await work.session.execute(
            update(SubscriptionAttempt)
            .where(SubscriptionAttempt.id == attempt_id)
            .values(status="running")
        )
        lease = await work.scheduler.claim_ready("named-owner", timedelta(seconds=30))
        await work.commit()
    assert lease is not None
    authority = BrokerAuthorizationBinding(
        run_id=context.run_id,
        task_id=task_id,
        attempt_id=attempt_id,
        worktree_id=context.worktree_id,
        role=SpecialistPurpose.ROUTINE_IMPLEMENTATION,
        policy_version=1,
        permitted_tools=frozenset({ToolName.BUILD_RUN_NAMED_CHECK}),
        broker_token="named-token",
    )
    subscription_context = SubscriptionToolAuthorizationContext(
        run_id=context.run_id,
        task_id=task_id,
        attempt_id=attempt_id,
        worktree_id=context.worktree_id,
        purpose=SpecialistPurpose.ROUTINE_IMPLEMENTATION,
        policy_version=1,
        permitted_tools=frozenset({ToolName.BUILD_RUN_NAMED_CHECK}),
    )
    broker = SubscriptionToolBroker(
        lambda: PostgresUnitOfWork(session_factory),
        lease=lease,
        authority=authority,
        effect=ControlledSubscriptionEffect(case.service, subscription_context),
    )
    case.factory.expected_call_id = uuid5(attempt_id, "forge-subscription-tool-v1:named")
    return case, broker, task_id, attempt_id


@pytest.mark.integration
async def test_subscription_named_check_runs_once_replays_and_has_no_legacy_lineage(
    session_factory, tmp_path
):
    case, broker, task_id, attempt_id = await _subscription_named_case(session_factory, tmp_path)
    kwargs = {
        "token": "named-token",
        "provider_call_key": "named",
        "tool_name": ToolName.BUILD_RUN_NAMED_CHECK,
        "arguments": {"command_name": "unit"},
    }
    receipt = await broker.invoke(**kwargs)
    assert receipt.accepted and receipt.result["status"] == "succeeded"
    assert await broker.invoke(**kwargs) == receipt
    assert case.factory.calls == 1
    async with PostgresUnitOfWork(session_factory) as work:
        call = await work.tool_calls.get(receipt.operation_id)
        assert call.status is ToolCallStatus.SUCCEEDED
        assert call.subscription_task_id == task_id and call.subscription_attempt_id == attempt_id
        assert call.agent_execution_id is None and call.step_id is None
        operation = await work.operations.get(receipt.operation_id)
        assert operation.request_payload["authority_schema_version"] == 2
        assert operation.request_payload["subscription_task_id"] == str(task_id)


@pytest.mark.integration
async def test_subscription_named_check_denies_unlisted_command_before_runner(
    session_factory, tmp_path
):
    case, broker, _, _ = await _subscription_named_case(session_factory, tmp_path)
    receipt = await broker.invoke(
        token="named-token",
        provider_call_key="bad-named",
        tool_name=ToolName.BUILD_RUN_NAMED_CHECK,
        arguments={"command_name": "not-in-policy"},
    )
    assert not receipt.accepted and receipt.result["status"] == "denied"
    assert case.factory.calls == 0
    async with PostgresUnitOfWork(session_factory) as work:
        calls = (
            await work.session.scalars(
                select(ToolCall).where(ToolCall.run_id == case.context.run_id)
            )
        ).all()
        assert len(calls) == 1 and calls[0].status == ToolCallStatus.DENIED.value.upper()


@pytest.mark.integration
async def test_subscription_named_check_cancellation_settles_terminal_receipt(
    session_factory, tmp_path
):
    case, broker, _, _ = await _subscription_named_case(session_factory, tmp_path)
    case.factory.release.clear()
    invocation = asyncio.create_task(
        broker.invoke(
            token="named-token",
            provider_call_key="named",
            tool_name=ToolName.BUILD_RUN_NAMED_CHECK,
            arguments={"command_name": "unit"},
        )
    )
    await asyncio.wait_for(case.factory.entered.wait(), 5)
    invocation.cancel()
    await asyncio.wait_for(case.factory.cancel_received.wait(), 5)
    case.factory.release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(invocation, 5)
    async with PostgresUnitOfWork(session_factory) as work:
        call = await work.tool_calls.get(case.factory.expected_call_id)
        assert call.status is ToolCallStatus.CANCELLED
        assert call.subscription_task_id is not None and call.agent_execution_id is None


@pytest.mark.integration
@pytest.mark.parametrize("foreign_lineage", [False, True])
async def test_subscription_named_check_recovers_historical_receipt_without_current_lease(
    session_factory, tmp_path, foreign_lineage
):
    case, broker, task_id, attempt_id = await _subscription_named_case(session_factory, tmp_path)
    receipt = await broker.invoke(
        token="named-token",
        provider_call_key="named",
        tool_name=ToolName.BUILD_RUN_NAMED_CHECK,
        arguments={"command_name": "unit"},
    )
    async with session_factory() as session, session.begin():
        call = await session.get(ToolCall, receipt.operation_id)
        assert call is not None
        call.status = ToolCallStatus.RUNNING.value.upper()
        call.completed_at = None
        call.duration_ms = None
        await session.execute(
            update(SubscriptionAttempt)
            .where(SubscriptionAttempt.id == attempt_id)
            .values(status="terminal")
        )
        await session.execute(
            update(SubscriptionTask)
            .where(SubscriptionTask.id == task_id)
            .values(cancel_requested=True)
        )
        await session.execute(
            update(SubscriptionScheduledTask)
            .where(SubscriptionScheduledTask.task_id == task_id)
            .values(lease_expires_at=None, lease_owner=None)
        )
        if foreign_lineage:
            operation = await session.get(OperationIntent, receipt.operation_id)
            assert operation is not None
            payload = dict(operation.request_payload)
            payload["subscription_attempt_id"] = str(uuid4())
            operation.request_payload = payload
            operation.request_digest = canonical_digest(payload)
    recovery = ToolRecoveryService(lambda: PostgresUnitOfWork(session_factory), case.store)
    result = await recovery.recover_one(receipt.operation_id)
    expected = (
        ToolRecoveryDisposition.INTERVENTION if foreign_lineage else ToolRecoveryDisposition.SETTLED
    )
    assert result.disposition is expected
    assert case.factory.calls == 1
    if foreign_lineage:
        return
    assert (
        await recovery.recover_one(receipt.operation_id)
    ).disposition is ToolRecoveryDisposition.TERMINAL
    async with PostgresUnitOfWork(session_factory) as work:
        call = await work.tool_calls.get(receipt.operation_id)
        assert call.status is ToolCallStatus.SUCCEEDED


@pytest.mark.integration
@pytest.mark.parametrize("changed", [False, True, "before_unavailable", "after_unavailable"])
async def test_subscription_check_retains_before_after_candidate_digests(
    session_factory, tmp_path, monkeypatch, changed
):
    from forge.application.ports.worktrees import GitSnapshotFile, GitWorkingTreeSnapshot

    case, broker, _, _ = await _subscription_named_case(session_factory, tmp_path)
    snapshots = []

    def snapshot(tree, *, secret_paths):
        import threading

        assert threading.current_thread() is not threading.main_thread()
        assert tree == case.service._worktree
        index = len(snapshots)
        value = GitWorkingTreeSnapshot(
            head_sha=case.service._git.head_sha(tree),
            base_sha=tree.base_sha,
            files=(
                GitSnapshotFile(
                    path="apps/file.py",
                    mode="100644",
                    content_digest=("c" if index and changed is True else "b") * 64,
                    byte_count=1,
                ),
            ),
            changed_paths=("apps/file.py",),
        )
        snapshots.append(value)
        if (changed == "before_unavailable" and index == 0) or (
            changed == "after_unavailable" and index == 1
        ):
            raise OSError("snapshot unavailable")
        return value

    monkeypatch.setattr(case.service._git, "working_tree_snapshot", snapshot, raising=False)
    receipt = await broker.invoke(
        token="named-token",
        provider_call_key="named",
        tool_name=ToolName.BUILD_RUN_NAMED_CHECK,
        arguments={"command_name": "unit"},
    )
    assert receipt.accepted and case.factory.calls == 1
    async with PostgresUnitOfWork(session_factory) as work:
        call = await work.tool_calls.get(receipt.operation_id)
        assert len(snapshots) == 2
        metadata = call.result_metadata
        assert metadata["candidate_tree_digest_before"] == (
            None if changed == "before_unavailable" else snapshots[0].candidate_tree_digest
        )
        assert metadata["candidate_tree_digest_after"] == (
            None if changed == "after_unavailable" else snapshots[1].candidate_tree_digest
        )
    verifier = ToolRecoveryService(lambda: PostgresUnitOfWork(session_factory), case.store)
    assert await verifier.verify_terminal_effect(receipt.operation_id) is not None
    assert (
        await broker.invoke(
            token="named-token",
            provider_call_key="named",
            tool_name=ToolName.BUILD_RUN_NAMED_CHECK,
            arguments={"command_name": "unit"},
        )
        == receipt
    )
    assert len(snapshots) == 2 and case.factory.calls == 1
