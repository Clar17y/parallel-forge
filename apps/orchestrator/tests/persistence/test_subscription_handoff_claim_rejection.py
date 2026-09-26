"""A malformed stored claim is rejected without consulting external storage."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from forge.application.ports.subscription_decisions import SubscriptionDecisionError
from forge.application.services.subscription_decisions import SubscriptionDecisionApplication
from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
from forge.application.services.subscription_handoff_application import (
    SubscriptionHandoffApplication,
)
from forge.domain.operation import canonical_digest
from forge.domain.subscription import CheckResultEvidence
from forge.persistence.models.scheduling import SubscriptionScheduledTask
from forge.persistence.models.subscription import SubscriptionDecisionRecord, SubscriptionTask
from forge.persistence.models.subscription_handoff import SubscriptionHandoffFence
from forge.persistence.models.subscription_results import SubscriptionAttemptResult
from forge.persistence.repositories.scheduling import PostgresSchedulingRepository
from sqlalchemy import select
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_handoff_proposal import handoff_case
from test_subscription_usage import _reservation


@pytest.mark.integration
@pytest.mark.parametrize("repairs", [0, 1])
@pytest.mark.parametrize("claim", ["receipts", "checks"])
async def test_invalid_receipts_reject_without_external_io(
    session_factory, tmp_path, repairs, claim
):
    factory, parent, child, _ = await handoff_case(
        session_factory,
        tmp_path,
        repairs=repairs,
        mutate_handoff=lambda value: (
            replace(value, evidence_receipt_ids=("malformed",))
            if claim == "receipts"
            else replace(
                value,
                check_results=(
                    CheckResultEvidence(
                        command_name="unit",
                        exit_code=0,
                        passed=True,
                        output_digest="a" * 64,
                        duration_ms=1,
                        receipt_id=str(uuid4()),
                    ),
                ),
            )
        ),
    )
    snapshot = AsyncMock(side_effect=AssertionError("invalid claim must not read Git"))
    verifier = AsyncMock()
    service = SubscriptionHandoffApplication(factory, verifier, snapshot)
    result = await service.apply(child.attempt.attempt_id)
    assert result.disposition == ("handoff_repair_queued" if repairs else "handoff_rejected")
    assert (await service.apply(child.attempt.attempt_id)).replayed
    snapshot.assert_not_awaited()
    verifier.assess.assert_not_awaited()
    async with factory() as work:
        stored = await work.session.get(SubscriptionAttemptResult, child.attempt.attempt_id)
        reason = "invalid_receipt_claims" if claim == "receipts" else "invalid_check_claims"
        assert stored.application_payload["claim_error"] == reason
        record = await work.session.scalar(
            select(SubscriptionDecisionRecord).where(
                SubscriptionDecisionRecord.idempotency_key
                == f"decision-rejection:{child.attempt.attempt_id}"
            )
        )
        assert reason.replace("_", " ") in str(record.payload)
        assert (await work.subscription_budget.usage(child.task.run_id)).consumed.repairs == repairs
        assert (await work.session.get(SubscriptionTask, parent.task.task_id)).state == (
            "blocked" if repairs else "queued"
        )
    if repairs:
        retry = await SubscriptionDecisionExecutor(factory).admit_next(
            "claim-repair",
            replace(
                _reservation(),
                max_duration_seconds=1,
                max_input_tokens=1,
                max_output_tokens=1,
                max_cost_minor=1,
                max_tool_calls=1,
                max_named_checks=0,
            ),
        )
        assert retry is not None and retry.attempt.attempt_number == 2
        assert (await service.apply(child.attempt.attempt_id)).replayed
        async with factory() as work:
            assert (
                await work.session.get(SubscriptionScheduledTask, child.task.task_id)
            ).lease_owner == "claim-repair"
            assert (await work.subscription_budget.usage(child.task.run_id)).consumed.repairs == 1


@pytest.mark.integration
@pytest.mark.parametrize("change", ["valid", "cancel", "expired", "source", "proposal", "rollback"])
async def test_invalid_claim_rejection_requires_current_authority(
    session_factory, tmp_path, monkeypatch, change
):
    factory, _, child, _ = await handoff_case(
        session_factory,
        tmp_path,
        repairs=1,
        mutate_handoff=None
        if change == "valid"
        else lambda value: replace(value, evidence_receipt_ids=("malformed",)),
    )
    application = SubscriptionDecisionApplication(factory)
    observation = await application.begin_handoff_observation(child.attempt.attempt_id, uuid4())
    if change == "proposal":
        observation = replace(
            observation, proposal=replace(observation.proposal, result_digest="f" * 64)
        )
    elif change in {"cancel", "expired", "source"}:
        async with factory() as work:
            if change == "cancel":
                (
                    await work.session.get(SubscriptionTask, child.task.task_id)
                ).cancel_requested = True
            elif change == "expired":
                fence = await work.session.get(
                    SubscriptionHandoffFence, observation.proposal.worktree.identity.worktree_name
                )
                fence.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            else:
                (
                    await work.session.get(SubscriptionAttemptResult, child.attempt.attempt_id)
                ).result_digest = "f" * 64
            await work.commit()
    elif change == "rollback":

        async def fail(*args, **kwargs):
            raise RuntimeError("injected scheduler failure")

        monkeypatch.setattr(PostgresSchedulingRepository, "reconcile_expired", fail)
    with pytest.raises(RuntimeError if change == "rollback" else SubscriptionDecisionError):
        await application.reject_handoff_claim(observation)
    async with factory() as work:
        stored = await work.session.get(SubscriptionAttemptResult, child.attempt.attempt_id)
        assert stored.disposition == "decision_pending" and stored.application_payload is None
        assert (await work.subscription_budget.usage(child.task.run_id)).consumed.repairs == 0


@pytest.mark.integration
async def test_claim_replay_rechecks_reason_against_frozen_source(session_factory, tmp_path):
    factory, _, child, _ = await handoff_case(
        session_factory,
        tmp_path,
        repairs=1,
        mutate_handoff=lambda value: replace(value, evidence_receipt_ids=("malformed",)),
    )
    application = SubscriptionDecisionApplication(factory)
    observation = await application.begin_handoff_observation(child.attempt.attempt_id, uuid4())
    await application.reject_handoff_claim(observation)
    async with factory() as work:
        stored = await work.session.get(SubscriptionAttemptResult, child.attempt.attempt_id)
        stored.application_payload = dict(
            stored.application_payload, claim_error="invalid_check_claims"
        )
        stored.application_digest = canonical_digest(stored.application_payload)
        await work.commit()
    with pytest.raises(SubscriptionDecisionError, match="claim rejection replay differs"):
        await application.handoff_replay(child.attempt.attempt_id)
    async with factory() as work:
        assert (await work.subscription_budget.usage(child.task.run_id)).consumed.repairs == 1
