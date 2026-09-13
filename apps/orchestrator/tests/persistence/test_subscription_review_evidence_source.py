"""Acceptance inputs must come from the current closed candidate's exact sources."""

import pytest
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_candidate_reads import reader_case


@pytest.mark.integration
async def test_no_review_evidence_retains_explicit_selection_without_approval(
    session_factory, tmp_path
):
    factory, primary = await reader_case(session_factory, tmp_path, review_required=False)
    async with factory() as work:
        evidence = await work.subscription_decisions.candidate_review_evidence(
            primary.task.run_id, primary.task.task_id
        )
        assert evidence.run_id == primary.task.run_id
        assert evidence.primary_task_id == primary.task.task_id
        assert evidence.candidate_epoch == primary.candidate_epoch
        assert evidence.selection.review_required is False
        assert evidence.selection.no_review_reason
        assert evidence.review_handoff is None and evidence.review_attempt_id is None
        assert evidence.selection_attempt_id != primary.attempt.attempt_id
        assert evidence.candidate.tree_digest == evidence.selection.candidate_tree_digest


@pytest.mark.integration
@pytest.mark.parametrize(
    "change", ["open", "epoch", "selection_digest", "foreign_primary", "pending_review"]
)
async def test_candidate_review_evidence_rejects_missing_or_stale_sources(
    session_factory, tmp_path, change
):
    from uuid import uuid4

    from forge.application.ports.subscription_decisions import SubscriptionDecisionError
    from forge.persistence.models.scheduling import SubscriptionSchedulerRun
    from forge.persistence.models.subscription import SubscriptionAttempt
    from forge.persistence.models.subscription_results import SubscriptionAttemptResult
    from sqlalchemy import select

    factory, admission = await reader_case(
        session_factory, tmp_path, review_required=change == "pending_review"
    )
    primary_id = (
        admission.task.parent_task_id if change == "pending_review" else admission.task.task_id
    )
    async with factory() as work:
        if change in {"open", "epoch"}:
            scheduler = await work.session.get(SubscriptionSchedulerRun, admission.task.run_id)
            if change == "open":
                scheduler.candidate_state = "open"
            else:
                scheduler.candidate_epoch += 1
        elif change == "selection_digest":
            source = await work.session.scalar(
                select(SubscriptionAttemptResult)
                .join(
                    SubscriptionAttempt,
                    SubscriptionAttempt.id == SubscriptionAttemptResult.attempt_id,
                )
                .where(
                    SubscriptionAttempt.run_id == admission.task.run_id,
                    SubscriptionAttemptResult.disposition == "review_selected",
                )
            )
            source.application_digest = "f" * 64
        await work.commit()
    async with factory() as work:
        with pytest.raises(SubscriptionDecisionError):
            await work.subscription_decisions.candidate_review_evidence(
                admission.task.run_id, uuid4() if change == "foreign_primary" else primary_id
            )
