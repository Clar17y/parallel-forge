"""Verify receipt bytes outside transactions and retain proof under current authority."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from forge.application.services.subscription_acceptance_receipts import (
    SubscriptionAcceptanceReceiptVerification,
)
from forge.persistence.models.subscription_results import SubscriptionAttemptResult
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_acceptance_receipt_sources import receipt_case


@pytest.mark.integration
async def test_acceptance_verifies_primary_snapshot_and_retains_real_receipt_proof(
    session_factory, tmp_path
):
    factory, proposal, call, descriptor, data = await receipt_case(session_factory, tmp_path)
    reads = []

    async def read(digest, **kwargs):
        async with factory() as work:
            await asyncio.wait_for(work.runs.get_for_update(proposal.decision.run_id), 5)
        reads.append(digest)
        return data

    service = SubscriptionAcceptanceReceiptVerification(
        factory,
        SimpleNamespace(verify=AsyncMock(return_value=True), open_bytes=read),
        SimpleNamespace(verify_terminal_effect=AsyncMock(return_value=None)),
    )
    proof = await service.verify(proposal.attempt_id)
    assert proof is not None
    assert proof.receipts[0].call.call_id == call.id
    assert proof.receipts[0].attempt_id == proposal.attempt_id
    assert proof.receipts[0].matches_candidate
    assert proof.receipts[0].snapshot == proposal.review.candidate
    assert proof.artifact_proofs[0][0] == descriptor.digest
    assert proof == await service.verify(proposal.attempt_id)
    assert reads == [descriptor.digest, descriptor.digest]
    async with factory() as work:
        source = await work.session.get(SubscriptionAttemptResult, proposal.attempt_id)
        assert source.application_payload["receipt_verification"] == proof.payload()
        assert source.disposition == "acceptance_prepared"
        assert (await work.runs.get(proposal.decision.run_id)).state.value == "IMPLEMENTING"


@pytest.mark.integration
@pytest.mark.parametrize("failure", ["offline", "bytes", "missing"])
async def test_unavailable_bytes_preserve_prepared_intent_and_repair_budget(
    session_factory, tmp_path, failure
):
    from forge.persistence.models.scheduling import SubscriptionScheduledTask

    factory, proposal, _, _, data = await receipt_case(session_factory, tmp_path)

    async def read(*args, **kwargs):
        if failure == "offline":
            raise OSError("artifact storage offline")
        return b"wrong bytes" if failure == "bytes" else data

    service = SubscriptionAcceptanceReceiptVerification(
        factory,
        SimpleNamespace(verify=AsyncMock(return_value=failure != "missing"), open_bytes=read),
        SimpleNamespace(verify_terminal_effect=AsyncMock(return_value=None)),
    )
    assert await service.verify(proposal.attempt_id) is None
    async with factory() as work:
        source = await work.session.get(SubscriptionAttemptResult, proposal.attempt_id)
        task = await work.session.get(SubscriptionScheduledTask, proposal.decision.task_id)
        assert source.disposition == "acceptance_prepared"
        assert "receipt_verification" not in source.application_payload
        assert task.repairs == 0


@pytest.mark.integration
@pytest.mark.parametrize(
    "change", ["epoch", "pause", "receipt", "call", "artifact", "launch", "usage"]
)
async def test_acceptance_rechecks_authority_and_evidence_after_artifact_io(
    session_factory, tmp_path, change
):
    from forge.application.ports.subscription_acceptance_receipts import AcceptanceReceiptClaimError
    from forge.application.ports.subscription_decisions import SubscriptionDecisionError
    from forge.persistence.models.execution import ArtifactLineage, ToolCall
    from forge.persistence.models.scheduling import SubscriptionSchedulerRun
    from forge.persistence.models.subscription import (
        SubscriptionClientLaunch,
        SubscriptionOperationBinding,
    )
    from forge.persistence.models.subscription_usage import SubscriptionAttemptConsumption
    from sqlalchemy import select

    factory, proposal, call, descriptor, data = await receipt_case(session_factory, tmp_path)

    async def read(*args, **kwargs):
        async with factory() as work:
            if change == "epoch":
                row = await work.session.get(SubscriptionSchedulerRun, proposal.decision.run_id)
                row.candidate_epoch += 1
            elif change == "pause":
                run = await work.runs.get_for_update(proposal.decision.run_id)
                await work.runs.pause(run.id, run.version, "run.paused", {})
            elif change == "receipt":
                row = await work.session.scalar(
                    select(SubscriptionOperationBinding).where(
                        SubscriptionOperationBinding.durable_operation_id == call.id
                    )
                )
                row.receipt_payload = {**row.receipt_payload, "accepted": False}
            elif change == "call":
                row = await work.session.get(ToolCall, call.id)
                row.result_metadata = {**row.result_metadata, "duration_ms": 99}
            elif change == "artifact":
                row = await work.session.scalar(
                    select(ArtifactLineage).where(
                        ArtifactLineage.artifact_id == descriptor.artifact_id,
                        ArtifactLineage.run_id == proposal.decision.run_id,
                    )
                )
                row.producer_kind = "changed"
            elif change == "launch":
                row = await work.session.scalar(
                    select(SubscriptionClientLaunch).where(
                        SubscriptionClientLaunch.attempt_id == proposal.attempt_id
                    )
                )
                row.state = "uncertain"
            else:
                row = await work.session.get(SubscriptionAttemptConsumption, proposal.attempt_id)
                await work.session.delete(row)
            await work.commit()
        return data

    service = SubscriptionAcceptanceReceiptVerification(
        factory,
        SimpleNamespace(verify=AsyncMock(return_value=True), open_bytes=read),
        SimpleNamespace(verify_terminal_effect=AsyncMock(return_value=None)),
    )
    with pytest.raises((SubscriptionDecisionError, AcceptanceReceiptClaimError)):
        await service.verify(proposal.attempt_id)
    async with factory() as work:
        source = await work.session.get(SubscriptionAttemptResult, proposal.attempt_id)
        assert "receipt_verification" not in source.application_payload


@pytest.mark.integration
async def test_equal_concurrent_verifications_retain_one_proof_and_rollback_is_atomic(
    session_factory, tmp_path
):
    factory, proposal, _, _, data = await receipt_case(session_factory, tmp_path)
    service = SubscriptionAcceptanceReceiptVerification(
        factory,
        SimpleNamespace(
            verify=AsyncMock(return_value=True), open_bytes=AsyncMock(return_value=data)
        ),
        SimpleNamespace(verify_terminal_effect=AsyncMock(return_value=None)),
    )
    async with factory() as work:
        sources = await work.subscription_decisions.acceptance_receipt_sources(proposal)
    proof = await service._verify(proposal, sources)
    assert proof is not None
    async with factory() as work:
        await work.subscription_decisions.record_acceptance_receipts(proposal, proof)
        await work.rollback()
    async with factory() as work:
        row = await work.session.get(SubscriptionAttemptResult, proposal.attempt_id)
        assert "receipt_verification" not in row.application_payload
    assert await asyncio.gather(
        service.verify(proposal.attempt_id), service.verify(proposal.attempt_id)
    ) == [proof, proof]


@pytest.mark.integration
async def test_receipt_proof_survives_later_candidate_mismatch_recovery(session_factory, tmp_path):
    from forge.application.ports.worktrees import GitWorkingTreeSnapshot
    from forge.application.services.subscription_acceptance import SubscriptionAcceptanceInspection

    factory, proposal, _, _, data = await receipt_case(session_factory, tmp_path)
    service = SubscriptionAcceptanceReceiptVerification(
        factory,
        SimpleNamespace(
            verify=AsyncMock(return_value=True), open_bytes=AsyncMock(return_value=data)
        ),
        SimpleNamespace(verify_terminal_effect=AsyncMock(return_value=None)),
    )
    proof = await service.verify(proposal.attempt_id)
    assert proof is not None
    changed = GitWorkingTreeSnapshot(
        head_sha="f" * 40, base_sha=proposal.worktree.base_sha, files=(), changed_paths=()
    )
    inspection = SubscriptionAcceptanceInspection(factory, AsyncMock(return_value=changed))
    rejected = await inspection.reject_mismatch(proposal.attempt_id)
    assert rejected.disposition == "acceptance_repair_queued"
    assert (await inspection.reject_mismatch(proposal.attempt_id)).replayed
    async with factory() as work:
        source = await work.session.get(SubscriptionAttemptResult, proposal.attempt_id)
        assert source.application_payload["receipt_verification"] == proof.payload()
