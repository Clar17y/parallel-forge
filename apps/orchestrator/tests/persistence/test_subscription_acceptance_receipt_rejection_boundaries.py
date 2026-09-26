"""Receipt-claim repair respects current controls, budgets and retained diagnostics."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from forge.application.ports.subscription_decisions import SubscriptionDecisionError
from forge.application.ports.worktrees import GitWorkingTreeSnapshot
from forge.application.services.subscription_acceptance import SubscriptionAcceptanceInspection
from forge.application.services.subscription_acceptance_receipts import (
    SubscriptionAcceptanceReceiptVerification,
)
from forge.application.services.subscription_decisions import SubscriptionDecisionApplication
from forge.domain.operation import canonical_digest
from forge.persistence.models.scheduling import SubscriptionScheduledTask, SubscriptionSchedulerRun
from forge.persistence.models.subscription import SubscriptionTask
from forge.persistence.models.subscription_results import (
    SubscriptionAttemptResult,
    SubscriptionRepairDebit,
)
from sqlalchemy.orm.attributes import flag_modified
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_acceptance_preparation import acceptance_case


@pytest.mark.integration
@pytest.mark.parametrize(
    "change",
    [
        "epoch_bool",
        "epoch_float",
        "epoch",
        "foreign_receipt",
        "known_error",
        "unknown_error",
        "repair",
        "debit",
        "removed",
    ],
)
async def test_receipt_rejection_replay_refuses_corrupt_receipt_or_debit(
    session_factory, tmp_path, change
):
    factory, primary, _ = await acceptance_case(session_factory, tmp_path)
    service = SubscriptionAcceptanceReceiptVerification(
        factory, SimpleNamespace(), SimpleNamespace()
    )
    await service.reject_claims(primary.attempt.attempt_id)
    async with factory() as work:
        source = await work.session.get(SubscriptionAttemptResult, primary.attempt.attempt_id)
        rejection = dict(source.application_payload["rejection"])
        if change == "epoch_bool":
            assert rejection["candidate_epoch"] == 1
            rejection["candidate_epoch"] = True
        elif change == "epoch_float":
            rejection["candidate_epoch"] = float(rejection["candidate_epoch"])
        elif change == "epoch":
            rejection["candidate_epoch"] += 1
        elif change == "foreign_receipt":
            rejection["receipt_id"] = str(uuid4())
        elif change == "known_error":
            rejection["claim_error"] = "callback_binding_differs"
        elif change == "unknown_error":
            rejection["claim_error"] = "made_up_error"
        elif change == "repair":
            rejection["repair"] = 1
        elif change == "debit":
            row = await work.session.get(SubscriptionRepairDebit, primary.attempt.attempt_id)
            await work.session.delete(row)
        source.application_payload = {**source.application_payload, "rejection": rejection}
        if change == "removed":
            source.application_payload = {
                k: v for k, v in source.application_payload.items() if k != "rejection"
            }
        flag_modified(source, "application_payload")
        source.application_digest = canonical_digest(source.application_payload)
        await work.commit()
    with pytest.raises(SubscriptionDecisionError):
        await service.reject_claims(primary.attempt.attempt_id)


@pytest.mark.integration
async def test_exhausted_receipt_repairs_terminate_task_without_reopening_candidate(
    session_factory, tmp_path
):
    factory, primary, _ = await acceptance_case(session_factory, tmp_path)
    async with factory() as work:
        row = await work.session.get(SubscriptionScheduledTask, primary.task.task_id)
        row.repairs = row.max_repairs
        await work.commit()
    service = SubscriptionAcceptanceReceiptVerification(
        factory, SimpleNamespace(), SimpleNamespace()
    )
    assert (
        await service.reject_claims(primary.attempt.attempt_id)
    ).disposition == "acceptance_rejected"
    assert (await service.reject_claims(primary.attempt.attempt_id)).replayed
    async with factory() as work:
        row = await work.session.get(SubscriptionTask, primary.task.task_id)
        scheduler = await work.session.get(SubscriptionSchedulerRun, primary.task.run_id)
        assert row.state == "terminal"
        assert (
            scheduler.candidate_epoch == primary.candidate_epoch
            and scheduler.candidate_state == "closed"
        )
        assert await work.session.get(SubscriptionRepairDebit, primary.attempt.attempt_id) is None


@pytest.mark.integration
@pytest.mark.parametrize("change", ["pause", "observation"])
async def test_receipt_repair_refuses_stopped_authority_or_known_candidate_mismatch(
    session_factory, tmp_path, change
):
    factory, primary, _ = await acceptance_case(session_factory, tmp_path)
    await SubscriptionDecisionApplication(factory).prepare_acceptance(primary.attempt.attempt_id)
    if change == "pause":
        async with factory() as work:
            run = await work.runs.get_for_update(primary.task.run_id)
            await work.runs.pause(run.id, run.version, "run.paused", {})
            await work.commit()
    else:
        inspection = SubscriptionAcceptanceInspection(
            factory,
            AsyncMock(
                return_value=GitWorkingTreeSnapshot(
                    head_sha="f" * 40,
                    base_sha="a" * 40,
                    files=(),
                    changed_paths=(),
                )
            ),
        )
        await inspection.inspect(primary.attempt.attempt_id)
    service = SubscriptionAcceptanceReceiptVerification(
        factory, SimpleNamespace(), SimpleNamespace()
    )
    with pytest.raises(SubscriptionDecisionError):
        await service.reject_claims(primary.attempt.attempt_id)
    async with factory() as work:
        source = await work.session.get(SubscriptionAttemptResult, primary.attempt.attempt_id)
        assert "rejection" not in source.application_payload
        assert await work.session.get(SubscriptionRepairDebit, primary.attempt.attempt_id) is None
