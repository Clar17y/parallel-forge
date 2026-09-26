"""An exhausted fallback does not hide the original approved route after reset."""

from datetime import UTC, datetime, timedelta

import pytest
from forge.application.ports.subscription_gateway import (
    SubscriptionFailure,
    SubscriptionInvocationResult,
)
from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
from forge.domain.provider_quota import QuotaExhaustion
from forge.domain.subscription import SpecialistPurpose
from forge.domain.subscription_quota import QuotaPoolKey
from subscription_launch_fixture import record_stopped_launch
from test_scheduler_acceptance import _remove_disposable_subscription_rows, _route  # noqa: F401
from test_subscription_quota import _factory, _report, _seed
from test_subscription_usage import _known, _reservation


@pytest.mark.integration
async def test_exhausted_fallback_returns_to_exact_frozen_preferred_route(
    session_factory, persisted_run
):
    clock = [datetime.now(UTC)]
    factory = _factory(session_factory, clock)
    _, tasks = await _seed(factory, persisted_run, ("p",), fallbacks=(_route("q"),))
    preferred_key = QuotaPoolKey("p", "local", "subscription-allowance_only")
    fallback_key = QuotaPoolKey("q", "local", "subscription-allowance_only")
    reset_at = clock[0] + timedelta(seconds=10)
    await _report(factory, preferred_key, clock[0], reset=reset_at)
    executor = SubscriptionDecisionExecutor(factory)
    fallback = await executor.admit_next("fallback-worker", _reservation())
    assert fallback is not None and fallback.task.route.effective == _route("q")
    assert fallback.attempt.attempt_number == 1
    outcome = await executor.settle(
        fallback,
        SubscriptionInvocationResult(
            attempt=fallback.attempt,
            failure=SubscriptionFailure.QUOTA,
            quota_exhaustion=QuotaExhaustion(clock[0], "provider_usage_exhausted"),
            telemetry=_known(),
            launch_proof=await record_stopped_launch(session_factory, fallback),
        ),
    )
    assert outcome.disposition == "quota_deferred"
    assert await executor.admit_next("both-blocked", _reservation()) is None
    clock[0] = reset_at
    # A new executor/UOW represents another worker after the original reset.
    restored = await SubscriptionDecisionExecutor(factory).admit_next(
        "preferred-probe", _reservation()
    )
    assert restored is not None
    assert restored.task.task_id == tasks[0]
    assert restored.attempt.attempt_number == 2
    assert restored.task.route == fallback.envelope.route_for(
        SpecialistPurpose.ROUTINE_IMPLEMENTATION
    )
    assert restored.envelope == fallback.envelope
    assert restored.task.owned_paths == fallback.task.owned_paths
    assert restored.task.budget == fallback.task.budget
    async with factory() as work:
        assert (
            await work.quota.status(preferred_key)
        ).probe_attempt_id == restored.attempt.attempt_id
        assert (await work.quota.status(fallback_key)).status == "blocked"
        usage = await work.subscription_budget.usage(persisted_run.id, tasks[0])
        assert usage.consumed.provider_attempts == usage.outstanding.provider_attempts == 1
        assert usage.consumed.repairs == 0
