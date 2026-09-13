"""Primary repair delegation invalidates the closed candidate atomically."""

from uuid import uuid4

import pytest
from forge.application.ports.subscription_gateway import SubscriptionInvocationResult
from forge.application.services.subscription_decisions import SubscriptionDecisionApplication
from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
from forge.domain.subscription import (
    DelegateDecision,
    LogicalTaskContract,
    SpecialistPurpose,
    TaskBudget,
)
from forge.persistence.models.scheduling import SubscriptionSchedulerRun
from subscription_launch_fixture import record_stopped_launch
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_candidate_reads import reader_case
from test_subscription_usage import _known, _reservation


async def redelegation_case(session_factory, tmp_path, *, path="apps/feature"):
    factory, primary = await reader_case(
        session_factory,
        tmp_path,
        review_required=False,
        primary_budget=TaskBudget(max_provider_attempts=8),
    )
    child = LogicalTaskContract(
        run_id=primary.task.run_id,
        task_id=uuid4(),
        parent_task_id=primary.task.task_id,
        purpose=SpecialistPurpose.ROUTINE_IMPLEMENTATION,
        route=primary.envelope.route_for(SpecialistPurpose.ROUTINE_IMPLEMENTATION),
        budget=_reservation(),
        max_repairs=0,
        owned_paths=(path,),
    )
    decision = DelegateDecision(
        run_id=primary.task.run_id,
        parent_task_id=primary.task.task_id,
        child_tasks=(child,),
        rationale="Repair candidate before acceptance",
    )
    launch = await record_stopped_launch(session_factory, primary)
    result = SubscriptionInvocationResult(
        attempt=primary.attempt, decision=decision, telemetry=_known(), launch_proof=launch
    )
    assert (
        await SubscriptionDecisionExecutor(factory).settle(primary, result)
    ).disposition == "decision_pending"
    return factory, primary, child


@pytest.mark.integration
async def test_redelegation_reopens_candidate_once_and_admits_writer(session_factory, tmp_path):
    factory, primary, child = await redelegation_case(session_factory, tmp_path)
    service = SubscriptionDecisionApplication(factory)
    result = await service.apply_delegation(primary.attempt.attempt_id)
    assert result.accepted and result.disposition == "delegated"
    async with factory() as work:
        scheduler = await work.session.get(SubscriptionSchedulerRun, primary.task.run_id)
        assert scheduler.candidate_state == "open"
        assert scheduler.candidate_epoch == primary.candidate_epoch + 1
        assert (
            await work.subscription_decisions.review_selection_context(
                primary.task.run_id, primary.task.task_id
            )
            is None
        )
    writer = await SubscriptionDecisionExecutor(factory).admit_next("repair-writer", _reservation())
    assert writer is not None and writer.task.task_id == child.task_id
    assert writer.candidate_epoch == primary.candidate_epoch + 1
    assert (await service.apply_delegation(primary.attempt.attempt_id)).replayed
    async with factory() as work:
        assert (
            await work.session.get(SubscriptionSchedulerRun, primary.task.run_id)
        ).candidate_epoch == writer.candidate_epoch


@pytest.mark.integration
async def test_invalid_redelegation_keeps_candidate_closed(session_factory, tmp_path):
    factory, primary, child = await redelegation_case(session_factory, tmp_path, path="outside")
    result = await SubscriptionDecisionApplication(factory).apply_delegation(
        primary.attempt.attempt_id
    )
    assert not result.accepted
    async with factory() as work:
        from forge.persistence.models.subscription import SubscriptionTask

        scheduler = await work.session.get(SubscriptionSchedulerRun, primary.task.run_id)
        assert (
            scheduler.candidate_state == "closed"
            and scheduler.candidate_epoch == primary.candidate_epoch
        )
        assert await work.session.get(SubscriptionTask, child.task_id) is None


@pytest.mark.integration
async def test_failed_child_enqueue_rolls_back_candidate_reopening(
    session_factory, tmp_path, monkeypatch
):
    from forge.persistence.models.subscription import SubscriptionTask
    from forge.persistence.repositories.scheduling import PostgresSchedulingRepository

    factory, primary, child = await redelegation_case(session_factory, tmp_path)

    async def fail(*args, **kwargs):
        raise RuntimeError("injected enqueue failure")

    monkeypatch.setattr(PostgresSchedulingRepository, "enqueue", fail)
    with pytest.raises(RuntimeError, match="enqueue failure"):
        await SubscriptionDecisionApplication(factory).apply_delegation(primary.attempt.attempt_id)
    async with factory() as work:
        scheduler = await work.session.get(SubscriptionSchedulerRun, primary.task.run_id)
        assert (
            scheduler.candidate_state == "closed"
            and scheduler.candidate_epoch == primary.candidate_epoch
        )
        assert await work.session.get(SubscriptionTask, child.task_id) is None


@pytest.mark.integration
@pytest.mark.parametrize("change", ["delete", "digest", "source"])
async def test_redelegation_replay_reproves_reopening_receipt(session_factory, tmp_path, change):
    from forge.application.ports.subscription_decisions import SubscriptionDecisionError
    from forge.persistence.models.subscription_results import SubscriptionAttemptResult

    factory, primary, _ = await redelegation_case(session_factory, tmp_path)
    service = SubscriptionDecisionApplication(factory)
    await service.apply_delegation(primary.attempt.attempt_id)
    async with factory() as work:
        result = await work.session.get(SubscriptionAttemptResult, primary.attempt.attempt_id)
        if change == "delete":
            from sqlalchemy import null

            result.application_payload = null()
            result.application_digest = None
        elif change == "digest":
            result.application_digest = "f" * 64
        else:
            from uuid import UUID

            source = await work.session.get(
                SubscriptionAttemptResult, UUID(result.application_payload["selection_attempt_id"])
            )
            source.application_digest = "f" * 64
        await work.commit()
    with pytest.raises(SubscriptionDecisionError):
        await service.apply_delegation(primary.attempt.attempt_id)
