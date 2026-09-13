"""Typed reviewer reports remain attached to their stopped durable handoff."""

from dataclasses import fields

import pytest
from forge.application.ports.subscription_decisions import PendingDecisionKind
from forge.application.services.subscription_decisions import SubscriptionDecisionApplication
from forge.domain.agent import ReviewDecision, ReviewOutput
from forge.domain.subscription import ReviewedTaskHandoff
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_review_handoff import review_handoff_case


def reviewed(handoff, decision=ReviewDecision.APPROVE):
    return ReviewedTaskHandoff(
        **{field.name: getattr(handoff, field.name) for field in fields(handoff)},
        review_output=ReviewOutput(
            decision=decision,
            tested_claims=("Inspected tree",),
            missing_evidence=()
            if decision is ReviewDecision.APPROVE
            else ("Validation needs attention",),
            summary="No findings",
        ),
    )


@pytest.mark.integration
async def test_review_report_is_discovered_and_loaded_as_handoff(session_factory, tmp_path):
    factory, child, handoff = await review_handoff_case(session_factory, tmp_path, mutate=reviewed)
    async with factory() as work:
        pending = await work.subscription_decisions.pending_applications(None, 100)
        row = next(item for item in pending if item.attempt_id == child.attempt.attempt_id)
        assert row.kind is PendingDecisionKind.HANDOFF
    proposal = await SubscriptionDecisionApplication(factory).handoff_proposal(
        child.attempt.attempt_id
    )
    assert proposal.handoff == handoff
    assert proposal.handoff.review_output.decision is ReviewDecision.APPROVE
