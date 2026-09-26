"""Real durable admission and controls around a controlled provider port."""

import asyncio
from contextlib import asynccontextmanager

import pytest
from forge.application.ports.subscription_gateway import (
    SubscriptionFailure,
    SubscriptionInterrupted,
    SubscriptionInvocationRequest,
    SubscriptionInvocationResult,
)
from forge.domain.subscription import BrokerAuthorizationBinding, HandoffStatus, TaskHandoff
from forge.persistence.unit_of_work import PostgresUnitOfWork
from forge.worker.subscription_runtime import SubscriptionAttemptRunner
from subscription_launch_fixture import record_stopped_launch
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_execution_constraints import _admitted
from test_subscription_usage import _known


def request_for(admission):
    return SubscriptionInvocationRequest(
        task=admission.task,
        attempt=admission.attempt,
        envelope=admission.envelope,
        authorization=BrokerAuthorizationBinding(
            run_id=admission.task.run_id,
            task_id=admission.task.task_id,
            attempt_id=admission.attempt.attempt_id,
            worktree_id="constraint-tree",
            role=admission.task.purpose,
            policy_version=admission.envelope.safety_policy_version,
            permitted_tools=frozenset(),
            broker_token="test-only-authority",
        ),
        prompt_version="test-v1",
        trusted_system_prompt="Return a typed decision.",
        untrusted_context={},
    )


class TrackedWork:
    def __init__(self, session_factory):
        self.sessions = session_factory
        self.active = 0
        self.opened = 0

    @asynccontextmanager
    async def __call__(self):
        async with PostgresUnitOfWork(self.sessions) as work:
            self.active += 1
            self.opened += 1
            try:
                yield work
            finally:
                self.active -= 1


@pytest.mark.integration
async def test_runner_settles_real_admission_after_provider_io(session_factory, persisted_run):
    executor, admission = await _admitted(session_factory, persisted_run)
    factory = TrackedWork(session_factory)

    class Gateway:
        async def execute(self, request):
            assert factory.active == 0
            proof = await record_stopped_launch(session_factory, admission)
            return SubscriptionInvocationResult(
                attempt=request.attempt,
                decision=TaskHandoff(
                    run_id=request.task.run_id,
                    task_id=request.task.task_id,
                    attempt_id=request.attempt.attempt_id,
                    status=HandoffStatus.COMPLETED,
                    candidate_tree_digest="a" * 64,
                    evidence_receipt_ids=("not-yet-accepted",),
                ),
                telemetry=_known(input_tokens=17),
                launch_proof=proof,
            )

    async def revoke():
        assert factory.active == 0

    outcome = await SubscriptionAttemptRunner(factory).execute(
        admission, request_for(admission), Gateway(), revoke
    )
    assert outcome.settlement.disposition == "decision_pending"
    assert not outcome.settlement.accepted
    assert factory.active == 0 and factory.opened >= 2
    assert (await executor.settle(admission, outcome.result)).replayed
    async with factory() as work:
        usage = await work.subscription_budget.usage(admission.task.run_id)
        assert usage.consumed.provider_attempts == 1
        assert usage.consumed.input_tokens == 17
        assert usage.outstanding.provider_attempts == 0


@pytest.mark.integration
async def test_runner_observes_durable_pause_and_preserves_usage(session_factory, persisted_run):
    _, admission = await _admitted(session_factory, persisted_run)
    factory = TrackedWork(session_factory)
    started = asyncio.Event()
    order = []

    class Gateway:
        async def execute(self, request):
            assert factory.active == 0
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                order.append("cancel")
                raise SubscriptionInterrupted(
                    SubscriptionInvocationResult(
                        attempt=request.attempt,
                        failure=SubscriptionFailure.INTERRUPTED,
                        telemetry=_known(input_tokens=23),
                    )
                ) from None

    async def revoke():
        assert factory.active == 0
        order.append("revoke")

    runner = SubscriptionAttemptRunner(factory, heartbeat_seconds=0.05, lease_seconds=5)
    execution = asyncio.create_task(
        runner.execute(admission, request_for(admission), Gateway(), revoke)
    )
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        async with PostgresUnitOfWork(session_factory) as work:
            run = await work.runs.get(admission.task.run_id)
            await work.runs.pause(run.id, run.version, "run.paused", {}, actor_class="operator")
            await work.commit()
        outcome = await asyncio.wait_for(execution, timeout=5)
        assert order == ["revoke", "cancel"]
        assert not outcome.settlement.accepted
        assert outcome.result.failure is not None
        async with factory() as work:
            usage = await work.subscription_budget.usage(admission.task.run_id)
            assert usage.consumed.provider_attempts == 1
            assert usage.consumed.input_tokens == 23
    finally:
        if not execution.done():
            execution.cancel()
        await asyncio.gather(execution, return_exceptions=True)
