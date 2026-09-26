"""The invocation owner accounts for shutdown and failed setup exactly once."""

import asyncio
from types import SimpleNamespace

import pytest
from forge.application.ports.subscription_gateway import (
    SubscriptionFailure,
    SubscriptionInvocationResult,
)
from forge.domain.subscription import TaskBudget
from forge.worker.subscription_invocation import (
    SubscriptionInvocationSession,
    SubscriptionInvocationWorker,
)
from test_subscription_attempt_runner import WorkFactory, invocation


def worker_case(session_factory, **options):
    admission, request = invocation()
    work = WorkFactory()
    worker = SubscriptionInvocationWorker(
        work,
        session_factory,
        artifacts=object(),
        owner="worker",
        reservation=TaskBudget(max_provider_attempts=1, max_repairs=0),
        **options,
    )

    async def admit(*args, **kwargs):
        return admission

    async def build(value):
        assert value == admission
        return request

    worker._executor = SimpleNamespace(admit_next=admit, settle=work.settle)
    worker._requests = SimpleNamespace(build=build)
    return worker, work, admission, request


async def test_repeated_shutdown_during_request_build_settles_without_provider():
    calls = []
    worker, work, _, request = worker_case(lambda *args: calls.append(args))
    building, release = asyncio.Event(), asyncio.Event()

    async def build(admission):
        building.set()
        await release.wait()
        return request

    worker._requests = SimpleNamespace(build=build)
    operation = asyncio.create_task(worker.run_once())
    await asyncio.wait_for(building.wait(), 1)
    operation.cancel()
    await asyncio.sleep(0)
    operation.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(operation, 1)
    assert calls == [] and len(work.results) == 1
    assert work.results[0].failure is SubscriptionFailure.INTERRUPTED
    assert work.results[0].telemetry.input_tokens == 0


@pytest.mark.parametrize(
    "error,failure",
    [
        (ValueError("invalid binding"), SubscriptionFailure.POLICY_DENIED),
        (RuntimeError("unavailable client"), SubscriptionFailure.UNAVAILABLE),
    ],
)
async def test_session_setup_failure_is_accounted_without_releasing_unknown_usage(error, failure):
    def session(*args):
        raise error

    worker, work, _, _ = worker_case(session)
    outcome = await worker.run_once()
    assert outcome.application is None
    assert len(work.results) == 1 and work.results[0].failure is failure
    assert work.results[0].telemetry.tool_call_count == 0
    assert str(error) not in work.results[0].failure_detail


async def test_repeated_shutdown_during_provider_revokes_before_cancel_and_settles():
    started, revoking, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    order = []

    def session(admission, request):
        class Gateway:
            async def execute(self, value):
                started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    order.append("cancel")
                    return SubscriptionInvocationResult(
                        attempt=value.attempt, failure=SubscriptionFailure.INTERRUPTED
                    )

        async def revoke():
            order.append("revoke")
            revoking.set()
            await release.wait()

        return SubscriptionInvocationSession(Gateway(), revoke)

    worker, work, _, _ = worker_case(session)
    operation = asyncio.create_task(worker.run_once())
    await asyncio.wait_for(started.wait(), 1)
    operation.cancel()
    await asyncio.wait_for(revoking.wait(), 1)
    operation.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(operation, 1)
    assert order == ["revoke", "cancel"]
    assert len(work.results) == 1
    assert work.results[0].failure is SubscriptionFailure.INTERRUPTED


async def test_stopped_worker_never_admits():
    worker, work, _, _ = worker_case(lambda *args: None)

    async def forbidden(*args, **kwargs):
        raise AssertionError("must not admit")

    worker._executor.admit_next = forbidden
    stop = asyncio.Event()
    stop.set()
    assert await worker.run_once(stop_event=stop) is None
    assert work.results == []


async def test_stop_during_admission_accounts_for_committed_attempt():
    worker, work, admission, _ = worker_case(
        lambda *args: pytest.fail("must not construct session")
    )
    stop = asyncio.Event()

    async def admit(*args, **kwargs):
        stop.set()
        return admission

    worker._executor.admit_next = admit
    outcome = await worker.run_once(stop_event=stop)
    assert outcome.attempt.result.failure is SubscriptionFailure.INTERRUPTED
    assert len(work.results) == 1


@pytest.mark.parametrize("reservation", [TaskBudget(), TaskBudget(max_provider_attempts=1)])
def test_worker_requires_one_attempt_reservation(reservation):
    with pytest.raises(ValueError, match="exactly one"):
        SubscriptionInvocationWorker(
            lambda: None,
            lambda *args: None,
            artifacts=object(),
            owner="worker",
            reservation=reservation,
        )


@pytest.mark.parametrize("disposition", ["decision_pending", "stale", "fenced"])
@pytest.mark.parametrize("scope_request", [False, True, "response", "reassign"])
async def test_only_pending_settlement_is_dispatched(disposition, scope_request):
    from uuid import uuid4

    from forge.application.ports.subscription_execution import SubscriptionSettlement
    from forge.domain.subscription import (
        BoundReassignDecision,
        BoundScopeResponseDecision,
        ScopeRequestDecision,
        WaitDecision,
    )

    def session(admission, request):
        class Gateway:
            async def execute(self, value):
                return SubscriptionInvocationResult(
                    attempt=value.attempt,
                    decision=BoundReassignDecision(
                        run_id=value.attempt.run_id,
                        task_id=uuid4(),
                        source_attempt_id=uuid4(),
                        expected_task_version=3,
                        new_route=value.task.route.effective,
                        reason="Approved specialist",
                    )
                    if scope_request == "reassign"
                    else BoundScopeResponseDecision(
                        run_id=value.attempt.run_id,
                        task_id=uuid4(),
                        request_attempt_id=uuid4(),
                        denied_paths=("src",),
                        reason="Denied",
                    )
                    if scope_request == "response"
                    else ScopeRequestDecision(
                        run_id=value.attempt.run_id,
                        task_id=value.attempt.task_id,
                        requested_paths=("src",),
                        reason="Need scope",
                    )
                    if scope_request
                    else WaitDecision(
                        run_id=value.attempt.run_id,
                        task_id=value.attempt.task_id,
                        waiting_on_task_ids=(uuid4(),),
                        reason="Wait for bounded child",
                    ),
                )

        async def revoke():
            pass

        return SubscriptionInvocationSession(Gateway(), revoke)

    worker, work, admission, _ = worker_case(session)

    async def settle(value, result):
        work.results.append(result)
        return SubscriptionSettlement(False, disposition)

    work.settle = settle
    applied = []

    async def apply_wait(attempt_id):
        assert len(work.results) == 1
        applied.append(attempt_id)
        return SubscriptionSettlement(True, "waiting")

    worker._decisions = SimpleNamespace(
        apply_wait=apply_wait,
        apply_scope_request=apply_wait,
        apply_scope_response=apply_wait,
        apply_reassignment=apply_wait,
    )
    outcome = await worker.run_once()
    assert applied == ([admission.attempt.attempt_id] if disposition == "decision_pending" else [])
    assert (outcome.application is not None) == (disposition == "decision_pending")
