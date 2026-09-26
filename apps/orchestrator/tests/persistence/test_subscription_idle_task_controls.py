"""Controls preserve already settled work and its pending scheduling meaning."""

from datetime import UTC, datetime, timedelta

import pytest
from forge.application.ports.subscription_gateway import (
    SubscriptionFailure,
    SubscriptionInvocationResult,
)
from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
from forge.application.services.subscription_task_controls import SubscriptionTaskControlService
from forge.domain.provider_quota import QuotaExhaustion
from forge.domain.subscription_quota import QuotaPolicy
from forge.persistence.models.scheduling import SubscriptionScheduledTask
from forge.persistence.models.subscription import SubscriptionTask
from forge.persistence.models.subscription_results import (
    SubscriptionAttemptResult,
    SubscriptionRepairDebit,
)
from forge.persistence.unit_of_work import PostgresUnitOfWork
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_scope_response import response_case
from test_subscription_task_control_recovery import active_case, control, finish_client
from test_subscription_usage import _known, _reservation


@pytest.mark.integration
async def test_idle_quota_pause_resume_keeps_exhaustion_and_budgets(session_factory, tmp_path):
    _, _, child, _, proof = await active_case(session_factory, tmp_path)
    clock = [datetime.now(UTC)]
    factory = lambda: PostgresUnitOfWork(session_factory, quota_clock=lambda: clock[0])
    executor = SubscriptionDecisionExecutor(factory)
    reset = clock[0] + timedelta(minutes=10)
    await finish_client(factory, child, proof)
    assert (
        await executor.settle(
            child,
            SubscriptionInvocationResult(
                attempt=child.attempt,
                failure=SubscriptionFailure.QUOTA,
                telemetry=_known(),
                quota_exhaustion=QuotaExhaustion(clock[0], "provider_usage_exhausted", reset),
                launch_proof=proof,
            ),
        )
    ).disposition == "quota_deferred"
    key = QuotaPolicy().key_for(child.task.route.effective)
    async with factory() as work:
        row = await work.session.get(SubscriptionAttemptResult, child.attempt.attempt_id)
        original = row.result_payload, row.result_digest, row.disposition
        usage = await work.subscription_budget.usage(child.task.run_id, child.task.task_id)
        quota = await work.quota.status(key)
    paused = await control(factory, child, "pause")
    assert paused.status == "paused"
    resumed = await control(factory, child, "resume", pause_id=paused.receipt_id)
    assert resumed.status == "queued"
    async with factory() as work:
        assert await work.subscription_budget.usage(child.task.run_id, child.task.task_id) == usage
        assert await work.quota.status(key) == quota
        assert await work.scheduler._active_count(run_id=child.task.run_id) == 0
        row = await work.session.get(SubscriptionAttemptResult, child.attempt.attempt_id)
        assert (row.result_payload, row.result_digest, row.disposition) == original
    assert await executor.admit_next("still-blocked", _reservation()) is None
    clock[0] = reset
    probe = await SubscriptionDecisionExecutor(factory).admit_next(
        "probe-after-resume", _reservation()
    )
    assert probe is not None and probe.task == child.task and probe.attempt.attempt_number == 2
    async with factory() as work:
        assert (await work.quota.status(key)).probe_attempt_id == probe.attempt.attempt_id
        assert (
            await work.subscription_budget.usage(child.task.run_id, child.task.task_id)
        ).consumed.repairs == 0


@pytest.mark.integration
async def test_quota_result_after_pause_resumes_without_repair_and_stays_deferred(
    session_factory, tmp_path
):
    _, _, child, _, proof = await active_case(session_factory, tmp_path)
    clock = [datetime.now(UTC)]
    factory = lambda: PostgresUnitOfWork(session_factory, quota_clock=lambda: clock[0])
    executor = SubscriptionDecisionExecutor(factory)
    paused = await control(factory, child, "pause")
    await finish_client(factory, child, proof)
    assert (
        await executor.settle(
            child,
            SubscriptionInvocationResult(
                attempt=child.attempt,
                failure=SubscriptionFailure.QUOTA,
                telemetry=_known(),
                quota_exhaustion=QuotaExhaustion(clock[0], "provider_usage_exhausted"),
                launch_proof=proof,
            ),
        )
    ).disposition == "stale"
    assert (await SubscriptionTaskControlService(factory).reconcile_all()).stopped == 1
    assert (await control(factory, child, "resume", pause_id=paused.receipt_id)).status == "queued"
    async with factory() as work:
        assert await work.session.get(SubscriptionRepairDebit, child.attempt.attempt_id) is None
        assert (await work.session.get(SubscriptionScheduledTask, child.task.task_id)).repairs == 0
        assert (
            await work.subscription_budget.usage(child.task.run_id, child.task.task_id)
        ).consumed.repairs == 0
        assert (
            await work.quota.status(QuotaPolicy().key_for(child.task.route.effective))
        ).status == "blocked"
    assert (
        await SubscriptionDecisionExecutor(factory).admit_next("after-paused-quota", _reservation())
        is None
    )


@pytest.mark.integration
async def test_idle_scope_pause_resumes_blocked_then_applies_and_replays(session_factory, tmp_path):
    factory, application, primary, child = await response_case(session_factory, tmp_path)
    async with factory() as work:
        usage = await work.subscription_budget.usage(child.task.run_id, child.task.task_id)
    for _ in range(2):
        paused = await control(factory, child, "pause")
        resumed = await control(factory, child, "resume", pause_id=paused.receipt_id)
        assert resumed.status == "blocked"
    async with factory() as work:
        assert (await work.session.get(SubscriptionTask, child.task.task_id)).state == "blocked"
        assert await work.subscription_budget.usage(child.task.run_id, child.task.task_id) == usage
    assert (
        await SubscriptionDecisionExecutor(factory).admit_next(
            "before-scope-answer", _reservation()
        )
        is None
    )
    assert (await application.apply_scope_response(primary.attempt.attempt_id)).accepted
    async with factory() as work:
        source = await work.session.get(SubscriptionAttemptResult, primary.attempt.attempt_id)
        receipt = source.application_payload, source.application_digest
    # Later controls and contract changes must not change the accepted answer's proof.
    paused = await control(factory, child, "pause")
    assert (await application.apply_scope_response(primary.attempt.attempt_id)).replayed
    assert (await control(factory, child, "resume", pause_id=paused.receipt_id)).status == "queued"
    assert (await application.apply_scope_response(primary.attempt.attempt_id)).replayed
    async with factory() as work:
        source = await work.session.get(SubscriptionAttemptResult, primary.attempt.attempt_id)
        assert (source.application_payload, source.application_digest) == receipt
        assert await work.subscription_budget.usage(child.task.run_id, child.task.task_id) == usage
