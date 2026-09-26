"""Primary waits release capacity and wake for the chosen child outcomes."""

from dataclasses import replace
from uuid import uuid4

import pytest
from forge.application.ports.subscription_gateway import (
    SubscriptionFailure,
    SubscriptionInvocationResult,
)
from forge.application.services.subscription_decisions import SubscriptionDecisionApplication
from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
from forge.domain.subscription import TaskBudget, WaitDecision
from forge.persistence.models.subscription import SubscriptionTask
from subscription_launch_fixture import record_stopped_launch
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_delegation_application import delegation_case
from test_subscription_usage import _known, _reservation


async def waiting_case(session_factory, tmp_path):
    def children(child, _):
        return tuple(
            replace(child, task_id=uuid4(), owned_paths=(f"apps/child-{i}",)) for i in range(4)
        )

    factory, parent, children, _ = await delegation_case(
        session_factory, tmp_path, children, primary_budget=TaskBudget(max_provider_attempts=20)
    )
    application = SubscriptionDecisionApplication(factory)
    await application.apply_delegation(parent.attempt.attempt_id)
    executor = SubscriptionDecisionExecutor(factory)
    child = await executor.admit_next("first-child", _reservation())
    await executor.settle(
        child,
        SubscriptionInvocationResult(
            attempt=child.attempt, failure=SubscriptionFailure.PROTOCOL, telemetry=_known()
        ),
    )
    return factory, executor, application, parent, children, child


@pytest.mark.integration
async def test_child_completion_wakes_primary_while_siblings_remain(session_factory, tmp_path):
    factory, _, _, parent, children, child = await waiting_case(session_factory, tmp_path)
    async with factory() as work:
        assert (await work.session.get(SubscriptionTask, parent.task.task_id)).state == "queued"
        for sibling in children:
            if sibling.task_id != child.task.task_id:
                assert (await work.session.get(SubscriptionTask, sibling.task_id)).state == "queued"


@pytest.mark.integration
async def test_wait_releases_slot_and_wakes_only_for_selected_children(session_factory, tmp_path):
    factory, executor, application, parent, children, completed = await waiting_case(
        session_factory, tmp_path
    )
    invocation = await executor.admit_next("primary-waits", _reservation())
    assert invocation.task.task_id == parent.task.task_id
    selected = next(item for item in children if item.task_id != completed.task.task_id)
    proof = await record_stopped_launch(session_factory, invocation)
    result = SubscriptionInvocationResult(
        attempt=invocation.attempt,
        decision=WaitDecision(
            run_id=parent.task.run_id,
            task_id=parent.task.task_id,
            waiting_on_task_ids=(selected.task_id,),
            reason="Need selected outcome",
        ),
        telemetry=_known(),
        launch_proof=proof,
    )
    await executor.settle(invocation, result)
    outcome = await application.apply_wait(invocation.attempt.attempt_id)
    assert outcome.accepted and outcome.disposition == "waiting"
    assert (await application.apply_wait(invocation.attempt.attempt_id)).replayed
    admitted = [await executor.admit_next(f"parallel-child-{i}", _reservation()) for i in range(3)]
    assert all(item is not None for item in admitted)
    chosen = next(item for item in admitted if item.task.task_id == selected.task_id)
    others = [item for item in admitted if item.task.task_id != selected.task_id]
    await executor.settle(
        others[0],
        SubscriptionInvocationResult(
            attempt=others[0].attempt, failure=SubscriptionFailure.PROTOCOL, telemetry=_known()
        ),
    )
    async with factory() as work:
        assert (await work.session.get(SubscriptionTask, parent.task.task_id)).state == "blocked"
    await executor.settle(
        chosen,
        SubscriptionInvocationResult(
            attempt=chosen.attempt, failure=SubscriptionFailure.PROTOCOL, telemetry=_known()
        ),
    )
    async with factory() as work:
        assert (await work.session.get(SubscriptionTask, parent.task.task_id)).state == "queued"
        assert (await work.session.get(SubscriptionTask, others[1].task.task_id)).state == "running"
    assert (await application.apply_wait(invocation.attempt.attempt_id)).replayed


async def settled_wait(session_factory, tmp_path, target="pending"):
    factory, executor, application, parent, children, completed = await waiting_case(
        session_factory, tmp_path
    )
    invocation = await executor.admit_next("primary-waits", _reservation())
    target_id = (
        completed.task.task_id
        if target == "completed"
        else parent.task.task_id
        if target == "parent"
        else uuid4()
        if target == "unknown"
        else next(child.task_id for child in children if child.task_id != completed.task.task_id)
    )
    proof = await record_stopped_launch(session_factory, invocation)
    result = SubscriptionInvocationResult(
        attempt=invocation.attempt,
        decision=WaitDecision(
            run_id=parent.task.run_id,
            task_id=parent.task.task_id,
            waiting_on_task_ids=(target_id,),
            reason="Wait for chosen child",
        ),
        telemetry=_known(),
        launch_proof=proof,
    )
    await executor.settle(invocation, result)
    return factory, executor, application, invocation, target_id


@pytest.mark.integration
async def test_wait_on_completed_child_immediately_requeues_and_replays(session_factory, tmp_path):
    factory, executor, application, invocation, _ = await settled_wait(
        session_factory, tmp_path, "completed"
    )
    assert (await application.apply_wait(invocation.attempt.attempt_id)).accepted
    async with factory() as work:
        assert (await work.session.get(SubscriptionTask, invocation.task.task_id)).state == "queued"
    resumed = await executor.admit_next("resumed-primary", _reservation())
    assert resumed.task.task_id == invocation.task.task_id
    assert (await application.apply_wait(invocation.attempt.attempt_id)).replayed


@pytest.mark.integration
@pytest.mark.parametrize("target", ["parent", "unknown"])
async def test_wait_rejects_nonchild_targets_and_queues_repair(session_factory, tmp_path, target):
    factory, _, application, invocation, _ = await settled_wait(session_factory, tmp_path, target)
    outcome = await application.apply_wait(invocation.attempt.attempt_id)
    assert not outcome.accepted and outcome.disposition == "decision_repair_queued"
    async with factory() as work:
        assert (await work.session.get(SubscriptionTask, invocation.task.task_id)).state == "queued"


@pytest.mark.integration
async def test_wait_cannot_apply_through_delegation_entrypoint(session_factory, tmp_path):
    from forge.application.ports.subscription_decisions import SubscriptionDecisionError

    _, _, application, invocation, _ = await settled_wait(session_factory, tmp_path)
    with pytest.raises(SubscriptionDecisionError):
        await application.apply_delegation(invocation.attempt.attempt_id)
    assert (await application.apply_wait(invocation.attempt.attempt_id)).accepted


@pytest.mark.integration
async def test_concurrent_wait_application_replays_once(session_factory, tmp_path):
    import asyncio

    _, _, application, invocation, _ = await settled_wait(session_factory, tmp_path)
    results = await asyncio.gather(
        *(application.apply_wait(invocation.attempt.attempt_id) for _ in range(2))
    )
    assert sorted(item.replayed for item in results) == [False, True]


@pytest.mark.integration
@pytest.mark.parametrize("mutation", ["payload", "record_type"])
async def test_corrupt_wait_record_does_not_undo_child_settlement(
    session_factory, tmp_path, mutation
):
    from forge.persistence.models.subscription import SubscriptionDecisionRecord
    from sqlalchemy import select

    factory, executor, application, invocation, _ = await settled_wait(session_factory, tmp_path)
    await application.apply_wait(invocation.attempt.attempt_id)
    async with factory() as work:
        record = await work.session.scalar(
            select(SubscriptionDecisionRecord).where(
                SubscriptionDecisionRecord.attempt_id == invocation.attempt.attempt_id
            )
        )
        if mutation == "payload":
            record.payload = {"invalid": True}
        else:
            record.record_type = "DelegateDecision"
        await work.commit()
    child = await executor.admit_next("child", _reservation())
    settled = await executor.settle(
        child,
        SubscriptionInvocationResult(
            attempt=child.attempt, failure=SubscriptionFailure.PROTOCOL, telemetry=_known()
        ),
    )
    assert settled.disposition == "failed"
    async with factory() as work:
        assert (await work.session.get(SubscriptionTask, child.task.task_id)).state == "terminal"
        assert (
            await work.session.get(SubscriptionTask, invocation.task.task_id)
        ).state == "blocked"


@pytest.mark.integration
async def test_inadmissible_wait_debits_one_repair_and_retries(session_factory, tmp_path):
    from forge.persistence.models.subscription_results import SubscriptionRepairDebit
    from sqlalchemy import func, select

    factory, executor, application, invocation, _ = await settled_wait(
        session_factory, tmp_path, "unknown"
    )
    outcome = await application.apply_wait(invocation.attempt.attempt_id)
    assert not outcome.accepted and outcome.disposition == "decision_repair_queued"
    assert (await application.apply_wait(invocation.attempt.attempt_id)).replayed
    async with factory() as work:
        assert (
            await work.session.scalar(select(func.count()).select_from(SubscriptionRepairDebit))
            == 1
        )
        assert (await work.session.get(SubscriptionTask, invocation.task.task_id)).state == "queued"
    retry = await executor.admit_next("primary-repairs", _reservation())
    assert retry.task.task_id == invocation.task.task_id
    assert retry.attempt.attempt_number == invocation.attempt.attempt_number + 1


@pytest.mark.integration
async def test_inadmissible_wait_without_repairs_terminates(session_factory, tmp_path):
    from forge.persistence.models.scheduling import SubscriptionScheduledTask
    from forge.persistence.models.subscription import SubscriptionAttempt
    from forge.persistence.models.subscription_results import SubscriptionRepairDebit
    from sqlalchemy import func, select

    factory, _, application, invocation, _ = await settled_wait(
        session_factory, tmp_path, "unknown"
    )
    async with factory() as work:
        row = await work.session.get(SubscriptionScheduledTask, invocation.task.task_id)
        row.max_repairs = 0
        await work.commit()
    outcome = await application.apply_wait(invocation.attempt.attempt_id)
    assert not outcome.accepted and outcome.disposition == "decision_rejected"
    assert (await application.apply_wait(invocation.attempt.attempt_id)).replayed
    async with factory() as work:
        assert (
            await work.session.scalar(select(func.count()).select_from(SubscriptionRepairDebit))
            == 0
        )
        assert (
            await work.session.get(SubscriptionAttempt, invocation.attempt.attempt_id)
        ).status == "terminal"
        row = await work.session.get(SubscriptionScheduledTask, invocation.task.task_id)
        assert row.state == "terminal" and row.lease_owner is None


@pytest.mark.integration
async def test_rejected_wait_replay_requires_failure_receipt(session_factory, tmp_path):
    from forge.application.ports.subscription_decisions import SubscriptionDecisionError
    from forge.persistence.models.subscription import SubscriptionDecisionRecord
    from sqlalchemy import delete

    factory, _, application, invocation, _ = await settled_wait(
        session_factory, tmp_path, "unknown"
    )
    await application.apply_wait(invocation.attempt.attempt_id)
    async with factory() as work:
        await work.session.execute(
            delete(SubscriptionDecisionRecord).where(
                SubscriptionDecisionRecord.idempotency_key
                == f"decision-rejection:{invocation.attempt.attempt_id}"
            )
        )
        await work.commit()
    with pytest.raises(SubscriptionDecisionError):
        await application.apply_wait(invocation.attempt.attempt_id)
