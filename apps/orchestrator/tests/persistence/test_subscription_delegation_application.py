"""Apply settled primary delegation with durable authority and atomic queueing."""

from uuid import uuid4

import pytest
from forge.application.ports.subscription_gateway import SubscriptionInvocationResult
from forge.application.services.subscription_decisions import SubscriptionDecisionApplication
from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
from forge.domain.subscription import (
    AcceptanceCriterion,
    DelegateDecision,
    LogicalTaskContract,
    SpecialistPurpose,
)
from forge.persistence.models.scheduling import SubscriptionScheduledTask
from forge.persistence.models.subscription import SubscriptionTask
from subscription_launch_fixture import record_stopped_launch
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_preparation import preparation_case
from test_subscription_usage import _known, _reservation


async def delegation_case(
    session_factory, tmp_path, mutate=None, *, primary_budget=None, plan_scope=None
):
    factory, _, command, prepare, _ = await preparation_case(
        session_factory, tmp_path, primary_budget=primary_budget, plan_scope=plan_scope
    )
    async with factory() as work:
        await prepare.execute(command, work)
    executor = SubscriptionDecisionExecutor(factory)
    admission = await executor.admit_next("primary-delegates", _reservation())
    child = LogicalTaskContract(
        run_id=admission.task.run_id,
        task_id=uuid4(),
        parent_task_id=admission.task.task_id,
        purpose=SpecialistPurpose.ROUTINE_IMPLEMENTATION,
        route=admission.envelope.route_for(SpecialistPurpose.ROUTINE_IMPLEMENTATION),
        budget=_reservation(),
        max_repairs=0,
        owned_paths=("apps/feature",),
        typed_acceptance=(
            AcceptanceCriterion(criterion_id="behavior", description="Implement behavior"),
        ),
    )
    children = (child,) if mutate is None else mutate(child, admission)
    decision = DelegateDecision(
        run_id=admission.task.run_id,
        parent_task_id=admission.task.task_id,
        child_tasks=children,
        rationale="Bounded implementation",
    )
    proof = await record_stopped_launch(session_factory, admission)
    result = SubscriptionInvocationResult(
        attempt=admission.attempt, decision=decision, telemetry=_known(), launch_proof=proof
    )
    assert (await executor.settle(admission, result)).disposition == "decision_pending"
    return factory, admission, children, result


@pytest.mark.integration
async def test_delegation_releases_primary_and_admits_child_once(session_factory, tmp_path):
    factory, admission, children, result = await delegation_case(session_factory, tmp_path)
    application = SubscriptionDecisionApplication(factory)
    outcome = await application.apply_delegation(admission.attempt.attempt_id)
    assert outcome.accepted and outcome.disposition == "delegated" and not outcome.replayed
    assert (await application.apply_delegation(admission.attempt.attempt_id)).replayed
    async with factory() as work:
        task = await work.session.get(SubscriptionTask, admission.task.task_id)
        scheduled = await work.session.get(SubscriptionScheduledTask, task.id)
        assert task.state == scheduled.state == "blocked"
        assert scheduled.lease_owner is None and scheduled.lease_expires_at is None
        assert task.version == admission.task_version + 2
    executor = SubscriptionDecisionExecutor(factory)
    child_admission = await executor.admit_next("child-worker", _reservation())
    assert child_admission is not None and child_admission.task.task_id == children[0].task_id
    assert (await application.apply_delegation(admission.attempt.attempt_id)).replayed
    assert (await executor.settle(admission, result)).replayed


@pytest.mark.integration
async def test_completed_child_failure_wakes_logical_primary(session_factory, tmp_path):
    from forge.application.ports.subscription_gateway import SubscriptionFailure

    factory, admission, _, _ = await delegation_case(session_factory, tmp_path)
    await SubscriptionDecisionApplication(factory).apply_delegation(admission.attempt.attempt_id)
    executor = SubscriptionDecisionExecutor(factory)
    child = await executor.admit_next("child", _reservation())
    await executor.settle(
        child,
        SubscriptionInvocationResult(
            attempt=child.attempt, failure=SubscriptionFailure.PROTOCOL, telemetry=_known()
        ),
    )
    async with factory() as work:
        parent = await work.session.get(SubscriptionTask, admission.task.task_id)
        scheduled = await work.session.get(SubscriptionScheduledTask, parent.id)
        assert parent.state == scheduled.state == "queued"


@pytest.mark.integration
async def test_child_cannot_wait_on_yielding_parent(session_factory, tmp_path):
    from dataclasses import replace

    factory, admission, _, _ = await delegation_case(
        session_factory,
        tmp_path,
        lambda child, parent: (replace(child, dependency_task_ids=(parent.task.task_id,)),),
    )
    outcome = await SubscriptionDecisionApplication(factory).apply_delegation(
        admission.attempt.attempt_id
    )
    assert not outcome.accepted and outcome.disposition == "decision_repair_queued"


@pytest.mark.integration
@pytest.mark.parametrize(
    "mutation",
    [
        "scope",
        "budget",
        "checks",
        "acceptance_check",
        "route",
        "duplicate",
        "foreign_dependency",
        "cycle",
    ],
)
async def test_bad_child_batch_is_rejected_without_partial_tasks(
    session_factory, tmp_path, mutation
):
    from dataclasses import replace

    from forge.persistence.models.subscription_results import SubscriptionAttemptResult

    def change(child, admission):
        if mutation == "scope":
            return (replace(child, owned_paths=("outside",)),)
        if mutation == "budget":
            return (
                replace(
                    child,
                    budget=replace(
                        child.budget,
                        max_duration_seconds=admission.task.budget.max_duration_seconds + 1,
                    ),
                ),
            )
        if mutation == "checks":
            return (replace(child, named_checks=("unapproved",)),)
        if mutation == "acceptance_check":
            return (
                replace(
                    child,
                    typed_acceptance=(
                        AcceptanceCriterion(
                            criterion_id="check",
                            description="pass",
                            required_check_names=("unapproved",),
                        ),
                    ),
                ),
            )
        if mutation == "route":
            route = replace(child.route.effective, model="unapproved-model")
            return (replace(child, route=replace(child.route, requested=route, effective=route)),)
        if mutation == "duplicate":
            return (child, child)
        if mutation == "foreign_dependency":
            return (replace(child, dependency_task_ids=(uuid4(),)),)
        second = replace(
            child,
            task_id=uuid4(),
            owned_paths=("apps/second",),
            dependency_task_ids=(child.task_id,),
        )
        return (replace(child, dependency_task_ids=(second.task_id,)), second)

    factory, admission, children, _ = await delegation_case(session_factory, tmp_path, change)
    outcome = await SubscriptionDecisionApplication(factory).apply_delegation(
        admission.attempt.attempt_id
    )
    assert not outcome.accepted
    async with factory() as work:
        for child in children:
            assert await work.session.get(SubscriptionTask, child.task_id) is None
        result = await work.session.get(SubscriptionAttemptResult, admission.attempt.attempt_id)
        assert not result.accepted and result.disposition == "decision_repair_queued"
        assert (await work.session.get(SubscriptionTask, admission.task.task_id)).state == "queued"


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
        "policy",
    ],
)
async def test_delegation_revalidates_source_after_settlement(session_factory, tmp_path, mutation):
    from forge.application.ports.subscription_decisions import SubscriptionDecisionError
    from forge.persistence.models import Run
    from forge.persistence.models.scheduling import SubscriptionSchedulerRun
    from forge.persistence.models.subscription import SubscriptionClientLaunch
    from forge.persistence.models.subscription_results import SubscriptionAttemptResult
    from sqlalchemy import select

    factory, admission, children, _ = await delegation_case(session_factory, tmp_path)
    async with factory() as work:
        if mutation == "pause":
            run = await work.runs.get(admission.task.run_id)
            await work.runs.pause(run.id, run.version, "run.paused", {}, actor_class="operator")
        elif mutation == "task_cancel":
            task = await work.session.get(SubscriptionTask, admission.task.task_id)
            task.cancel_requested = True
        elif mutation == "candidate":
            scheduler = await work.session.get(SubscriptionSchedulerRun, admission.task.run_id)
            scheduler.candidate_epoch += 1
        elif mutation == "task_version":
            task = await work.session.get(SubscriptionTask, admission.task.task_id)
            task.version += 1
        elif mutation == "worktree":
            task = await work.session.get(SubscriptionScheduledTask, admission.task.task_id)
            task.worktree_id = "foreign-tree"
        elif mutation == "launch":
            launch = await work.session.scalar(
                select(SubscriptionClientLaunch).where(
                    SubscriptionClientLaunch.attempt_id == admission.attempt.attempt_id
                )
            )
            launch.state = "uncertain"
        elif mutation == "source_digest":
            result = await work.session.get(SubscriptionAttemptResult, admission.attempt.attempt_id)
            result.result_digest = "f" * 64
        else:
            from forge.domain.operation import canonical_digest

            run = await work.session.get(Run, admission.task.run_id)
            policy = await work.projects.get_policy(run.project_id, run.policy_version)
            document = {**policy.document, "version": run.policy_version + 1}
            await work.projects.append_policy(
                project_id=run.project_id,
                expected_policy_version=run.policy_version,
                policy_digest=canonical_digest(document),
                policy_document=document,
            )
            run.policy_version += 1
        await work.commit()
    with pytest.raises(SubscriptionDecisionError):
        await SubscriptionDecisionApplication(factory).apply_delegation(
            admission.attempt.attempt_id
        )
    async with factory() as work:
        assert await work.session.get(SubscriptionTask, children[0].task_id) is None


@pytest.mark.integration
async def test_concurrent_delegation_replays_exactly_once(session_factory, tmp_path):
    import asyncio

    factory, admission, _, _ = await delegation_case(session_factory, tmp_path)
    application = SubscriptionDecisionApplication(factory)
    results = await asyncio.gather(
        *(application.apply_delegation(admission.attempt.attempt_id) for _ in range(2))
    )
    assert sorted(result.replayed for result in results) == [False, True]


@pytest.mark.integration
async def test_delegation_batch_failure_rolls_back_earlier_child(
    session_factory, tmp_path, monkeypatch
):
    from dataclasses import replace

    from forge.persistence.repositories.scheduling import PostgresSchedulingRepository

    def children(child, _):
        return (child, replace(child, task_id=uuid4(), owned_paths=("apps/second",)))

    factory, admission, children, _ = await delegation_case(session_factory, tmp_path, children)
    original = PostgresSchedulingRepository.enqueue
    calls = 0

    async def fail_second(self, task):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected enqueue failure")
        return await original(self, task)

    with monkeypatch.context() as patch:
        patch.setattr(PostgresSchedulingRepository, "enqueue", fail_second)
        with pytest.raises(RuntimeError, match="injected"):
            await SubscriptionDecisionApplication(factory).apply_delegation(
                admission.attempt.attempt_id
            )
    async with factory() as work:
        for child in children:
            assert await work.session.get(SubscriptionTask, child.task_id) is None
    assert (
        await SubscriptionDecisionApplication(factory).apply_delegation(
            admission.attempt.attempt_id
        )
    ).accepted


@pytest.mark.integration
async def test_forward_dependency_is_created_before_dependent(session_factory, tmp_path):
    from dataclasses import replace

    def children(child, _):
        dependent = replace(
            child,
            task_id=uuid4(),
            owned_paths=("apps/dependent",),
            dependency_task_ids=(child.task_id,),
        )
        return (dependent, child)

    factory, admission, tasks, _ = await delegation_case(session_factory, tmp_path, children)
    await SubscriptionDecisionApplication(factory).apply_delegation(admission.attempt.attempt_id)
    claimed = await SubscriptionDecisionExecutor(factory).admit_next("ready-child", _reservation())
    assert claimed.task.task_id == tasks[1].task_id


@pytest.mark.integration
async def test_unresolved_effect_blocks_delegation_application(session_factory, tmp_path):
    from forge.application.ports.subscription_decisions import SubscriptionDecisionError
    from forge.persistence.models.scheduling import SubscriptionScheduledEffect

    factory, admission, _, _ = await delegation_case(session_factory, tmp_path)
    async with factory() as work:
        work.session.add(
            SubscriptionScheduledEffect(
                id=uuid4(),
                run_id=admission.task.run_id,
                task_id=admission.task.task_id,
                lease_owner=admission.lease.owner,
                lease_generation=admission.lease.generation,
                candidate_epoch=admission.candidate_epoch,
                whole_worktree_exclusive=False,
            )
        )
        await work.commit()
    with pytest.raises(SubscriptionDecisionError):
        await SubscriptionDecisionApplication(factory).apply_delegation(
            admission.attempt.attempt_id
        )


@pytest.mark.integration
async def test_applied_replay_after_pause_never_recreates_missing_record(session_factory, tmp_path):
    from forge.application.ports.subscription_decisions import SubscriptionDecisionError
    from forge.persistence.models.subscription import SubscriptionDecisionRecord
    from sqlalchemy import delete

    factory, admission, _, _ = await delegation_case(session_factory, tmp_path)
    application = SubscriptionDecisionApplication(factory)
    await application.apply_delegation(admission.attempt.attempt_id)
    async with factory() as work:
        run = await work.runs.get(admission.task.run_id)
        await work.runs.pause(run.id, run.version, "run.paused", {}, actor_class="operator")
        await work.commit()
    assert (await application.apply_delegation(admission.attempt.attempt_id)).replayed
    async with factory() as work:
        await work.session.execute(
            delete(SubscriptionDecisionRecord).where(
                SubscriptionDecisionRecord.attempt_id == admission.attempt.attempt_id
            )
        )
        await work.commit()
    with pytest.raises(SubscriptionDecisionError):
        await application.apply_delegation(admission.attempt.attempt_id)
