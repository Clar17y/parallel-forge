"""Run and task stops preserve confirmed exhaustion and its retry accounting."""

from datetime import UTC, datetime

import pytest
from forge.application.handlers.run_controls import ResumeRunHandler
from forge.application.ports.subscription_gateway import (
    SubscriptionFailure,
    SubscriptionInvocationResult,
)
from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
from forge.domain.provider_quota import QuotaExhaustion
from forge.domain.subscription_quota import QuotaPolicy
from forge.persistence.models.scheduling import SubscriptionScheduledTask
from forge.persistence.models.subscription import SubscriptionTask
from forge.persistence.models.subscription_results import (
    SubscriptionAttemptResult,
    SubscriptionRepairDebit,
)
from forge.persistence.repositories.commands import PostgresCommandRepository
from subscription_launch_fixture import record_stopped_launch
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_resume_boundaries import prepared_case
from test_subscription_resume_controls import pause_for_resume
from test_subscription_scope_application import scope_case
from test_subscription_task_control_recovery import control
from test_subscription_usage import _known, _reservation


@pytest.mark.integration
async def test_quota_result_after_run_pause_resumes_without_repair_and_replays(
    session_factory, tmp_path
):
    factory, _, run_id, store = await prepared_case(session_factory, tmp_path)
    executor = SubscriptionDecisionExecutor(factory)
    admitted = await executor.admit_next("paused-primary", _reservation())
    assert admitted is not None
    proof = await record_stopped_launch(session_factory, admitted)
    _, resume = await pause_for_resume(factory, session_factory, run_id)
    assert (
        await executor.settle(
            admitted,
            SubscriptionInvocationResult(
                attempt=admitted.attempt,
                failure=SubscriptionFailure.QUOTA,
                telemetry=_known(),
                quota_exhaustion=QuotaExhaustion(datetime.now(UTC), "provider_usage_exhausted"),
                launch_proof=proof,
            ),
        )
    ).disposition == "stale"
    async with factory() as work:
        usage = await work.subscription_budget.usage(run_id, admitted.task.task_id)
        source = await work.session.get(SubscriptionAttemptResult, admitted.attempt.attempt_id)
        retained = source.result_payload, source.result_digest, source.accepted
    handler = ResumeRunHandler(artifact_store=store)
    for _ in range(2):
        async with factory() as work:
            await handler(resume, work)
    async with factory() as work:
        assert (
            await work.session.get(SubscriptionScheduledTask, admitted.task.task_id)
        ).repairs == 0
        assert await work.session.get(SubscriptionRepairDebit, admitted.attempt.attempt_id) is None
        assert await work.subscription_budget.usage(run_id, admitted.task.task_id) == usage
        source = await work.session.get(SubscriptionAttemptResult, admitted.attempt.attempt_id)
        assert (source.result_payload, source.result_digest, source.accepted) == retained
        assert (
            await work.quota.status(QuotaPolicy().key_for(admitted.task.route.effective))
        ).status == "blocked"
    assert (
        await SubscriptionDecisionExecutor(factory).admit_next("resumed-primary", _reservation())
        is None
    )


@pytest.mark.integration
@pytest.mark.parametrize("task_paused", [False, True])
async def test_cancelling_pending_task_while_run_paused_never_replays_its_decision(
    session_factory, tmp_path, task_paused
):
    factory, _, _, child = await scope_case(session_factory, tmp_path)
    if task_paused:
        await control(factory, child, "pause")
    commands = PostgresCommandRepository(session_factory)
    async with factory() as work:
        outstanding = await work.commands.list_outstanding_normal(
            run_id=child.task.run_id, exclude_command_id=None
        )
    for command in outstanding:
        assert command.command_type == "prepare_worktree"
        await commands.complete(command.id, worker_id=command.lease_owner)
    await pause_for_resume(factory, session_factory, child.task.run_id)
    assert (await control(factory, child, "cancel")).status == "cancelled"
    async with factory() as work:
        assert (await work.session.get(SubscriptionTask, child.task.task_id)).cancel_requested
        assert not (
            await work.session.get(SubscriptionAttemptResult, child.attempt.attempt_id)
        ).accepted
        assert await work.session.get(SubscriptionRepairDebit, child.attempt.attempt_id) is None
        assert await work.scheduler._active_count(run_id=child.task.run_id) == 0
