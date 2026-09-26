"""Intrinsic contradictions need no artifact or filesystem observation."""

from dataclasses import replace
from uuid import uuid4

import pytest
from forge.domain.subscription import AcceptanceCriterion, CheckResultEvidence
from test_subscription_handoff import case


def test_completed_handoff_retains_check_history_ending_in_success():
    from forge.application.ports.subscription_handoff import handoff_claim_error

    _, _, handoff, kwargs = case()
    task = replace(kwargs["task"], named_checks=("unit",))
    failed, passed = (str(uuid4()) for _ in range(2))
    history = (
        CheckResultEvidence(
            command_name="unit",
            exit_code=1,
            passed=False,
            output_digest="a" * 64,
            duration_ms=1,
            receipt_id=failed,
        ),
        CheckResultEvidence(
            command_name="unit",
            exit_code=0,
            passed=True,
            output_digest="b" * 64,
            duration_ms=1,
            receipt_id=passed,
        ),
    )
    completed = replace(
        handoff,
        check_results=history,
        evidence_receipt_ids=(*handoff.evidence_receipt_ids, failed, passed),
    )
    assert handoff_claim_error(completed, task) is None
    assert (
        handoff_claim_error(replace(completed, check_results=history[::-1]), task)
        == "invalid_check_claims"
    )


@pytest.mark.parametrize(
    "change",
    [
        "valid",
        "malformed",
        "uppercase",
        "duplicate",
        "missing_check",
        "typed_check",
        "unlisted_check",
        "failed_check",
        "duplicate_check",
        "valid_check",
    ],
)
def test_claim_invalidity_is_proved_by_frozen_claim(change):
    from forge.application.ports.subscription_handoff import handoff_claim_error

    _, _, handoff, kwargs = case()
    task = kwargs["task"]
    if change == "malformed":
        handoff = replace(handoff, evidence_receipt_ids=("not-a-receipt",))
    elif change == "uppercase":
        handoff = replace(handoff, evidence_receipt_ids=("ABCDEFAB-1234-1234-1234-123456789ABC",))
    elif change == "duplicate":
        handoff = replace(handoff, evidence_receipt_ids=handoff.evidence_receipt_ids * 2)
    elif change == "missing_check":
        task = replace(task, named_checks=("unit",))
    elif change == "typed_check":
        task = replace(
            task,
            typed_acceptance=(
                AcceptanceCriterion(
                    criterion_id="check",
                    description="Unit checks pass",
                    required_check_names=("unit",),
                ),
            ),
        )
    elif change in {"unlisted_check", "failed_check", "duplicate_check", "valid_check"}:
        identity = str(uuid4())
        if change != "unlisted_check":
            handoff = replace(
                handoff, evidence_receipt_ids=(*handoff.evidence_receipt_ids, identity)
            )
        handoff = replace(
            handoff,
            check_results=(
                CheckResultEvidence(
                    command_name="unit",
                    exit_code=1 if change == "failed_check" else 0,
                    passed=change != "failed_check",
                    output_digest="a" * 64,
                    duration_ms=1,
                    receipt_id=identity,
                ),
            ),
        )
        if change == "duplicate_check":
            handoff = replace(handoff, check_results=handoff.check_results * 2)
    reason = handoff_claim_error(handoff, task)
    if change in {"valid", "valid_check"}:
        assert reason is None
    else:
        assert reason == (
            "invalid_check_claims"
            if change
            in {"missing_check", "typed_check", "unlisted_check", "failed_check", "duplicate_check"}
            else "invalid_receipt_claims"
        )
