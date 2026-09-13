"""A quota-selected fallback remains valid through real handoff and acceptance."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import test_subscription_plan_gate as plan_fixture
from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
from forge.domain.provider_quota import QuotaExhaustion
from forge.persistence.models.scheduling import SubscriptionScheduledTask
from forge.persistence.models.subscription import SubscriptionAttempt
from sqlalchemy import func, select
from test_scheduler_acceptance import (  # noqa: F401
    _admit_run,
    _remove_disposable_subscription_rows,
    _route,
)
from test_subscription_acceptance_receipt_integration import retained_receipts_case


@pytest.mark.integration
async def test_quota_fallback_handoff_remains_acceptance_evidence(
    session_factory, tmp_path, monkeypatch
):
    limited, fallback = _route("limited"), _route("fallback")
    primary_routes = []

    async def admit_profile(work, run, routes, **kwargs):
        primary_routes.append(routes[0])
        return await _admit_run(
            work, run, (routes[0], limited), worker_fallbacks=(fallback,), **kwargs
        )

    original_admit = SubscriptionDecisionExecutor.admit_next
    admissions = []

    async def admit_after_report(executor, owner, reservation, **kwargs):
        if owner == "worker":
            async with executor._work_factory() as work:
                now = datetime.now(UTC)
                await work.quota.report_exhaustion(
                    work.quota.policy.key_for(limited),
                    QuotaExhaustion(now, "operator_report", now + timedelta(hours=1)),
                    actor_id=uuid4(),
                    idempotency_key="fallback-integration",
                )
                await work.commit()
        admission = await original_admit(executor, owner, reservation, **kwargs)
        assert admission is not None
        admissions.append(admission)
        if owner == "worker":
            assert admission.task.route.requested == limited
            assert admission.task.route.effective == fallback
            assert admission.attempt.attempt_number == 1
            assert admission.task.owned_paths == ("apps/feature",)
        else:
            assert admission.task.route.effective == primary_routes[0]
            assert admission.task.route.is_primary
        return admission

    # Configure the approved profile before it is frozen; all scheduling,
    # broker receipts, stopped results, handoff and acceptance checks remain real.
    monkeypatch.setattr(plan_fixture, "_admit_run", admit_profile)
    monkeypatch.setattr(SubscriptionDecisionExecutor, "admit_next", admit_after_report)
    factory, _, worker, proof = await retained_receipts_case(session_factory, tmp_path)
    assert proof.receipts[0].attempt_id == worker.attempt.attempt_id
    assert len([item for item in admissions if item.task.task_id == worker.task.task_id]) == 1
    async with factory() as work:
        usage = await work.subscription_budget.usage(worker.task.run_id, worker.task.task_id)
        assert usage.consumed.provider_attempts == 1 and usage.consumed.repairs == 0
        assert usage.outstanding.provider_attempts == 0
        scheduled = await work.session.get(SubscriptionScheduledTask, worker.task.task_id)
        assert scheduled.state == "terminal" and scheduled.repairs == 0
        assert (
            await work.session.scalar(
                select(func.count())
                .select_from(SubscriptionAttempt)
                .where(SubscriptionAttempt.task_row_id == worker.task.task_id)
            )
            == 1
        )
        assert (await work.quota.status(work.quota.policy.key_for(limited))).status == "blocked"
