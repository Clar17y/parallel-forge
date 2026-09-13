"""A selected review can return control to a primary that requests repairs."""

from uuid import uuid4

import pytest
from forge.application.ports.subscription_gateway import SubscriptionInvocationResult
from forge.application.services.subscription_decisions import SubscriptionDecisionApplication
from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
from forge.application.services.subscription_handoff_application import (
    SubscriptionHandoffApplication,
)
from forge.domain.subscription import (
    DelegateDecision,
    HandoffStatus,
    LogicalTaskContract,
    SpecialistPurpose,
    TaskBudget,
    TaskHandoff,
)
from subscription_launch_fixture import record_stopped_launch
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_candidate_reads import reader_case
from test_subscription_usage import _known, _reservation


@pytest.mark.integration
async def test_primary_can_delegate_after_selected_review_rejection(session_factory, tmp_path):
    factory, reviewer = await reader_case(
        session_factory,
        tmp_path,
        primary_budget=TaskBudget(max_provider_attempts=8),
    )
    executor = SubscriptionDecisionExecutor(factory)
    handoff = TaskHandoff(
        run_id=reviewer.task.run_id,
        task_id=reviewer.task.task_id,
        attempt_id=reviewer.attempt.attempt_id,
        status=HandoffStatus.COMPLETED,
        candidate_tree_digest="f" * 64,
        evidence_receipt_ids=(str(uuid4()),),
        summary="Invalid candidate claim returns control to primary",
    )
    proof = await record_stopped_launch(session_factory, reviewer)
    await executor.settle(
        reviewer,
        SubscriptionInvocationResult(
            attempt=reviewer.attempt,
            decision=handoff,
            telemetry=_known(),
            launch_proof=proof,
        ),
    )

    async def no_snapshot(proposal):
        raise AssertionError("false claim should be rejected before IO")

    rejected = await SubscriptionHandoffApplication(factory, None, no_snapshot).apply(
        reviewer.attempt.attempt_id
    )
    assert rejected.disposition == "handoff_rejected"
    primary = await executor.admit_next("primary-repairs", _reservation())
    assert primary is not None and primary.task.purpose is SpecialistPurpose.PRIMARY
    child = LogicalTaskContract(
        run_id=primary.task.run_id,
        task_id=uuid4(),
        parent_task_id=primary.task.task_id,
        purpose=SpecialistPurpose.ROUTINE_IMPLEMENTATION,
        route=primary.envelope.route_for(SpecialistPurpose.ROUTINE_IMPLEMENTATION),
        budget=_reservation(),
        max_repairs=0,
        owned_paths=("apps/feature",),
    )
    decision = DelegateDecision(
        run_id=primary.task.run_id,
        parent_task_id=primary.task.task_id,
        child_tasks=(child,),
        rationale="Repair before requesting review again",
    )
    proof = await record_stopped_launch(session_factory, primary)
    await executor.settle(
        primary,
        SubscriptionInvocationResult(
            attempt=primary.attempt,
            decision=decision,
            telemetry=_known(),
            launch_proof=proof,
        ),
    )
    result = await SubscriptionDecisionApplication(factory).apply_delegation(
        primary.attempt.attempt_id
    )
    assert result.accepted
    writer = await executor.admit_next("repair-worker", _reservation())
    assert writer is not None and writer.task.task_id == child.task_id
    assert writer.candidate_epoch == primary.candidate_epoch + 1
