"""Discriminating invocation lifetime and transaction-boundary checks."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from forge.application.ports.scheduling import SchedulingLeaseRevoked
from forge.application.ports.subscription_execution import (
    SubscriptionAdmission,
    SubscriptionSettlement,
)
from forge.application.ports.subscription_gateway import (
    SubscriptionFailure,
    SubscriptionInterrupted,
    SubscriptionInvocationRequest,
    SubscriptionInvocationResult,
)
from forge.domain.scheduling import TaskLease
from forge.domain.subscription import (
    AttemptIdentity,
    AuthMode,
    BillingMode,
    BrokerAuthorizationBinding,
    ExecutionEnvelope,
    LogicalTaskContract,
    ReasoningEffort,
    RouteBinding,
    RouteSpec,
    SpecialistPurpose,
    TaskBudget,
)
from forge.worker.subscription_runtime import SubscriptionAttemptRunner


def invocation():
    run_id, task_id = uuid4(), uuid4()
    route = RouteSpec(
        provider="openai",
        client="codex_app_server",
        model="gpt-5.6",
        effort=ReasoningEffort.LOW,
        auth_mode=AuthMode.SUBSCRIPTION,
        billing_mode=BillingMode.ALLOWANCE_ONLY,
    )
    binding = RouteBinding(requested=route, effective=route, is_primary=True)
    task = LogicalTaskContract(
        run_id=run_id,
        task_id=task_id,
        purpose=SpecialistPurpose.PRIMARY,
        route=binding,
        budget=TaskBudget(max_duration_seconds=1),
        owned_paths=("src",),
    )
    attempt = AttemptIdentity(run_id=run_id, task_id=task_id, attempt_id=uuid4())
    envelope = ExecutionEnvelope(
        run_id=run_id,
        profile_id=uuid4(),
        profile_version=1,
        safety_policy_version=1,
        routes=((SpecialistPurpose.PRIMARY, binding),),
    )
    request = SubscriptionInvocationRequest(
        task=task,
        attempt=attempt,
        envelope=envelope,
        authorization=BrokerAuthorizationBinding(
            run_id=run_id,
            task_id=task_id,
            attempt_id=attempt.attempt_id,
            worktree_id="test",
            role=task.purpose,
            policy_version=1,
            permitted_tools=frozenset(),
            broker_token="test",
        ),
        prompt_version="v1",
        trusted_system_prompt="Return typed output",
        untrusted_context={},
    )
    admission = SubscriptionAdmission(
        TaskLease(
            run_id=run_id,
            task_id=task_id,
            owner="worker",
            generation=1,
            expires_at=datetime.now(UTC) + timedelta(seconds=30),
        ),
        task,
        attempt,
        envelope,
        0,
        1,
    )
    return admission, request


class WorkFactory:
    def __init__(self, fail_renewal=None):
        self.active = 0
        self.renewals = 0
        self.results = []
        self.fail_renewal = fail_renewal

    async def renew(self, lease, duration):
        self.renewals += 1
        if self.fail_renewal is not None and self.renewals >= self.fail_renewal:
            raise SchedulingLeaseRevoked("stopped lease")
        return lease

    async def settle(self, admission, result):
        self.results.append(result)
        return SubscriptionSettlement(False, "recorded")

    async def commit(self):
        pass

    @asynccontextmanager
    async def __call__(self):
        self.active += 1
        try:
            yield SimpleNamespace(scheduler=self, subscription_execution=self, commit=self.commit)
        finally:
            self.active -= 1


@pytest.mark.parametrize(
    "values",
    [
        {"heartbeat_seconds": 5, "lease_seconds": 5},
        {"heartbeat_seconds": float("nan")},
        {"lease_seconds": float("inf")},
        {"lease_seconds": 86401},
        {"cleanup_seconds": True},
        {"lease_seconds": 0.5},
    ],
)
def test_attempt_runner_rejects_invalid_timings(values):
    with pytest.raises(ValueError):
        SubscriptionAttemptRunner(lambda: None, **values)


async def test_zero_reserved_duration_never_invokes_provider():
    from dataclasses import replace

    admission, request = invocation()
    request = replace(
        request,
        attempt_budget=replace(
            request.task.budget, max_duration_seconds=0, max_provider_attempts=1, max_repairs=0
        ),
    )
    factory = WorkFactory()
    calls = []

    class Gateway:
        async def execute(self, value):
            calls.append(value)
            return successful(value)

    async def revoke():
        pass

    result = await SubscriptionAttemptRunner(factory).execute(admission, request, Gateway(), revoke)
    assert result.result.failure is SubscriptionFailure.DEADLINE
    assert calls == [] and len(factory.results) == 1


async def test_prelaunch_stop_settles_without_invoking_provider():
    admission, request = invocation()
    factory = WorkFactory(fail_renewal=1)
    order = []

    class Gateway:
        async def execute(self, request):
            raise AssertionError("provider launched after stop")

    async def revoke():
        assert factory.active == 0
        order.append("revoke")

    outcome = await SubscriptionAttemptRunner(factory).execute(
        admission, request, Gateway(), revoke
    )
    assert order == ["revoke"]
    assert len(factory.results) == 1 and outcome.result.failure is not None


def successful(request):
    from forge.domain.subscription import AttemptTelemetry, WaitDecision

    return SubscriptionInvocationResult(
        attempt=request.attempt,
        decision=WaitDecision(
            run_id=request.task.run_id,
            task_id=request.task.task_id,
            waiting_on_task_ids=(uuid4(),),
            reason="await child",
        ),
        telemetry=AttemptTelemetry(input_tokens=23),
    )


async def test_success_renews_then_revokes_and_settles_once_outside_provider_io():
    admission, request = invocation()
    factory = WorkFactory()
    order = []

    class Gateway:
        async def execute(self, value):
            assert factory.active == 0 and factory.renewals == 1
            order.append("execute")
            return successful(value)

    async def revoke():
        assert factory.active == 0
        order.append("revoke")

    result = await SubscriptionAttemptRunner(factory).execute(admission, request, Gateway(), revoke)
    assert order == ["execute", "revoke"]
    assert factory.renewals == 2 and len(factory.results) == 1
    assert result.result.failure is None


@pytest.mark.parametrize("trigger", ["renewal", "external", "deadline"])
async def test_interruption_revokes_before_cancel_and_preserves_measurement(trigger):
    admission, request = invocation()
    factory = WorkFactory(fail_renewal=2 if trigger == "renewal" else None)
    started = asyncio.Event()
    order = []
    measured = successful(request).telemetry

    class Gateway:
        async def execute(self, value):
            assert factory.active == 0
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                order.append("cancel")
                raise SubscriptionInterrupted(
                    SubscriptionInvocationResult(
                        attempt=value.attempt,
                        failure=SubscriptionFailure.INTERRUPTED,
                        telemetry=measured,
                    )
                ) from None

    async def revoke():
        assert factory.active == 0
        order.append("revoke")

    runner = SubscriptionAttemptRunner(
        factory, heartbeat_seconds=0.01, lease_seconds=1, cleanup_seconds=0.05
    )
    execution = asyncio.create_task(runner.execute(admission, request, Gateway(), revoke))
    await asyncio.wait_for(started.wait(), 1)
    if trigger == "external":
        execution.cancel()
        with pytest.raises(SubscriptionInterrupted) as caught:
            await execution
        result = caught.value.result
    else:
        result = (await asyncio.wait_for(execution, 2)).result
    assert order == ["revoke", "cancel"]
    assert result.telemetry == measured and result.failure is not None
    assert len(factory.results) == 1 and factory.active == 0


async def test_completed_provider_cannot_win_failed_final_renewal():
    admission, request = invocation()
    factory = WorkFactory(fail_renewal=2)

    class Gateway:
        async def execute(self, value):
            return successful(value)

    async def revoke():
        pass

    outcome = await SubscriptionAttemptRunner(factory).execute(
        admission, request, Gateway(), revoke
    )
    assert outcome.result.failure is SubscriptionFailure.INTERRUPTED
    assert outcome.result.decision is None and outcome.result.telemetry.input_tokens == 23


@pytest.mark.parametrize("bad", ["bool", "exception", "foreign"])
async def test_malformed_provider_outcome_settles_protocol_failure(bad):
    admission, request = invocation()
    factory = WorkFactory()

    class Gateway:
        async def execute(self, value):
            if bad == "bool":
                return True
            if bad == "exception":
                raise RuntimeError("untrusted provider text")
            _, other = invocation()
            return successful(other)

    async def revoke():
        pass

    outcome = await SubscriptionAttemptRunner(factory).execute(
        admission, request, Gateway(), revoke
    )
    assert outcome.result.failure is SubscriptionFailure.PROTOCOL
    assert len(factory.results) == 1


async def test_failed_revoke_prevents_success():
    admission, request = invocation()
    factory = WorkFactory()

    class Gateway:
        async def execute(self, value):
            return successful(value)

    async def revoke():
        raise RuntimeError("revocation unavailable")

    outcome = await SubscriptionAttemptRunner(factory).execute(
        admission, request, Gateway(), revoke
    )
    assert outcome.result.failure is SubscriptionFailure.UNCERTAIN
    assert outcome.result.telemetry.input_tokens == 23


async def test_noncooperative_provider_is_bounded_and_eventually_observed():
    admission, request = invocation()
    factory = WorkFactory(fail_renewal=2)
    release = asyncio.Event()
    stopped = asyncio.Event()

    class Gateway:
        async def execute(self, value):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()
                stopped.set()
                raise RuntimeError("late failure must be observed")

    async def revoke():
        pass

    runner = SubscriptionAttemptRunner(
        factory, heartbeat_seconds=0.01, lease_seconds=1, cleanup_seconds=0.01
    )
    try:
        outcome = await asyncio.wait_for(runner.execute(admission, request, Gateway(), revoke), 0.5)
        assert outcome.result.failure is SubscriptionFailure.UNCERTAIN
        assert outcome.result.telemetry.input_tokens is None
        assert len(factory.results) == 1 and runner._pending
    finally:
        release.set()
        await asyncio.wait_for(stopped.wait(), 1)
        await asyncio.sleep(0)
    assert not runner._pending


async def test_uncertain_gateway_cleanup_is_not_downgraded_by_lease_failure():
    admission, request = invocation()
    factory = WorkFactory(fail_renewal=2)

    class Gateway:
        async def execute(self, value):
            return SubscriptionInvocationResult(
                attempt=value.attempt, failure=SubscriptionFailure.UNCERTAIN
            )

    async def revoke():
        pass

    outcome = await SubscriptionAttemptRunner(factory).execute(
        admission, request, Gateway(), revoke
    )
    assert outcome.result.failure is SubscriptionFailure.UNCERTAIN


async def test_completion_during_failed_heartbeat_cannot_escape_control_fence():
    admission, request = invocation()
    factory = WorkFactory()
    renewal_started, finish_provider, fail_renewal = (asyncio.Event() for _ in range(3))
    original = factory.renew

    async def renew(lease, duration):
        if factory.renewals == 1:
            renewal_started.set()
            await fail_renewal.wait()
            raise ValueError("lease lost during completion")
        return await original(lease, duration)

    factory.renew = renew

    class Gateway:
        async def execute(self, value):
            await finish_provider.wait()
            return successful(value)

    async def revoke():
        pass

    runner = SubscriptionAttemptRunner(factory, heartbeat_seconds=0.01, lease_seconds=1)
    execution = asyncio.create_task(runner.execute(admission, request, Gateway(), revoke))
    await asyncio.wait_for(renewal_started.wait(), 1)
    finish_provider.set()
    await asyncio.sleep(0)
    fail_renewal.set()
    outcome = await asyncio.wait_for(execution, 1)
    assert outcome.result.failure is SubscriptionFailure.UNCERTAIN
    assert outcome.result.telemetry.input_tokens == 23


async def test_repeated_external_cancellation_does_not_interrupt_revocation_or_settlement():
    admission, request = invocation()
    factory = WorkFactory()
    started, revoking, release = (asyncio.Event() for _ in range(3))
    order = []

    class Gateway:
        async def execute(self, value):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                order.append("cancel")
                raise SubscriptionInterrupted(
                    SubscriptionInvocationResult(
                        attempt=value.attempt,
                        failure=SubscriptionFailure.INTERRUPTED,
                        telemetry=successful(value).telemetry,
                    )
                ) from None

    async def revoke():
        revoking.set()
        await release.wait()
        order.append("revoke")

    execution = asyncio.create_task(
        SubscriptionAttemptRunner(factory).execute(admission, request, Gateway(), revoke)
    )
    await asyncio.wait_for(started.wait(), 1)
    execution.cancel()
    await asyncio.wait_for(revoking.wait(), 1)
    execution.cancel()
    release.set()
    with pytest.raises(SubscriptionInterrupted) as caught:
        await asyncio.wait_for(execution, 1)
    assert caught.value.result.telemetry.input_tokens == 23
    assert order == ["revoke", "cancel"] and len(factory.results) == 1


async def test_noncooperative_revoke_is_bounded_and_observed():
    admission, request = invocation()
    factory = WorkFactory()
    release, finished = asyncio.Event(), asyncio.Event()

    class Gateway:
        async def execute(self, value):
            return successful(value)

    async def revoke():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()
            finished.set()
            raise RuntimeError("late revoke failure") from None

    runner = SubscriptionAttemptRunner(factory, cleanup_seconds=0.01)
    try:
        outcome = await asyncio.wait_for(runner.execute(admission, request, Gateway(), revoke), 0.5)
        assert outcome.result.failure is SubscriptionFailure.UNCERTAIN and len(factory.results) == 1
    finally:
        release.set()
        await asyncio.wait_for(finished.wait(), 1)
        await asyncio.sleep(0)
    assert not runner._pending


async def test_request_identity_mismatch_never_opens_work_or_calls_provider():
    admission, _ = invocation()
    _, foreign = invocation()
    factory = WorkFactory()
    with pytest.raises(ValueError, match="differs"):
        await SubscriptionAttemptRunner(factory).execute(admission, foreign, None, None)
    assert factory.renewals == 0 and not factory.results


@pytest.mark.parametrize("lost_lease", [False, True])
async def test_database_renewal_failure_is_uncertain_not_policy_denied(lost_lease):
    admission, request = invocation()
    factory = WorkFactory()

    async def renew(lease, duration):
        if lost_lease:
            from forge.application.ports.scheduling import SchedulingLeaseLost

            raise SchedulingLeaseLost("lease ownership changed")
        raise RuntimeError("database connection unavailable")

    factory.renew = renew

    class Gateway:
        async def execute(self, value):
            raise AssertionError("must not launch")

    async def revoke():
        pass

    outcome = await SubscriptionAttemptRunner(factory).execute(
        admission, request, Gateway(), revoke
    )
    assert outcome.result.failure is SubscriptionFailure.UNCERTAIN


async def test_provider_failure_detail_survives_same_runner_classification():
    from dataclasses import replace

    admission, request = invocation()
    factory = WorkFactory()

    class Gateway:
        async def execute(self, value):
            raise SubscriptionInterrupted(
                replace(
                    successful(value),
                    decision=None,
                    failure=SubscriptionFailure.INTERRUPTED,
                    failure_detail="Client confirmed interruption",
                )
            )

    async def revoke():
        pass

    outcome = await SubscriptionAttemptRunner(factory).execute(
        admission, request, Gateway(), revoke
    )
    assert outcome.result.failure_detail == "Client confirmed interruption"
