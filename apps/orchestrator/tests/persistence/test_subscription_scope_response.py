"""Only an exact stopped request permits primary-controlled child scope changes."""

import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest
from forge.agents.subscription_protocol import json_value
from forge.application.ports.subscription_decisions import SubscriptionDecisionError
from forge.application.ports.subscription_gateway import SubscriptionInvocationResult
from forge.application.services.subscription_decision_recovery import SubscriptionDecisionRecovery
from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
from forge.application.services.subscription_requests import SubscriptionRequestBuilder
from forge.artifacts.filesystem import FilesystemArtifactStore
from forge.domain.subscription import (
    BoundScopeResponseDecision,
    ScopeRequestDecision,
    TaskBudget,
    decode_subscription_record,
)
from forge.persistence.models.scheduling import (
    SubscriptionScheduledEffect,
    SubscriptionScheduledTask,
)
from forge.persistence.models.subscription import (
    SubscriptionClientLaunch,
    SubscriptionTask,
)
from forge.persistence.models.subscription_results import (
    SubscriptionAttemptResult,
    SubscriptionRepairDebit,
)
from sqlalchemy import select
from subscription_launch_fixture import record_stopped_launch
from test_scheduler_acceptance import (
    _claim,
    _enqueue,
    _remove_disposable_subscription_rows,  # noqa: F401
)
from test_subscription_scope_application import scope_case
from test_subscription_usage import _known, _reservation


async def response_case(
    session_factory, tmp_path, *, grant=True, mutate=None, requested_paths=("apps/shared",)
):
    factory, application, _, child = await scope_case(
        session_factory,
        tmp_path,
        child_budget=TaskBudget(max_provider_attempts=3),
        requested_paths=requested_paths,
    )
    await application.apply_scope_request(child.attempt.attempt_id)
    executor = SubscriptionDecisionExecutor(factory)
    primary = await executor.admit_next("responding-primary", _reservation())
    assert primary is not None
    decision = BoundScopeResponseDecision(
        run_id=child.task.run_id,
        task_id=child.task.task_id,
        request_attempt_id=child.attempt.attempt_id,
        granted_paths=requested_paths if grant else (),
        denied_paths=() if grant else requested_paths,
        reason="Scope decision",
    )
    if mutate is not None:
        decision = mutate(decision)
    proof = await record_stopped_launch(session_factory, primary)
    assert (
        await executor.settle(
            primary,
            SubscriptionInvocationResult(
                attempt=primary.attempt, decision=decision, telemetry=_known(), launch_proof=proof
            ),
        )
    ).disposition == "decision_pending"
    return factory, application, primary, child


@pytest.mark.integration
@pytest.mark.parametrize("grant", [False, True])
async def test_scope_response_resumes_child_preserving_budget_and_result(
    session_factory, tmp_path, grant
):
    factory, application, primary, child = await response_case(
        session_factory, tmp_path, grant=grant
    )
    async with factory() as work:
        source = await work.session.get(SubscriptionAttemptResult, primary.attempt.attempt_id)
        original = source.result_payload, source.result_digest
    outcome = await application.apply_scope_response(primary.attempt.attempt_id)
    assert outcome.accepted and outcome.disposition == "scope_responded" and not outcome.replayed
    assert (await application.apply_scope_response(primary.attempt.attempt_id)).replayed

    async with factory() as work:
        row = await work.session.get(SubscriptionTask, child.task.task_id)
        contract = decode_subscription_record(row.payload)
        assert row.state == "queued" and contract.budget == child.task.budget
        assert contract.route == child.task.route
        assert contract.owned_paths == child.task.owned_paths + (("apps/shared",) if grant else ())
        source = await work.session.get(SubscriptionAttemptResult, primary.attempt.attempt_id)
        assert (source.result_payload, source.result_digest) == original
    executor = SubscriptionDecisionExecutor(factory)
    admitted = [
        await executor.admit_next("next-primary", _reservation()),
        await executor.admit_next("next-child", _reservation()),
    ]
    resumed = next(
        value for value in admitted if value and value.task.task_id == child.task.task_id
    )
    assert resumed.attempt.attempt_number == 2 and resumed.task.budget == child.task.budget
    request = await SubscriptionRequestBuilder(factory).build(resumed)
    own = next(
        value
        for value in request.untrusted_context["task_outcomes"]
        if value["task_id"] == str(child.task.task_id)
    )
    answer = decode_subscription_record(json_value(own["scope_response"]))
    assert (
        answer.request_attempt_id == child.attempt.attempt_id and answer.reason == "Scope decision"
    )
    assert own["scope_response_attempt_id"] == str(primary.attempt.attempt_id)
    assert own["scope_request"] is None
    assert (await application.apply_scope_response(primary.attempt.attempt_id)).replayed


@pytest.mark.integration
@pytest.mark.parametrize("change", ["request", "target", "partial", "outside"])
async def test_invalid_scope_response_consumes_bounded_primary_repair(
    session_factory, tmp_path, change
):
    mutation = {
        "request": lambda value: replace(value, request_attempt_id=uuid4()),
        "target": lambda value: replace(value, task_id=uuid4()),
        "partial": lambda value: replace(value, granted_paths=("apps/shared",)),
        "outside": None,
    }[change]
    paths = (
        ("outside",)
        if change == "outside"
        else ("apps/shared", "apps/second")
        if change == "partial"
        else ("apps/shared",)
    )
    factory, application, primary, child = await response_case(
        session_factory, tmp_path, mutate=mutation, requested_paths=paths
    )
    outcome = await application.apply_scope_response(primary.attempt.attempt_id)
    assert not outcome.accepted and outcome.disposition == "decision_repair_queued"
    assert (await application.apply_scope_response(primary.attempt.attempt_id)).replayed
    async with factory() as work:
        assert (
            await work.session.get(SubscriptionRepairDebit, primary.attempt.attempt_id) is not None
        )
        row = await work.session.get(SubscriptionTask, child.task.task_id)
        assert row.state == "blocked" and decode_subscription_record(row.payload) == child.task


@pytest.mark.integration
@pytest.mark.parametrize(
    "change",
    [
        "cancel",
        "pause",
        "version",
        "generation",
        "paths",
        "effect",
        "child_launch",
        "parent_launch",
        "source",
    ],
)
async def test_scope_response_defers_when_current_child_or_source_differs(
    session_factory, tmp_path, change
):
    factory, application, primary, child = await response_case(session_factory, tmp_path)
    async with factory() as work:
        row = await work.session.get(SubscriptionTask, child.task.task_id)
        scheduled = await work.session.get(SubscriptionScheduledTask, child.task.task_id)
        if change == "cancel":
            row.cancel_requested = True
        elif change == "pause":
            row.pause_requested = True
        elif change == "version":
            row.version += 1
        elif change == "generation":
            scheduled.lease_generation += 1
        elif change == "paths":
            scheduled.owned_paths = ["other"]
        elif change == "source":
            (
                await work.session.get(SubscriptionAttemptResult, child.attempt.attempt_id)
            ).result_digest = "f" * 64
        elif change.endswith("launch"):
            identity = (
                child.attempt.attempt_id if change == "child_launch" else primary.attempt.attempt_id
            )
            await work.session.delete(
                await work.session.scalar(
                    select(SubscriptionClientLaunch).where(
                        SubscriptionClientLaunch.attempt_id == identity
                    )
                )
            )
        else:
            work.session.add(
                SubscriptionScheduledEffect(
                    id=uuid4(),
                    run_id=child.task.run_id,
                    task_id=child.task.task_id,
                    lease_owner=child.lease.owner,
                    lease_generation=child.lease.generation,
                    candidate_epoch=child.candidate_epoch,
                )
            )
        await work.commit()
    with pytest.raises(SubscriptionDecisionError):
        await application.apply_scope_response(primary.attempt.attempt_id)
    async with factory() as work:
        assert await work.session.get(SubscriptionRepairDebit, primary.attempt.attempt_id) is None
        assert (
            await work.session.get(SubscriptionAttemptResult, primary.attempt.attempt_id)
        ).disposition == "decision_pending"


@pytest.mark.integration
async def test_scope_response_rollback_and_concurrent_replay(session_factory, tmp_path):
    factory, application, primary, child = await response_case(session_factory, tmp_path)
    async with factory() as work:
        await work.subscription_decisions.apply_scope_response(primary.attempt.attempt_id)
        await work.rollback()
    async with factory() as work:
        assert (
            decode_subscription_record(
                (await work.session.get(SubscriptionTask, child.task.task_id)).payload
            )
            == child.task
        )
    outcomes = await asyncio.wait_for(
        asyncio.gather(
            application.apply_scope_response(primary.attempt.attempt_id),
            application.apply_scope_response(primary.attempt.attempt_id),
        ),
        10,
    )
    assert sorted(value.replayed for value in outcomes) == [False, True]


@pytest.mark.integration
async def test_granted_scope_queues_behind_active_sibling(session_factory, tmp_path):
    factory, application, primary, child = await response_case(session_factory, tmp_path)
    async with factory() as work:
        worktree = (
            await work.session.get(SubscriptionScheduledTask, child.task.task_id)
        ).worktree_id
        await _enqueue(
            work,
            child.task.run_id,
            provider="p",
            worktree=worktree,
            parent_id=primary.task.task_id,
            paths=("apps/shared",),
        )
        await work.commit()
    sibling = await _claim(session_factory, "sibling")
    assert sibling is not None
    await application.apply_scope_response(primary.attempt.attempt_id)
    executor = SubscriptionDecisionExecutor(factory)
    next_primary = await executor.admit_next("primary", _reservation())
    assert next_primary is not None and next_primary.task.task_id == primary.task.task_id
    assert await executor.admit_next("blocked-child", _reservation()) is None
    async with factory() as work:
        await work.scheduler.finish(sibling, successful=True)
        await work.commit()
    resumed = await executor.admit_next("child", _reservation())
    assert resumed is not None and resumed.task.task_id == child.task.task_id


@pytest.mark.integration
@pytest.mark.parametrize("change", ["receipt", "request_source", "decision"])
async def test_scope_response_replay_detects_changed_evidence(session_factory, tmp_path, change):
    from forge.persistence.models.subscription import SubscriptionDecisionRecord

    factory, application, primary, child = await response_case(session_factory, tmp_path)
    await application.apply_scope_response(primary.attempt.attempt_id)
    async with factory() as work:
        if change == "receipt":
            (
                await work.session.get(SubscriptionAttemptResult, primary.attempt.attempt_id)
            ).application_digest = "f" * 64
        elif change == "request_source":
            (
                await work.session.get(SubscriptionAttemptResult, child.attempt.attempt_id)
            ).result_digest = "f" * 64
        else:
            record = await work.session.scalar(
                select(SubscriptionDecisionRecord).where(
                    SubscriptionDecisionRecord.attempt_id == primary.attempt.attempt_id
                )
            )
            record.payload = {"invalid": True}
        await work.commit()
    with pytest.raises(SubscriptionDecisionError):
        await application.apply_scope_response(primary.attempt.attempt_id)


@pytest.mark.integration
async def test_new_response_cannot_answer_already_resumed_request(session_factory, tmp_path):
    factory, application, primary, child = await response_case(session_factory, tmp_path)
    await application.apply_scope_response(primary.attempt.attempt_id)
    executor = SubscriptionDecisionExecutor(factory)
    next_primary = await executor.admit_next("primary", _reservation())
    assert next_primary is not None and next_primary.task.task_id == primary.task.task_id
    proof = await record_stopped_launch(session_factory, next_primary)
    await executor.settle(
        next_primary,
        SubscriptionInvocationResult(
            attempt=next_primary.attempt,
            decision=BoundScopeResponseDecision(
                run_id=child.task.run_id,
                task_id=child.task.task_id,
                request_attempt_id=child.attempt.attempt_id,
                granted_paths=("apps/shared",),
                reason="Repeated answer",
            ),
            telemetry=_known(),
            launch_proof=proof,
        ),
    )
    with pytest.raises(SubscriptionDecisionError, match="no longer current"):
        await application.apply_scope_response(next_primary.attempt.attempt_id)
    async with factory() as work:
        assert (
            await work.session.get(SubscriptionRepairDebit, next_primary.attempt.attempt_id) is None
        )


@pytest.mark.integration
async def test_periodic_recovery_applies_response_once(session_factory, tmp_path):
    factory, _, _, _ = await response_case(session_factory, tmp_path)
    recovery = SubscriptionDecisionRecovery(
        factory, FilesystemArtifactStore(tmp_path / "response-artifacts")
    )
    report = await recovery.reconcile_all()
    assert report.applied == 1 and report.deferred == report.unsupported == 0
    assert (await recovery.reconcile_all()).applied == 0


@pytest.mark.integration
async def test_scope_response_can_grant_and_deny_parts_of_request(session_factory, tmp_path):
    factory, application, primary, child = await response_case(
        session_factory,
        tmp_path,
        requested_paths=("apps/shared", "outside"),
        mutate=lambda value: replace(
            value, granted_paths=("apps/shared",), denied_paths=("outside",)
        ),
    )
    assert (await application.apply_scope_response(primary.attempt.attempt_id)).accepted
    async with factory() as work:
        updated = decode_subscription_record(
            (await work.session.get(SubscriptionTask, child.task.task_id)).payload
        )
        assert updated.owned_paths == child.task.owned_paths + ("apps/shared",)


@pytest.mark.integration
async def test_old_response_cannot_answer_a_new_request_from_same_child(session_factory, tmp_path):
    factory, application, primary, child = await response_case(session_factory, tmp_path)
    await application.apply_scope_response(primary.attempt.attempt_id)
    executor = SubscriptionDecisionExecutor(factory)
    new_primary = await executor.admit_next("new-primary", _reservation())
    new_child = await executor.admit_next("new-child", _reservation())
    assert (
        new_primary.task.task_id == primary.task.task_id
        and new_child.task.task_id == child.task.task_id
    )
    proof = await record_stopped_launch(session_factory, new_child)
    await executor.settle(
        new_child,
        SubscriptionInvocationResult(
            attempt=new_child.attempt,
            decision=ScopeRequestDecision(
                run_id=child.task.run_id,
                task_id=child.task.task_id,
                requested_paths=("apps/next",),
                reason="Next interface",
            ),
            telemetry=_known(),
            launch_proof=proof,
        ),
    )
    await application.apply_scope_request(new_child.attempt.attempt_id)
    proof = await record_stopped_launch(session_factory, new_primary)
    await executor.settle(
        new_primary,
        SubscriptionInvocationResult(
            attempt=new_primary.attempt,
            decision=BoundScopeResponseDecision(
                run_id=child.task.run_id,
                task_id=child.task.task_id,
                request_attempt_id=child.attempt.attempt_id,
                granted_paths=("apps/shared",),
                reason="Old response",
            ),
            telemetry=_known(),
            launch_proof=proof,
        ),
    )
    with pytest.raises(SubscriptionDecisionError, match="no longer current"):
        await application.apply_scope_response(new_primary.attempt.attempt_id)
    assert (await application.apply_scope_response(primary.attempt.attempt_id)).replayed
    async with factory() as work:
        current = await work.session.get(SubscriptionTask, child.task.task_id)
        assert (
            current.state == "blocked"
            and decode_subscription_record(current.payload) == new_child.task
        )
