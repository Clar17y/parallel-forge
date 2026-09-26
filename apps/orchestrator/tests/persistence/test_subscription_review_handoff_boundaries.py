"""Closed review handoffs cannot cross selection, epoch, or control boundaries."""

import pytest
from forge.application.ports.subscription_decisions import SubscriptionDecisionError
from forge.application.services.subscription_decisions import SubscriptionDecisionApplication
from forge.persistence.models.scheduling import SubscriptionSchedulerRun
from forge.persistence.models.subscription import SubscriptionAttempt, SubscriptionTask
from forge.persistence.models.subscription_results import SubscriptionAttemptResult
from sqlalchemy import select
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_review_handoff import review_handoff_case


@pytest.mark.integration
@pytest.mark.parametrize("change", ["epoch", "draining", "selection", "missing", "cancel"])
async def test_review_handoff_requires_current_selected_candidate(
    session_factory, tmp_path, change
):
    factory, child, _ = await review_handoff_case(session_factory, tmp_path)
    async with factory() as work:
        if change in {"epoch", "draining"}:
            scheduler = await work.session.get(SubscriptionSchedulerRun, child.task.run_id)
            if change == "epoch":
                scheduler.candidate_epoch += 1
            else:
                scheduler.candidate_state = "draining"
        elif change == "cancel":
            task = await work.session.get(SubscriptionTask, child.task.task_id)
            task.cancel_requested = True
        else:
            source = await work.session.scalar(
                select(SubscriptionAttemptResult)
                .join(
                    SubscriptionAttempt,
                    SubscriptionAttempt.id == SubscriptionAttemptResult.attempt_id,
                )
                .where(
                    SubscriptionAttempt.task_row_id == child.task.parent_task_id,
                    SubscriptionAttemptResult.disposition == "review_selected",
                )
            )
            if change == "selection":
                source.application_digest = "f" * 64
            else:
                source.disposition = "candidate_prepared"
        await work.commit()
    with pytest.raises(SubscriptionDecisionError):
        await SubscriptionDecisionApplication(factory).handoff_proposal(child.attempt.attempt_id)
