"""Typed results atomically settle usage and schedule bounded repairs."""

import pytest
from forge.application.ports.subscription_gateway import (
    SubscriptionFailure,
    SubscriptionInvocationResult,
)
from forge.persistence.models.scheduling import SubscriptionScheduledTask
from forge.persistence.models.subscription import SubscriptionAttempt
from forge.persistence.unit_of_work import PostgresUnitOfWork
from sqlalchemy import select
from subscription_launch_fixture import record_stopped_launch
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_execution_constraints import _admitted
from test_subscription_usage import _known, _reservation


@pytest.mark.integration
async def test_failed_attempt_charges_once_and_schedules_fresh_bounded_repair(
    session_factory, persisted_run
):
    executor, admission = await _admitted(session_factory, persisted_run)
    result = SubscriptionInvocationResult(
        attempt=admission.attempt,
        failure=SubscriptionFailure.PROTOCOL,
        telemetry=_known(input_tokens=5),
    )
    settled = await executor.settle(admission, result)
    assert settled.disposition == "repair_queued"
    assert (await executor.settle(admission, result)).replayed
    async with PostgresUnitOfWork(session_factory) as work:
        usage = await work.subscription_budget.usage(persisted_run.id)
        assert usage.consumed.provider_attempts == 1 and usage.consumed.repairs == 1
        assert usage.consumed.input_tokens == 5 and usage.outstanding.provider_attempts == 1
        attempt = await work.session.get(SubscriptionAttempt, admission.attempt.attempt_id)
        assert attempt.status == "terminal"
        task = await work.session.scalar(
            select(SubscriptionScheduledTask).where(
                SubscriptionScheduledTask.task_id == admission.task.task_id
            )
        )
        assert task.state == "queued" and task.repairs == 1
    again = await executor.admit_next("worker", _reservation())
    assert again is not None and again.attempt.attempt_number == 2
    assert again.attempt.attempt_id != admission.attempt.attempt_id


@pytest.mark.integration
async def test_repeated_failures_exhaust_durable_attempts_without_reset(
    session_factory, persisted_run
):
    executor, admission = await _admitted(session_factory, persisted_run)
    for number in range(1, 4):
        assert admission.attempt.attempt_number == number
        result = SubscriptionInvocationResult(
            attempt=admission.attempt, failure=SubscriptionFailure.PROTOCOL, telemetry=_known()
        )
        settled = await executor.settle(admission, result)
        assert settled.disposition == ("repair_queued" if number < 3 else "failed")
        assert (await executor.settle(admission, result)).replayed
        admission = await executor.admit_next("worker", _reservation())
    assert admission is None
    async with PostgresUnitOfWork(session_factory) as work:
        usage = await work.subscription_budget.usage(persisted_run.id)
        assert usage.consumed.provider_attempts == 3
        assert usage.consumed.repairs == 2
        assert usage.outstanding.provider_attempts == 0


@pytest.mark.integration
async def test_worker_failed_handoff_queues_repair_without_primary_turn(
    session_factory, persisted_run
):
    from forge.domain.subscription import HandoffStatus, TaskHandoff

    executor, admission = await _admitted(session_factory, persisted_run)
    handoff = TaskHandoff(
        run_id=persisted_run.id,
        task_id=admission.task.task_id,
        attempt_id=admission.attempt.attempt_id,
        status=HandoffStatus.FAILED,
        summary="Focused check failed; repair within assigned scope",
    )
    launch_proof = await record_stopped_launch(session_factory, admission)
    result = SubscriptionInvocationResult(
        launch_proof=launch_proof, attempt=admission.attempt, decision=handoff, telemetry=_known()
    )
    assert (await executor.settle(admission, result)).disposition == "repair_queued"
    async with PostgresUnitOfWork(session_factory) as work:
        usage = await work.subscription_budget.usage(persisted_run.id)
        assert usage.consumed.repairs == 1
    assert (await executor.admit_next("worker", _reservation())).attempt.attempt_number == 2


@pytest.mark.integration
async def test_sibling_repairs_reserve_the_last_attempt_slot_once(session_factory, persisted_run):
    import asyncio

    from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
    from test_scheduler_acceptance import _admit_run, _enqueue, _route

    async with PostgresUnitOfWork(session_factory) as work:
        primary = await _admit_run(work, persisted_run, (_route("p"), _route("p")))
        for path in ("apps/a", "apps/b"):
            await _enqueue(
                work,
                persisted_run.id,
                provider="p",
                worktree="sibling-tree",
                parent_id=primary,
                paths=(path,),
            )
        await work.commit()
    executor = SubscriptionDecisionExecutor(lambda: PostgresUnitOfWork(session_factory))
    admissions = [
        await executor.admit_next("worker-a", _reservation()),
        await executor.admit_next("worker-b", _reservation()),
    ]
    assert all(admissions)
    settled = await asyncio.gather(
        *(
            executor.settle(
                admission,
                SubscriptionInvocationResult(
                    attempt=admission.attempt,
                    failure=SubscriptionFailure.PROTOCOL,
                    telemetry=_known(),
                ),
            )
            for admission in admissions
        )
    )
    assert sorted(item.disposition for item in settled) == ["failed", "repair_queued"]
    async with PostgresUnitOfWork(session_factory) as work:
        usage = await work.subscription_budget.usage(persisted_run.id)
        assert usage.consumed.provider_attempts == 2 and usage.consumed.repairs == 1
        assert usage.outstanding.provider_attempts == 1
    assert await executor.admit_next("repair", _reservation()) is not None
    async with PostgresUnitOfWork(session_factory) as work:
        usage = await work.subscription_budget.usage(persisted_run.id)
        assert usage.outstanding.provider_attempts == 1


@pytest.mark.integration
async def test_task_version_change_revokes_old_result_but_retains_usage(
    session_factory, persisted_run
):
    from forge.persistence.models.subscription import SubscriptionTask

    executor, admission = await _admitted(session_factory, persisted_run)
    async with PostgresUnitOfWork(session_factory) as work:
        task = await work.session.get(SubscriptionTask, admission.task.task_id)
        task.version += 1
        await work.commit()
    result = SubscriptionInvocationResult(
        attempt=admission.attempt,
        failure=SubscriptionFailure.PROTOCOL,
        telemetry=_known(input_tokens=5),
    )
    receipt = await executor.settle(admission, result)
    assert not receipt.accepted and receipt.disposition == "stale"
    async with PostgresUnitOfWork(session_factory) as work:
        usage = await work.subscription_budget.usage(persisted_run.id)
        assert usage.consumed.provider_attempts == 1 and usage.consumed.repairs == 0


@pytest.mark.integration
async def test_unapplied_decision_keeps_conflicting_task_fenced(session_factory, persisted_run):
    from forge.domain.subscription import HandoffStatus, TaskHandoff
    from test_scheduler_acceptance import _enqueue

    executor, admission = await _admitted(session_factory, persisted_run)
    async with PostgresUnitOfWork(session_factory) as work:
        await _enqueue(
            work,
            persisted_run.id,
            provider="p",
            worktree="constraint-tree",
            parent_id=admission.task.parent_task_id,
            paths=("apps",),
        )
        await work.commit()
    launch_proof = await record_stopped_launch(session_factory, admission)
    result = SubscriptionInvocationResult(
        launch_proof=launch_proof,
        attempt=admission.attempt,
        telemetry=_known(),
        decision=TaskHandoff(
            run_id=persisted_run.id,
            task_id=admission.task.task_id,
            attempt_id=admission.attempt.attempt_id,
            status=HandoffStatus.COMPLETED,
            candidate_tree_digest="a" * 64,
            evidence_receipt_ids=("not-yet-verified",),
        ),
    )
    settlement = await executor.settle(admission, result)
    assert settlement.disposition == "decision_pending" and not settlement.accepted
    assert await executor.admit_next("conflicting", _reservation()) is None


@pytest.mark.integration
async def test_unsafe_handoff_settles_usage_without_retaining_credentials(
    session_factory, persisted_run
):
    import json
    from dataclasses import replace

    from forge.domain.subscription import HandoffStatus, TaskHandoff
    from forge.persistence.models.subscription import SubscriptionDecisionRecord
    from forge.persistence.models.subscription_results import SubscriptionAttemptResult
    from forge.persistence.repositories.subscription import SubscriptionConflict

    executor, admission = await _admitted(session_factory, persisted_run)
    synthetic = "synthetic-credential-for-redaction-test"
    handoff = TaskHandoff(
        run_id=persisted_run.id,
        task_id=admission.task.task_id,
        attempt_id=admission.attempt.attempt_id,
        status=HandoffStatus.FAILED,
        summary="password=" + synthetic,
    )
    launch_proof = await record_stopped_launch(session_factory, admission)
    result = SubscriptionInvocationResult(
        launch_proof=launch_proof,
        attempt=admission.attempt,
        decision=handoff,
        telemetry=_known(input_tokens=7),
    )
    settled = await executor.settle(admission, result)
    assert settled.disposition == "repair_queued"
    assert (await executor.settle(admission, result)).replayed
    with pytest.raises(SubscriptionConflict, match="replay conflicts"):
        await executor.settle(
            admission,
            replace(
                result, decision=replace(handoff, summary="password=different-synthetic-value")
            ),
        )
    async with PostgresUnitOfWork(session_factory) as work:
        usage = await work.subscription_budget.usage(persisted_run.id)
        assert usage.consumed.input_tokens == 7 and usage.consumed.repairs == 1
        receipt = await work.session.get(SubscriptionAttemptResult, admission.attempt.attempt_id)
        records = (
            await work.session.scalars(
                select(SubscriptionDecisionRecord).where(
                    SubscriptionDecisionRecord.attempt_id == admission.attempt.attempt_id
                )
            )
        ).all()
        assert len(records) == 1
        assert synthetic not in json.dumps([receipt.result_payload, records[0].payload])


@pytest.mark.integration
async def test_failure_detail_redaction_preserves_exact_replay_identity(
    session_factory, persisted_run
):
    from dataclasses import replace

    from forge.persistence.repositories.subscription import SubscriptionConflict

    executor, admission = await _admitted(session_factory, persisted_run)
    result = SubscriptionInvocationResult(
        attempt=admission.attempt,
        failure=SubscriptionFailure.PROTOCOL,
        failure_detail="password=synthetic-first-value",
        telemetry=_known(),
    )
    assert (await executor.settle(admission, result)).accepted
    assert (await executor.settle(admission, result)).replayed
    with pytest.raises(SubscriptionConflict, match="replay conflicts"):
        await executor.settle(
            admission, replace(result, failure_detail="password=synthetic-second-value")
        )


@pytest.mark.integration
async def test_unsafe_telemetry_text_preserves_measured_usage_and_exact_replay(
    session_factory, persisted_run
):
    import json
    from dataclasses import replace

    from forge.persistence.models.subscription_results import SubscriptionAttemptResult
    from forge.persistence.repositories.subscription import SubscriptionConflict

    executor, admission = await _admitted(session_factory, persisted_run)
    telemetry = replace(
        _known(input_tokens=9), unknown_telemetry_reasons=("password=synthetic-telemetry-secret",)
    )
    result = SubscriptionInvocationResult(
        attempt=admission.attempt, failure=SubscriptionFailure.PROTOCOL, telemetry=telemetry
    )
    assert (await executor.settle(admission, result)).accepted
    assert (await executor.settle(admission, result)).replayed
    with pytest.raises(SubscriptionConflict, match="replay conflicts"):
        await executor.settle(
            admission,
            replace(
                result,
                telemetry=replace(
                    telemetry, unknown_telemetry_reasons=("password=different-synthetic-secret",)
                ),
            ),
        )
    async with PostgresUnitOfWork(session_factory) as work:
        usage = await work.subscription_budget.usage(persisted_run.id)
        assert usage.consumed.input_tokens == 9 and usage.consumed.provider_attempts == 1
        attempt = await work.session.get(SubscriptionAttempt, admission.attempt.attempt_id)
        receipt = await work.session.get(SubscriptionAttemptResult, admission.attempt.attempt_id)
        stored = json.dumps([attempt.telemetry_payload, receipt.result_payload])
        assert "synthetic-telemetry-secret" not in stored


@pytest.mark.integration
async def test_pending_nested_decision_is_retained_losslessly(session_factory, persisted_run):
    from dataclasses import replace
    from uuid import uuid4

    from forge.domain.subscription import DelegateDecision, decode_subscription_record
    from forge.persistence.models.subscription_results import SubscriptionAttemptResult

    executor, admission = await _admitted(session_factory, persisted_run)
    child = replace(admission.task, task_id=uuid4(), parent_task_id=admission.task.task_id)
    decision = DelegateDecision(
        run_id=persisted_run.id,
        parent_task_id=admission.task.task_id,
        child_tasks=(child,),
        rationale="Assigned work",
    )
    launch_proof = await record_stopped_launch(session_factory, admission)
    result = SubscriptionInvocationResult(
        launch_proof=launch_proof, attempt=admission.attempt, decision=decision, telemetry=_known()
    )
    assert (await executor.settle(admission, result)).disposition == "decision_pending"
    async with PostgresUnitOfWork(session_factory) as work:
        receipt = await work.session.get(SubscriptionAttemptResult, admission.attempt.attempt_id)
        assert decode_subscription_record(receipt.result_payload["decision"]) == decision


@pytest.mark.integration
async def test_candidate_barrier_drains_an_admitted_result(session_factory, persisted_run):
    from forge.persistence.models.scheduling import SubscriptionSchedulerRun

    executor, admission = await _admitted(session_factory, persisted_run)
    async with PostgresUnitOfWork(session_factory) as work:
        epoch = await work.scheduler.begin_candidate(persisted_run.id)
        await work.commit()
    result = SubscriptionInvocationResult(
        attempt=admission.attempt, failure=SubscriptionFailure.POLICY_DENIED, telemetry=_known()
    )
    settled = await executor.settle(admission, result)
    assert settled.accepted and settled.disposition == "failed"
    async with PostgresUnitOfWork(session_factory) as work:
        await work.scheduler.close_candidate(persisted_run.id, epoch)
        row = await work.session.get(SubscriptionSchedulerRun, persisted_run.id)
        assert row.candidate_state == "closed" and row.candidate_epoch > admission.candidate_epoch
        await work.commit()


@pytest.mark.integration
async def test_schema_one_result_replay_remains_compatible(session_factory, persisted_run):
    import hashlib
    import json

    from forge.application.services.tools import _safe_metadata
    from forge.domain.subscription import encode_subscription_record
    from forge.persistence.models.subscription_results import SubscriptionAttemptResult

    executor, admission = await _admitted(session_factory, persisted_run)
    result = SubscriptionInvocationResult(
        attempt=admission.attempt, failure=SubscriptionFailure.PROTOCOL, telemetry=_known()
    )
    await executor.settle(admission, result)
    legacy = {
        "schema_version": 1,
        "attempt": encode_subscription_record(admission.attempt),
        "decision": None,
        "telemetry": encode_subscription_record(result.telemetry),
        "failure": "protocol",
        "failure_detail": None,
    }
    async with PostgresUnitOfWork(session_factory) as work:
        row = await work.session.get(SubscriptionAttemptResult, admission.attempt.attempt_id)
        row.result_payload = _safe_metadata(legacy)
        row.result_digest = hashlib.sha256(
            json.dumps(legacy, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        await work.commit()
    assert (await executor.settle(admission, result)).replayed
    async with PostgresUnitOfWork(session_factory) as work:
        usage = await work.subscription_budget.usage(persisted_run.id)
        assert usage.consumed.repairs == 1 and usage.consumed.provider_attempts == 1


@pytest.mark.integration
async def test_unsafe_plan_is_a_protocol_failure_not_a_modified_pending_proposal(
    session_factory, persisted_run
):
    from forge.domain.plan import PlanOutput
    from forge.persistence.models.subscription_results import SubscriptionAttemptResult

    executor, admission = await _admitted(session_factory, persisted_run)
    plan = PlanOutput(
        summary="password=synthetic-plan-secret",
        assumptions=(),
        affected_components=("apps",),
        steps=("Implement",),
        required_checks=("test",),
        risks=("Changes",),
        security_considerations=(),
        dependency_changes=(),
    )
    launch_proof = await record_stopped_launch(session_factory, admission)
    result = SubscriptionInvocationResult(
        launch_proof=launch_proof,
        attempt=admission.attempt,
        decision=plan,
        telemetry=_known(input_tokens=3),
    )
    settled = await executor.settle(admission, result)
    assert settled.accepted and settled.disposition == "repair_queued"
    async with PostgresUnitOfWork(session_factory) as work:
        row = await work.session.get(SubscriptionAttemptResult, admission.attempt.attempt_id)
        assert row.result_payload["effective_failure"] == SubscriptionFailure.PROTOCOL.value
        assert "synthetic-plan-secret" not in str(row.result_payload)
        assert (await work.subscription_budget.usage(persisted_run.id)).consumed.input_tokens == 3
