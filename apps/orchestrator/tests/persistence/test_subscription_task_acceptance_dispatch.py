"""Stopped child-acceptance results complete without another provider invocation."""

import pytest
from forge.application.services.subscription_decision_recovery import SubscriptionDecisionRecovery
from forge.persistence.models.subscription import SubscriptionAttempt
from forge.persistence.models.subscription_results import SubscriptionAttemptResult
from sqlalchemy import func, select
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_acceptance_preparation import acceptance_case
from test_subscription_task_acceptance import task_acceptance_case


@pytest.mark.integration
async def test_recovery_applies_task_acceptance_without_new_attempt(
    session_factory, tmp_path, monkeypatch
):
    factory, primary, _, _ = await task_acceptance_case(session_factory, tmp_path, monkeypatch)
    async with factory() as work:
        count = await work.session.scalar(select(func.count()).select_from(SubscriptionAttempt))
    recovery = SubscriptionDecisionRecovery(factory, object())
    report = await recovery.reconcile_all()
    assert (report.applied, report.deferred, report.unsupported) == (1, 0, 0)
    assert (await recovery.reconcile_all()).applied == 0
    async with factory() as work:
        result = await work.session.get(SubscriptionAttemptResult, primary.attempt.attempt_id)
        assert result.disposition == "task_accepted"
        assert (
            await work.session.scalar(select(func.count()).select_from(SubscriptionAttempt))
            == count
        )


@pytest.mark.integration
async def test_final_acceptance_stays_pending_until_evidence_application_is_wired(
    session_factory, tmp_path
):
    factory, primary, _ = await acceptance_case(session_factory, tmp_path)
    report = await SubscriptionDecisionRecovery(factory, object()).reconcile_all()
    assert (report.applied, report.deferred, report.unsupported) == (0, 0, 1)
    async with factory() as work:
        result = await work.session.get(SubscriptionAttemptResult, primary.attempt.attempt_id)
        assert result.disposition == "decision_pending" and result.application_payload is None
