"""Load a stopped child's exact proposal without releasing its ownership."""

from dataclasses import replace
from uuid import uuid4

import pytest
from forge.application.ports.subscription_decisions import SubscriptionDecisionError
from forge.application.ports.subscription_gateway import SubscriptionInvocationResult
from forge.application.services.subscription_decisions import SubscriptionDecisionApplication
from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
from forge.domain.subscription import HandoffStatus, TaskBudget, TaskHandoff
from forge.persistence.models.scheduling import SubscriptionScheduledTask, SubscriptionSchedulerRun
from forge.persistence.models.subscription import (
    SubscriptionAttempt,
    SubscriptionClientLaunch,
    SubscriptionTask,
)
from forge.persistence.models.subscription_results import SubscriptionAttemptResult
from sqlalchemy import select
from subscription_launch_fixture import record_stopped_launch
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_delegation_application import delegation_case
from test_subscription_usage import _known, _reservation


async def handoff_case(
    session_factory, tmp_path, *, repairs=0, primary_budget=None, mutate_handoff=None
):
    factory, parent, _, _ = await delegation_case(
        session_factory,
        tmp_path,
        lambda child, _: (
            replace(
                child,
                max_repairs=repairs,
                budget=replace(
                    child.budget,
                    max_repairs=repairs,
                    max_provider_attempts=1 + repairs,
                ),
            ),
        ),
        primary_budget=primary_budget
        or (replace(TaskBudget(), max_provider_attempts=8) if repairs else None),
    )
    await SubscriptionDecisionApplication(factory).apply_delegation(parent.attempt.attempt_id)
    executor = SubscriptionDecisionExecutor(factory)
    child = await executor.admit_next("handoff-child", _reservation())
    handoff = TaskHandoff(
        run_id=child.task.run_id,
        task_id=child.task.task_id,
        attempt_id=child.attempt.attempt_id,
        status=HandoffStatus.COMPLETED,
        candidate_tree_digest="a" * 64,
        evidence_receipt_ids=(str(uuid4()),),
        summary="Ready for evidence verification",
    )
    if mutate_handoff is not None:
        handoff = mutate_handoff(handoff)
    proof = await record_stopped_launch(session_factory, child)
    result = SubscriptionInvocationResult(
        attempt=child.attempt, decision=handoff, telemetry=_known(), launch_proof=proof
    )
    assert (await executor.settle(child, result)).disposition == "decision_pending"
    return factory, parent, child, handoff


@pytest.mark.integration
async def test_handoff_proposal_retains_exact_source_and_ownership(session_factory, tmp_path):
    factory, parent, child, handoff = await handoff_case(session_factory, tmp_path)
    application = SubscriptionDecisionApplication(factory)
    proposal = await application.handoff_proposal(child.attempt.attempt_id)
    assert proposal.task == child.task and proposal.handoff == handoff
    assert proposal.candidate_epoch == child.candidate_epoch
    assert proposal.task_version == child.task_version + 1
    assert proposal.policy.version == child.envelope.safety_policy_version
    assert proposal.worktree.identity.run_id == child.task.run_id
    assert await application.handoff_proposal(child.attempt.attempt_id) == proposal
    async with factory() as work:
        task = await work.session.get(SubscriptionTask, child.task.task_id)
        scheduled = await work.session.get(SubscriptionScheduledTask, task.id)
        attempt = await work.session.get(SubscriptionAttempt, child.attempt.attempt_id)
        result = await work.session.get(SubscriptionAttemptResult, attempt.id)
        assert task.state == scheduled.state == attempt.status == "reconciling"
        assert scheduled.lease_owner == "handoff-child"
        assert result.disposition == "decision_pending" and not result.accepted
        assert result.result_digest == proposal.result_digest
        assert (await work.session.get(SubscriptionTask, parent.task.task_id)).state == "blocked"


@pytest.mark.integration
@pytest.mark.parametrize(
    "mutation",
    [
        "pause",
        "task_cancel",
        "candidate",
        "task_version",
        "worktree",
        "launch",
        "source_digest",
        "parent",
        "scheduled_parent",
        "lease_owner",
        "lease_generation",
        "scheduled_scope",
        "scheduled_read_only",
    ],
)
async def test_handoff_proposal_rejects_changed_authority(session_factory, tmp_path, mutation):
    factory, _, child, _ = await handoff_case(session_factory, tmp_path)
    async with factory() as work:
        task = await work.session.get(SubscriptionTask, child.task.task_id)
        if mutation == "pause":
            run = await work.runs.get(task.run_id)
            await work.runs.pause(run.id, run.version, "run.paused", {}, actor_class="operator")
        elif mutation == "task_cancel":
            task.cancel_requested = True
        elif mutation == "candidate":
            scheduler = await work.session.get(SubscriptionSchedulerRun, task.run_id)
            scheduler.candidate_epoch += 1
        elif mutation == "task_version":
            task.version += 1
        elif mutation == "worktree":
            scheduled = await work.session.get(SubscriptionScheduledTask, task.id)
            scheduled.worktree_id = "foreign-tree"
        elif mutation == "launch":
            launch = await work.session.scalar(
                select(SubscriptionClientLaunch).where(
                    SubscriptionClientLaunch.attempt_id == child.attempt.attempt_id
                )
            )
            launch.state = "uncertain"
        elif mutation == "source_digest":
            result = await work.session.get(SubscriptionAttemptResult, child.attempt.attempt_id)
            result.result_digest = "f" * 64
        elif mutation == "parent":
            task.parent_task_id = None
        else:
            scheduled = await work.session.get(SubscriptionScheduledTask, task.id)
            if mutation == "scheduled_parent":
                scheduled.parent_task_id = None
            elif mutation == "lease_owner":
                scheduled.lease_owner = "different-owner"
            elif mutation == "scheduled_scope":
                scheduled.owned_paths = []
            elif mutation == "scheduled_read_only":
                scheduled.read_only = True
            else:
                scheduled.lease_generation += 1
        await work.commit()
    with pytest.raises(SubscriptionDecisionError):
        await SubscriptionDecisionApplication(factory).handoff_proposal(child.attempt.attempt_id)


@pytest.mark.integration
async def test_handoff_proposal_rejects_primary_decision(session_factory, tmp_path):
    factory, parent, _, _ = await delegation_case(session_factory, tmp_path)
    with pytest.raises(SubscriptionDecisionError):
        await SubscriptionDecisionApplication(factory).handoff_proposal(parent.attempt.attempt_id)


@pytest.mark.integration
async def test_handoff_proposal_allows_expired_original_lease(session_factory, tmp_path):
    from datetime import UTC, datetime, timedelta

    factory, _, child, handoff = await handoff_case(session_factory, tmp_path)
    async with factory() as work:
        scheduled = await work.session.get(SubscriptionScheduledTask, child.task.task_id)
        scheduled.lease_expires_at = datetime.now(UTC) - timedelta(minutes=1)
        await work.commit()
    proposal = await SubscriptionDecisionApplication(factory).handoff_proposal(
        child.attempt.attempt_id
    )
    assert proposal.handoff == handoff
