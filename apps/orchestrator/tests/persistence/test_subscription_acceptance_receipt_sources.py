"""Acceptance receipts retain real, stopped producer identities across attempts."""

import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID

import pytest
from forge.application.ports.tools import ToolCallRecord
from forge.application.ports.worktrees import GitWorkingTreeSnapshot
from forge.application.services.subscription_decisions import SubscriptionDecisionApplication
from forge.domain.artifact import ArtifactDescriptor, canonical_storage_pointer
from forge.domain.operation import canonical_digest
from forge.domain.subscription import ToolCallBinding
from forge.domain.tool import ToolCallStatus, ToolName
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_acceptance_preparation import acceptance_case


async def receipt_case(session_factory, tmp_path, *, acceptance_factory=acceptance_case):
    tmp_path.mkdir(parents=True, exist_ok=True)
    factory, primary, decision = await acceptance_factory(session_factory, tmp_path)
    await SubscriptionDecisionApplication(factory).prepare_acceptance(primary.attempt.attempt_id)
    async with factory() as work:
        proposal = await work.subscription_decisions.acceptance_proposal(primary.attempt.attempt_id)
    snapshot = GitWorkingTreeSnapshot(
        head_sha=proposal.review.candidate.head_sha,
        base_sha=proposal.worktree.base_sha,
        files=(),
        changed_paths=(),
    )
    call, descriptor, data = await record_snapshot_receipt(
        factory,
        primary,
        UUID(decision.evidence_receipt_ids[0]),
        snapshot,
        proposal.worktree,
        proposal.policy.version,
    )
    return factory, proposal, call, descriptor, data


async def record_snapshot_receipt(factory, primary, identity, snapshot, worktree, policy_version):
    """Persist a test snapshot with real callback, source and artifact bindings."""
    async with factory() as work:
        data = json.dumps(
            dict(
                snapshot.manifest(),
                run_id=str(primary.task.run_id),
                task_id=str(primary.task.task_id),
                attempt_id=str(primary.attempt.attempt_id),
                tool_call_id=str(identity),
                worktree_id=worktree.identity.worktree_name,
                policy_version=policy_version,
            ),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
        digest = hashlib.sha256(data).hexdigest()
        descriptor = await work.artifacts.record(
            ArtifactDescriptor(
                digest=digest,
                media_type="application/json",
                byte_count=len(data),
                storage_path=canonical_storage_pointer(digest),
            ),
            run_id=primary.task.run_id,
            producer_type="subscription_working_tree_snapshot",
            producer_id=identity,
        )
        call = ToolCallRecord(
            id=identity,
            run_id=primary.task.run_id,
            agent_execution_id=None,
            subscription_task_id=primary.task.task_id,
            subscription_attempt_id=primary.attempt.attempt_id,
            subscription_purpose=primary.task.purpose.value,
            tool_name=ToolName.GIT_DIFF,
            normalized_arguments={"scope": "snapshot"},
            authorized=True,
            status=ToolCallStatus.SUCCEEDED,
            started_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
            policy_version=policy_version,
            resource_id=worktree.identity.worktree_name,
            request_digest=canonical_digest({"scope": "snapshot"}),
            invocation_schema_version=1,
            result_metadata_schema_version=1,
            artifact_digests=(digest,),
            result_metadata={
                "manifest_digest": digest,
                "candidate_tree_digest": snapshot.candidate_tree_digest,
            },
        )
        call = await work.tool_calls.record(call)
        await work.subscription.record_operation_receipt(
            ToolCallBinding(
                attempt_id=primary.attempt.attempt_id,
                provider_call_key="acceptance-snapshot",
                durable_operation_id=identity,
                tool_name=call.tool_name,
                arguments_digest=canonical_digest(call.normalized_arguments),
            ),
            run_id=primary.task.run_id,
            task_id=primary.task.task_id,
            receipt={
                "accepted": True,
                "result": {
                    "tool_name": call.tool_name.value,
                    "status": "succeeded",
                    "artifact_digests": [digest],
                    "metadata": dict(call.result_metadata),
                    "operation_intent_id": None,
                },
            },
        )
        await work.commit()
    return call, descriptor, data


@pytest.mark.integration
async def test_acceptance_receipt_loads_primary_frozen_producer(session_factory, tmp_path):
    factory, proposal, call, _, _ = await receipt_case(session_factory, tmp_path)
    async with factory() as work:
        sources = await work.subscription_decisions.acceptance_receipt_sources(proposal)
        assert len(sources) == 1
        assert sources[0].call == call
        assert sources[0].task.task_id == proposal.decision.task_id
        assert sources[0].task.purpose.value == "primary"
        assert sources[0].call.agent_execution_id is None
        assert len(sources[0].producer_digest) == len(sources[0].receipt_digest) == 64


@pytest.mark.integration
@pytest.mark.parametrize("change", ["missing", "receipt", "arguments", "resource", "metadata"])
async def test_invalid_cited_receipt_is_a_definite_claim_error(session_factory, tmp_path, change):
    from dataclasses import replace

    from forge.application.ports.subscription_acceptance_receipts import AcceptanceReceiptClaimError
    from forge.domain.subscription import decode_subscription_record, encode_subscription_record
    from forge.persistence.models.execution import ToolCall
    from forge.persistence.models.subscription import SubscriptionOperationBinding
    from sqlalchemy import select

    factory, proposal, call, _, _ = await receipt_case(session_factory, tmp_path)
    async with factory() as work:
        row = await work.session.get(ToolCall, call.id)
        if change == "missing":
            await work.session.delete(row)
        elif change == "resource":
            row.result_metadata = {**row.result_metadata, "resource_id": "different-worktree"}
        else:
            binding = await work.session.scalar(
                select(SubscriptionOperationBinding).where(
                    SubscriptionOperationBinding.durable_operation_id == call.id
                )
            )
            if change == "receipt":
                binding.receipt_payload = {**binding.receipt_payload, "accepted": False}
            elif change == "arguments":
                binding.payload = encode_subscription_record(
                    replace(decode_subscription_record(binding.payload), arguments_digest="f" * 64)
                )
            else:
                binding.receipt_payload = {
                    **binding.receipt_payload,
                    "result": {
                        **binding.receipt_payload["result"],
                        "metadata": {"manifest_digest": "f" * 64},
                    },
                }
        await work.commit()
    async with factory() as work:
        with pytest.raises(AcceptanceReceiptClaimError):
            await work.subscription_decisions.acceptance_receipt_sources(proposal)


@pytest.mark.integration
async def test_foreign_receipt_is_absent_from_acceptance_run(session_factory, tmp_path):
    from dataclasses import replace

    from forge.application.ports.subscription_acceptance_receipts import AcceptanceReceiptClaimError

    _, _, call, _, _ = await receipt_case(session_factory, tmp_path / "first")
    (tmp_path / "second").mkdir()
    factory, primary, _ = await acceptance_case(
        session_factory,
        tmp_path / "second",
        mutate=lambda decision: replace(decision, evidence_receipt_ids=(str(call.id),)),
    )
    async with factory() as work:
        await work.subscription_decisions.prepare_acceptance(primary.attempt.attempt_id)
        proposal = await work.subscription_decisions.acceptance_proposal(primary.attempt.attempt_id)
        with pytest.raises(AcceptanceReceiptClaimError, match="absent or foreign"):
            await work.subscription_decisions.acceptance_receipt_sources(proposal)
