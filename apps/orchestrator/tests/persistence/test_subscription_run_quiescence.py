"""Run restoration and teardown include durable subscription ownership."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from forge.application.ports.subscription_gateway import (
    SubscriptionFailure,
    SubscriptionInvocationResult,
)
from forge.persistence.models.scheduling import (
    SubscriptionScheduledEffect,
    SubscriptionScheduledTask,
)
from forge.persistence.models.subscription import SubscriptionAttempt, SubscriptionClientLaunch
from forge.persistence.unit_of_work import PostgresUnitOfWork
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_execution_constraints import _admitted
from test_subscription_usage import _known


@pytest.mark.integration
async def test_subscription_attempt_blocks_quiescence_until_settled(session_factory, persisted_run):
    executor, admission = await _admitted(session_factory, persisted_run)
    async with PostgresUnitOfWork(session_factory) as work:
        proof = await work.runs.prove_quiescent(persisted_run.id)
        assert not proof.is_quiescent
    settled = await executor.settle(
        admission,
        SubscriptionInvocationResult(
            attempt=admission.attempt,
            failure=SubscriptionFailure.PROTOCOL,
            telemetry=_known(),
        ),
    )
    assert settled.disposition == "repair_queued"
    async with PostgresUnitOfWork(session_factory) as work:
        # Queued work has no live effect authority until its fresh admission.
        assert (await work.runs.prove_quiescent(persisted_run.id)).is_quiescent


@pytest.mark.integration
@pytest.mark.parametrize("remaining", ["attempt", "lease", "effect", "client"])
async def test_subscription_residual_authority_blocks_quiescence(
    session_factory, persisted_run, remaining
):
    executor, admission = await _admitted(session_factory, persisted_run)
    await executor.settle(
        admission,
        SubscriptionInvocationResult(
            attempt=admission.attempt,
            failure=SubscriptionFailure.PROTOCOL,
            telemetry=_known(),
        ),
    )
    async with PostgresUnitOfWork(session_factory) as work:
        assert (await work.runs.prove_quiescent(persisted_run.id)).is_quiescent
        # A partial recovery must not hide another resource that remains unsettled.
        if remaining == "attempt":
            attempt = await work.session.get(SubscriptionAttempt, admission.attempt.attempt_id)
            attempt.status = "reconciling"
        elif remaining == "lease":
            task = await work.session.get(SubscriptionScheduledTask, admission.task.task_id)
            task.state = "leased"
            task.lease_owner = admission.lease.owner
            task.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        elif remaining == "effect":
            work.session.add(
                SubscriptionScheduledEffect(
                    id=uuid4(),
                    run_id=persisted_run.id,
                    task_id=admission.task.task_id,
                    lease_owner=admission.lease.owner,
                    lease_generation=admission.lease.generation,
                    candidate_epoch=admission.candidate_epoch,
                    state="reconciling",
                )
            )
        else:
            work.session.add(
                SubscriptionClientLaunch(
                    id=uuid4(),
                    attempt_id=admission.attempt.attempt_id,
                    launch_id="uncertain-client",
                    worker_identity=admission.lease.owner,
                    state="uncertain",
                )
            )
        await work.session.flush()
        assert not (await work.runs.prove_quiescent(persisted_run.id)).is_quiescent
        # Another run's resources do not block this run's worktree.
        assert (await work.runs.prove_quiescent(uuid4())).is_quiescent
