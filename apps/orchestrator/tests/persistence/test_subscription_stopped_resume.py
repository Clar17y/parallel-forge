"""A stopped subscription invocation can resume without losing identity or usage."""

from types import SimpleNamespace

import pytest
from forge.application.handlers.run_controls import ResumeRunHandler
from forge.application.ports.subscription_gateway import (
    SubscriptionFailure,
    SubscriptionInvocationResult,
)
from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
from forge.domain.run import RunState
from forge.persistence.models.scheduling import SubscriptionScheduledTask
from forge.persistence.models.subscription_results import SubscriptionAttemptResult
from subscription_launch_fixture import record_stopped_launch
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_resume_boundaries import prepared_case
from test_subscription_resume_controls import pause_for_resume
from test_subscription_usage import _known, _reservation


async def stopped_case(session_factory, tmp_path, **options):
    factory, _, run_id, store = await prepared_case(session_factory, tmp_path, **options)
    executor = SubscriptionDecisionExecutor(factory)
    admission = await executor.admit_next("paused-primary", _reservation())
    assert admission is not None
    stopped = await record_stopped_launch(session_factory, admission)
    _, resume = await pause_for_resume(factory, session_factory, run_id)
    result = SubscriptionInvocationResult(
        attempt=admission.attempt,
        launch_proof=stopped,
        failure=SubscriptionFailure.INTERRUPTED,
        telemetry=_known(),
    )
    assert (await executor.settle(admission, result)).disposition == "stale"
    return SimpleNamespace(
        factory=factory,
        run_id=run_id,
        store=store,
        executor=executor,
        admission=admission,
        resume=resume,
        result=result,
    )


@pytest.mark.integration
async def test_confirmed_stopped_primary_resumes_with_preserved_result_and_budget(
    session_factory, tmp_path
):
    case = await stopped_case(session_factory, tmp_path)
    factory, run_id, store = case.factory, case.run_id, case.store
    executor, admission, resume, result = case.executor, case.admission, case.resume, case.result
    async with factory() as work:
        source = await work.session.get(SubscriptionAttemptResult, admission.attempt.attempt_id)
        retained = (
            source.result_digest,
            source.result_payload,
            source.disposition,
            source.accepted,
        )
        usage = await work.subscription_budget.usage(run_id, admission.task.task_id)
    handler = ResumeRunHandler(artifact_store=store)
    for _ in range(2):
        async with factory() as work:
            await handler(resume, work)
    async with factory() as work:
        source = await work.session.get(SubscriptionAttemptResult, admission.attempt.attempt_id)
        assert (
            source.result_digest,
            source.result_payload,
            source.disposition,
            source.accepted,
        ) == retained
        assert (await work.runs.get(run_id)).state is RunState.IMPLEMENTING
        scheduled = await work.session.get(SubscriptionScheduledTask, admission.task.task_id)
        assert scheduled.state == "queued" and scheduled.repairs == 1
        current = await work.subscription_budget.usage(run_id, admission.task.task_id)
        assert current.consumed.provider_attempts == usage.consumed.provider_attempts
        assert current.consumed.repairs == usage.consumed.repairs + 1
    assert (await executor.settle(admission, result)).replayed
    resumed = await executor.admit_next("continued-primary", _reservation())
    assert resumed is not None and resumed.task == admission.task
    assert resumed.attempt.attempt_number == admission.attempt.attempt_number + 1
    assert resumed.attempt.attempt_id != admission.attempt.attempt_id
    async with factory() as work:
        await handler(resume, work)
