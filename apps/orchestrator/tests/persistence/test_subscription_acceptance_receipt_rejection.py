"""Invalid final receipt citations get bounded repair without reopening valid code."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from forge.application.services.subscription_acceptance_receipts import (
    SubscriptionAcceptanceReceiptVerification,
)
from forge.application.services.subscription_decisions import SubscriptionDecisionApplication
from forge.persistence.models.scheduling import SubscriptionScheduledTask, SubscriptionSchedulerRun
from forge.persistence.models.subscription import SubscriptionTask
from forge.persistence.models.subscription_results import SubscriptionAttemptResult
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_acceptance_preparation import acceptance_case


@pytest.mark.integration
async def test_missing_receipt_repair_keeps_selected_candidate_closed(session_factory, tmp_path):
    factory, primary, decision = await acceptance_case(session_factory, tmp_path)
    await SubscriptionDecisionApplication(factory).prepare_acceptance(primary.attempt.attempt_id)
    store = SimpleNamespace(verify=AsyncMock(), open_bytes=AsyncMock())
    terminal = SimpleNamespace(verify_terminal_effect=AsyncMock())
    service = SubscriptionAcceptanceReceiptVerification(factory, store, terminal)
    rejected = await service.reject_claims(primary.attempt.attempt_id)
    assert not rejected.accepted and rejected.disposition == "acceptance_repair_queued"
    assert (await service.reject_claims(primary.attempt.attempt_id)).replayed
    async with factory() as work:
        source = await work.session.get(SubscriptionAttemptResult, primary.attempt.attempt_id)
        task = await work.session.get(SubscriptionTask, primary.task.task_id)
        scheduled = await work.session.get(SubscriptionScheduledTask, primary.task.task_id)
        scheduler = await work.session.get(SubscriptionSchedulerRun, primary.task.run_id)
        assert task.state == scheduled.state == "queued" and scheduled.repairs == 1
        assert scheduler.candidate_state == "closed"
        assert scheduler.candidate_epoch == primary.candidate_epoch
        assert source.application_payload["rejection"] == {
            "reason": "receipt_claim_invalid",
            "repair": True,
            "candidate_epoch": primary.candidate_epoch,
            "receipt_id": decision.evidence_receipt_ids[0],
            "claim_error": "absent_or_foreign",
        }
    store.verify.assert_not_awaited()
    store.open_bytes.assert_not_awaited()
    terminal.verify_terminal_effect.assert_not_awaited()


@pytest.mark.integration
async def test_valid_receipt_cannot_spend_repair_budget_even_when_storage_is_unavailable(
    session_factory, tmp_path
):
    from forge.application.ports.subscription_decisions import SubscriptionDecisionError
    from test_subscription_acceptance_receipt_sources import receipt_case

    factory, proposal, _, _, _ = await receipt_case(session_factory, tmp_path)
    service = SubscriptionAcceptanceReceiptVerification(
        factory,
        SimpleNamespace(verify=AsyncMock(return_value=False), open_bytes=AsyncMock()),
        SimpleNamespace(verify_terminal_effect=AsyncMock()),
    )
    assert await service.verify(proposal.attempt_id) is None
    with pytest.raises(SubscriptionDecisionError, match="no invalid receipt claim"):
        await service.reject_claims(proposal.attempt_id)
    async with factory() as work:
        source = await work.session.get(SubscriptionAttemptResult, proposal.attempt_id)
        task = await work.session.get(SubscriptionScheduledTask, proposal.decision.task_id)
        assert source.disposition == "acceptance_prepared" and task.repairs == 0


@pytest.mark.integration
async def test_receipt_claim_rejection_rolls_back_and_concurrent_calls_debit_once(
    session_factory, tmp_path
):
    import asyncio

    from forge.persistence.models.subscription_results import SubscriptionRepairDebit

    factory, primary, _ = await acceptance_case(session_factory, tmp_path)
    await SubscriptionDecisionApplication(factory).prepare_acceptance(primary.attempt.attempt_id)
    async with factory() as work:
        await work.subscription_decisions.reject_acceptance_receipt_claims(
            primary.attempt.attempt_id
        )
        await work.rollback()
    async with factory() as work:
        source = await work.session.get(SubscriptionAttemptResult, primary.attempt.attempt_id)
        assert (
            source.disposition == "acceptance_prepared"
            and "rejection" not in source.application_payload
        )
        assert await work.session.get(SubscriptionRepairDebit, primary.attempt.attempt_id) is None
    service = SubscriptionAcceptanceReceiptVerification(
        factory, SimpleNamespace(), SimpleNamespace()
    )
    outcomes = await asyncio.gather(
        *(service.reject_claims(primary.attempt.attempt_id) for _ in range(2))
    )
    assert sorted(result.replayed for result in outcomes) == [False, True]
    async with factory() as work:
        task = await work.session.get(SubscriptionScheduledTask, primary.task.task_id)
        assert task.repairs == 1


@pytest.mark.integration
async def test_receipt_rejection_replays_after_next_primary_admission_with_actionable_handoff(
    session_factory, tmp_path
):
    from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
    from forge.domain.subscription import decode_subscription_record
    from forge.persistence.models.subscription import SubscriptionDecisionRecord
    from sqlalchemy import select
    from test_subscription_usage import _reservation

    factory, primary, decision = await acceptance_case(session_factory, tmp_path)
    service = SubscriptionAcceptanceReceiptVerification(
        factory, SimpleNamespace(), SimpleNamespace()
    )
    await service.reject_claims(primary.attempt.attempt_id)
    following = await SubscriptionDecisionExecutor(factory).admit_next(
        "repair-receipt-claims", _reservation()
    )
    assert following.task.task_id == primary.task.task_id
    assert following.candidate_epoch == primary.candidate_epoch
    assert (await service.reject_claims(primary.attempt.attempt_id)).replayed
    async with factory() as work:
        failure = await work.session.scalar(
            select(SubscriptionDecisionRecord).where(
                SubscriptionDecisionRecord.run_id == primary.task.run_id,
                SubscriptionDecisionRecord.idempotency_key
                == f"decision-rejection:{primary.attempt.attempt_id}",
            )
        )
        handoff = decode_subscription_record(failure.payload)
        assert decision.evidence_receipt_ids[0] in handoff.summary
        assert "absent or foreign" in handoff.summary


@pytest.mark.integration
@pytest.mark.parametrize("entrypoint", ["verify", "reject_claims", "reject_mismatch"])
async def test_acceptance_entrypoints_preserve_intrinsic_rejection_from_preparation(
    session_factory, tmp_path, entrypoint
):
    from dataclasses import replace

    from forge.application.services.subscription_acceptance import SubscriptionAcceptanceInspection

    factory, primary, _ = await acceptance_case(
        session_factory,
        tmp_path,
        mutate=lambda decision: replace(decision, evidence_receipt_ids=("not-a-receipt",)),
    )
    service = (
        SubscriptionAcceptanceInspection(factory, AsyncMock())
        if entrypoint == "reject_mismatch"
        else SubscriptionAcceptanceReceiptVerification(
            factory, SimpleNamespace(), SimpleNamespace()
        )
    )
    first = await getattr(service, entrypoint)(primary.attempt.attempt_id)
    second = await getattr(service, entrypoint)(primary.attempt.attempt_id)
    if entrypoint == "verify":
        assert first is second is None
    else:
        assert first.disposition == "decision_repair_queued" and second.replayed
    async with factory() as work:
        source = await work.session.get(SubscriptionAttemptResult, primary.attempt.attempt_id)
        task = await work.session.get(SubscriptionScheduledTask, primary.task.task_id)
        assert source.disposition == "decision_repair_queued" and task.repairs == 1
