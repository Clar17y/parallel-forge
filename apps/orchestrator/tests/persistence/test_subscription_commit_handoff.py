"""Real commit receipts bind raw requests while retaining only safe audit arguments."""

from dataclasses import replace

import pytest
from forge.application.services.subscription_broker import (
    ControlledSubscriptionEffect,
    SubscriptionToolBroker,
)
from forge.application.services.subscription_handoff import SubscriptionHandoffVerifier
from forge.application.services.tool_recovery import ToolRecoveryService
from forge.domain.operation import canonical_digest
from forge.domain.subscription import (
    HandoffStatus,
    TaskHandoff,
    decode_subscription_record,
    encode_subscription_record,
)
from forge.domain.tool import ToolName
from forge.persistence.models.execution import ToolCall
from forge.persistence.models.subscription import SubscriptionOperationBinding
from forge.persistence.unit_of_work import PostgresUnitOfWork
from sqlalchemy import select
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_snapshot_tool import _snapshot_case


@pytest.mark.integration
@pytest.mark.parametrize("corruption", [None, "callback", "request", "normalized_message"])
async def test_actual_commit_handoff_checks_both_request_and_terminal_message(
    session_factory, tmp_path, corruption
):
    original, _, attempt, store, tree, authority = await _snapshot_case(session_factory, tmp_path)
    permitted = frozenset({ToolName.GIT_COMMIT, ToolName.GIT_DIFF})
    broker = SubscriptionToolBroker(
        lambda: PostgresUnitOfWork(session_factory),
        lease=original._lease,
        authority=replace(authority, permitted_tools=permitted),
        effect=ControlledSubscriptionEffect(
            original._effect.service, replace(original._effect.context, permitted_tools=permitted)
        ),
    )
    arguments = {"message": "test: bind the complete commit request"}
    committed = await broker.invoke(
        token="commit-token",
        provider_call_key="commit-evidence",
        tool_name=ToolName.GIT_COMMIT,
        arguments=arguments,
    )
    assert committed.accepted
    snapshot = await broker.invoke(
        token="commit-token",
        provider_call_key="commit-snapshot",
        tool_name=ToolName.GIT_DIFF,
        arguments={"scope": "snapshot"},
    )
    assert snapshot.accepted
    factory = lambda: PostgresUnitOfWork(session_factory)
    async with factory() as work:
        task = await work.subscription.get_task(authority.run_id, authority.task_id)
        call = await work.session.get(ToolCall, committed.operation_id)
        assert set(call.normalized_arguments) == {"message_digest"}
        assert call.result_metadata["request_digest"] == canonical_digest(arguments)
        assert call.result_metadata["request_digest"] != canonical_digest(call.normalized_arguments)
        if corruption == "callback":
            binding = await work.session.scalar(
                select(SubscriptionOperationBinding).where(
                    SubscriptionOperationBinding.durable_operation_id == committed.operation_id
                )
            )
            value = decode_subscription_record(binding.payload)
            binding.payload = encode_subscription_record(replace(value, arguments_digest="f" * 64))
        elif corruption == "request":
            call.result_metadata = {**call.result_metadata, "request_digest": "f" * 64}
        elif corruption == "normalized_message":
            call.normalized_arguments = {"message_digest": "f" * 64}
        await work.commit()
    handoff = TaskHandoff(
        run_id=authority.run_id,
        task_id=authority.task_id,
        attempt_id=attempt,
        status=HandoffStatus.COMPLETED,
        candidate_commit=committed.result["metadata"]["new_sha"],
        candidate_tree_digest=snapshot.result["metadata"]["candidate_tree_digest"],
        evidence_receipt_ids=(str(committed.operation_id), str(snapshot.operation_id)),
        summary="An actual controlled commit and snapshot",
    )
    proof = await SubscriptionHandoffVerifier(
        factory, store, ToolRecoveryService(factory, store)
    ).verify(
        handoff,
        task=task,
        policy_version=authority.policy_version,
        worktree_id=authority.worktree_id,
        resource_id=authority.worktree_id,
        base_sha=tree.base_sha,
    )
    assert (proof is not None) is (corruption is None)
