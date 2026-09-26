"""Individual controls retain stopped decisions and revoke late provider output."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from forge.application.ports.subscription_decisions import SubscriptionDecisionError
from forge.application.ports.subscription_gateway import (
    SubscriptionFailure,
    SubscriptionInvocationResult,
)
from forge.application.services.subscription_decisions import SubscriptionDecisionApplication
from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
from forge.application.services.subscription_profiles import LocalOperatorProfileActor
from forge.application.services.subscription_task_controls import SubscriptionTaskControlService
from forge.domain.operation import canonical_digest
from forge.domain.subscription import BoundScopeResponseDecision, ScopeRequestDecision, TaskBudget
from forge.domain.subscription_launch import SubscriptionLaunchTerminalProof
from forge.domain.subscription_task_controls import (
    SubscriptionTaskControlRequest,
    TaskControlConflict,
)
from forge.persistence.models.scheduling import SubscriptionScheduledTask
from forge.persistence.models.subscription import SubscriptionAttempt, SubscriptionTask
from forge.persistence.models.subscription_results import (
    SubscriptionAttemptResult,
    SubscriptionRepairDebit,
)
from sqlalchemy import func, select
from subscription_launch_fixture import record_stopped_launch
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_delegation_application import delegation_case
from test_subscription_scope_application import scope_case
from test_subscription_usage import _known, _reservation


async def control(factory, child, action, *, pause_id=None, key=None):
    async with factory() as work:
        run = await work.runs.get(child.task.run_id)
        task = await work.session.get(SubscriptionTask, child.task.task_id)
        request = SubscriptionTaskControlRequest(
            action=action,
            expected_run_version=run.version,
            expected_task_version=task.version,
            reason="Operator investigation",
            pause_receipt_id=pause_id,
        )
    return await SubscriptionTaskControlService(factory).control(
        run_id=child.task.run_id,
        task_id=child.task.task_id,
        actor=LocalOperatorProfileActor(),
        idempotency_key=key or str(uuid4()),
        request=request,
    )


@pytest.mark.integration
async def test_pending_decision_pause_resume_preserves_source_and_applies_once(
    session_factory, tmp_path
):
    factory, application, parent, child = await scope_case(session_factory, tmp_path)
    async with factory() as work:
        result = await work.session.get(SubscriptionAttemptResult, child.attempt.attempt_id)
        original = canonical_digest(result.result_payload), result.result_digest
        before = await work.session.scalar(select(func.count()).select_from(SubscriptionAttempt))
        usage = await work.subscription_budget.usage(child.task.run_id, child.task.task_id)

    paused = await control(factory, child, "pause")
    assert paused.status == "paused" and paused.task_version == child.task_version + 2
    with pytest.raises(SubscriptionDecisionError):
        await application.apply_scope_request(child.attempt.attempt_id)
    async with factory() as work:
        assert (await work.session.get(SubscriptionTask, parent.task.task_id)).state == "blocked"
        assert await work.scheduler._active_count(run_id=child.task.run_id) == 0

    # A new service and new transactions must recover the same retained decision.
    resumed = await control(factory, child, "resume", pause_id=paused.receipt_id)
    assert resumed.status == "decision_pending" and resumed.task_version == paused.task_version + 1
    applied = await application.apply_scope_request(child.attempt.attempt_id)
    assert applied.accepted and applied.disposition == "scope_requested"
    assert (await application.apply_scope_request(child.attempt.attempt_id)).replayed
    async with factory() as work:
        result = await work.session.get(SubscriptionAttemptResult, child.attempt.attempt_id)
        assert (canonical_digest(result.result_payload), result.result_digest) == original
        assert (
            await work.session.scalar(select(func.count()).select_from(SubscriptionAttempt))
            == before
        )
        assert await work.session.get(SubscriptionRepairDebit, child.attempt.attempt_id) is None
        assert await work.subscription_budget.usage(child.task.run_id, child.task.task_id) == usage
        scheduled = await work.session.get(SubscriptionScheduledTask, child.task.task_id)
        assert scheduled.repairs == 0 and scheduled.state == "blocked"
        assert (await work.session.get(SubscriptionTask, parent.task.task_id)).state == "queued"


async def active_case(session_factory, tmp_path):
    factory, parent, children, _ = await delegation_case(
        session_factory,
        tmp_path,
        lambda child, _: (
            replace(child, budget=TaskBudget(max_provider_attempts=3), max_repairs=2),
        ),
        primary_budget=TaskBudget(max_provider_attempts=8),
        plan_scope=("apps",),
    )
    await SubscriptionDecisionApplication(factory).apply_delegation(parent.attempt.attempt_id)
    executor = SubscriptionDecisionExecutor(factory)
    child = await executor.admit_next("worker", _reservation())
    assert child is not None and child.task.task_id == children[0].task_id
    proof = SubscriptionLaunchTerminalProof(
        launch_id=str(uuid4()),
        pid=12345,
        process_identity="controlled-task-stop-process",
        outcome="exited",
        return_code=0,
        stop_confirmed=True,
        stdout_bytes=100,
        stderr_bytes=0,
        stdout_truncated=False,
        stderr_truncated=False,
    )
    async with factory() as work:
        await work.subscription.launch_intent(
            child.attempt.attempt_id, proof.launch_id, worker_identity=child.lease.owner
        )
        await work.subscription.launch_started(
            child.attempt.attempt_id,
            proof.launch_id,
            worker_identity=child.lease.owner,
            pid=proof.pid,
            process_start_token=proof.process_identity,
        )
        await work.commit()
    return factory, parent, child, executor, proof


async def finish_client(factory, child, proof, *, uncertain=False):
    async with factory() as work:
        await work.subscription.launch_finished(
            child.attempt.attempt_id,
            proof.launch_id,
            worker_identity=child.lease.owner,
            terminal=proof,
            uncertain=uncertain,
        )
        await work.commit()


def scope_result(child, proof):
    return SubscriptionInvocationResult(
        attempt=child.attempt,
        decision=ScopeRequestDecision(
            run_id=child.task.run_id,
            task_id=child.task.task_id,
            requested_paths=("apps/shared",),
            reason="Shared interface needed",
        ),
        telemetry=_known(),
        launch_proof=proof,
    )


@pytest.mark.integration
async def test_late_result_is_retained_but_resume_requires_a_fresh_budgeted_attempt(
    session_factory, tmp_path
):
    from forge.application.ports.scheduling import SchedulingConflict

    factory, parent, child, executor, proof = await active_case(session_factory, tmp_path)
    paused = await control(factory, child, "pause")
    assert paused.status == "pause_requested"
    recovery = SubscriptionTaskControlService(factory)
    assert (await recovery.reconcile_all()).deferred == 1
    with pytest.raises(TaskControlConflict):
        await control(factory, child, "resume", pause_id=paused.receipt_id)
    async with factory() as work:
        assert await work.scheduler._active_count(run_id=child.task.run_id) == 1
        with pytest.raises(SchedulingConflict):
            await work.scheduler.renew(child.lease, timedelta(seconds=30))
    await finish_client(factory, child, proof)
    result = scope_result(child, proof)
    assert (await executor.settle(child, result)).disposition == "stale"
    assert (await recovery.reconcile_all()).stopped == 1
    async with factory() as work:
        original = await work.session.get(SubscriptionAttemptResult, child.attempt.attempt_id)
        digest = original.result_digest
        assert not original.accepted
        assert await work.scheduler._active_count(run_id=child.task.run_id) == 0
        assert (await work.session.get(SubscriptionTask, parent.task.task_id)).state == "blocked"
    resumed = await control(factory, child, "resume", pause_id=paused.receipt_id)
    assert resumed.status == "queued"
    with pytest.raises(SubscriptionDecisionError):
        await SubscriptionDecisionApplication(factory).apply_scope_request(child.attempt.attempt_id)
    async with factory() as work:
        original = await work.session.get(SubscriptionAttemptResult, child.attempt.attempt_id)
        assert original.result_digest == digest and not original.accepted
        assert await work.session.get(SubscriptionRepairDebit, child.attempt.attempt_id) is not None
        assert (await work.session.get(SubscriptionScheduledTask, child.task.task_id)).repairs == 1
    next_attempt = await executor.admit_next("replacement-worker", _reservation())
    assert next_attempt is not None and next_attempt.task == child.task
    assert next_attempt.attempt.attempt_number == 2
    assert next_attempt.attempt.attempt_id != child.attempt.attempt_id


@pytest.mark.integration
@pytest.mark.parametrize("action", ["pause", "cancel"])
async def test_unconfirmed_client_keeps_control_requested_and_capacity_owned(
    session_factory, tmp_path, action
):
    factory, _, child, executor, proof = await active_case(session_factory, tmp_path)
    requested = await control(factory, child, action)
    assert requested.status == f"{action}_requested"
    uncertain = proof.model_copy(
        update={"stop_confirmed": False, "outcome": "uncertain", "return_code": None}
    )
    await finish_client(factory, child, uncertain, uncertain=True)
    await executor.settle(
        child,
        SubscriptionInvocationResult(
            attempt=child.attempt,
            failure=SubscriptionFailure.UNCERTAIN,
            telemetry=_known(),
            launch_proof=uncertain,
        ),
    )
    report = await SubscriptionTaskControlService(factory).reconcile_all()
    assert report.stopped == 0 and report.deferred == 1
    async with factory() as work:
        assert await work.scheduler._active_count(run_id=child.task.run_id) == 1
        task = await work.session.get(SubscriptionTask, child.task.task_id)
        assert task.state == "reconciling"
        assert await work.session.get(SubscriptionRepairDebit, child.attempt.attempt_id) is None


@pytest.mark.integration
async def test_cancel_settles_only_after_client_exit_then_wakes_parent_without_a_handoff(
    session_factory, tmp_path
):
    factory, parent, child, executor, proof = await active_case(session_factory, tmp_path)
    receipt = await control(factory, child, "cancel")
    assert receipt.status == "cancel_requested"
    async with factory() as work:
        assert (await work.session.get(SubscriptionTask, parent.task.task_id)).state == "blocked"
    await finish_client(factory, child, proof)
    assert (await executor.settle(child, scope_result(child, proof))).disposition == "stale"
    assert (await SubscriptionTaskControlService(factory).reconcile_all()).stopped == 1
    async with factory() as work:
        task = await work.session.get(SubscriptionTask, child.task.task_id)
        scheduled = await work.session.get(SubscriptionScheduledTask, child.task.task_id)
        assert task.state == scheduled.state == "terminal"
        assert (
            task.cancel_requested and scheduled.cancel_requested and scheduled.lease_owner is None
        )
        result = await work.session.get(SubscriptionAttemptResult, child.attempt.attempt_id)
        assert result.disposition == "task_cancelled" and not result.accepted
        assert (await work.session.get(SubscriptionTask, parent.task.task_id)).state == "queued"
        outcomes = await work.subscription.invocation_outcomes(
            child.task.run_id, (child.task.task_id,)
        )
        outcome = next(value for value in outcomes if value.task_id == child.task.task_id)
        assert outcome.recorded_handoff is None and outcome.cancel_requested


@pytest.mark.integration
async def test_repeated_pause_resume_preserves_scope_response_version_binding(
    session_factory, tmp_path
):
    factory, application, _, child = await scope_case(
        session_factory, tmp_path, child_budget=TaskBudget(max_provider_attempts=3)
    )
    for _ in range(2):
        pause = await control(factory, child, "pause")
        await control(factory, child, "resume", pause_id=pause.receipt_id)
    await application.apply_scope_request(child.attempt.attempt_id)
    executor = SubscriptionDecisionExecutor(factory)
    primary = await executor.admit_next("responding-primary", _reservation())
    assert primary is not None
    proof = await record_stopped_launch(session_factory, primary)
    decision = BoundScopeResponseDecision(
        run_id=child.task.run_id,
        task_id=child.task.task_id,
        request_attempt_id=child.attempt.attempt_id,
        granted_paths=("apps/shared",),
        denied_paths=(),
        reason="Approved scope",
    )
    await executor.settle(
        primary,
        SubscriptionInvocationResult(
            attempt=primary.attempt, decision=decision, telemetry=_known(), launch_proof=proof
        ),
    )
    assert (await application.apply_scope_response(primary.attempt.attempt_id)).accepted
    assert (await application.apply_scope_response(primary.attempt.attempt_id)).replayed


@pytest.mark.integration
@pytest.mark.parametrize("change", ["result", "settlement", "resume_receipt", "candidate"])
async def test_pending_resume_rejects_changed_durable_source(session_factory, tmp_path, change):
    from copy import deepcopy

    from forge.persistence.models.api import ApiMutation
    from forge.persistence.models.scheduling import SubscriptionSchedulerRun
    from forge.persistence.models.subscription_task_stops import SubscriptionTaskStop

    factory, application, _, child = await scope_case(session_factory, tmp_path)
    pause = await control(factory, child, "pause")
    if change == "resume_receipt":
        resume = await control(factory, child, "resume", pause_id=pause.receipt_id)
    async with factory() as work:
        if change == "result":
            row = await work.session.get(SubscriptionAttemptResult, child.attempt.attempt_id)
            payload = deepcopy(row.result_payload)
            payload["decision"]["reason"] = "Changed retained proposal"
            row.result_payload, row.result_digest = payload, canonical_digest(payload)
        elif change == "settlement":
            row = await work.session.get(SubscriptionTaskStop, pause.receipt_id)
            row.settlement_digest = "0" * 64
        elif change == "candidate":
            row = await work.session.get(SubscriptionSchedulerRun, child.task.run_id)
            row.candidate_epoch += 1
        else:
            row = await work.session.get(ApiMutation, resume.receipt_id)
            payload = deepcopy(row.response_payload)
            payload["receipt"]["reason"] = "Changed retained operator response"
            row.response_payload = payload
        await work.commit()
    if change == "resume_receipt":
        with pytest.raises(SubscriptionDecisionError):
            await application.apply_scope_request(child.attempt.attempt_id)
    else:
        with pytest.raises(TaskControlConflict):
            await control(factory, child, "resume", pause_id=pause.receipt_id)


@pytest.mark.integration
async def test_pause_races_application_without_accepting_a_revoked_result(
    session_factory, tmp_path
):
    factory, application, _, child = await scope_case(session_factory, tmp_path)
    outcomes = await asyncio.gather(
        control(factory, child, "pause"),
        application.apply_scope_request(child.attempt.attempt_id),
        return_exceptions=True,
    )
    assert sum(not isinstance(value, BaseException) for value in outcomes) == 1
    async with factory() as work:
        task = await work.session.get(SubscriptionTask, child.task.task_id)
        result = await work.session.get(SubscriptionAttemptResult, child.attempt.attempt_id)
        assert (task.pause_requested, result.accepted) in ((True, False), (False, True))


@pytest.mark.integration
@pytest.mark.parametrize("phase", ["pending", "late"])
async def test_whole_run_pause_resume_preserves_the_separate_task_pause(
    session_factory, tmp_path, phase
):
    from forge.artifacts.filesystem import FilesystemArtifactStore
    from forge.persistence.repositories.commands import PostgresCommandRepository
    from test_subscription_resume_controls import pause_and_resume

    if phase == "pending":
        factory, _, _, child = await scope_case(session_factory, tmp_path)
        pause = await control(factory, child, "pause")
    else:
        factory, _, child, executor, proof = await active_case(session_factory, tmp_path)
        pause = await control(factory, child, "pause")
        await finish_client(factory, child, proof)
        await executor.settle(child, scope_result(child, proof))
        assert (await SubscriptionTaskControlService(factory).reconcile_all()).stopped == 1
    commands = PostgresCommandRepository(session_factory)
    async with factory() as work:
        outstanding = await work.commands.list_outstanding_normal(
            run_id=child.task.run_id, exclude_command_id=None
        )
    for command in outstanding:
        assert command.command_type == "prepare_worktree"
        await commands.complete(command.id, worker_id=command.lease_owner)
    assert (
        await pause_and_resume(
            factory,
            session_factory,
            child.task.run_id,
            FilesystemArtifactStore(tmp_path / "artifacts"),
        )
        is None
    )
    async with factory() as work:
        assert (await work.session.get(SubscriptionTask, child.task.task_id)).pause_requested
        assert await work.session.get(SubscriptionRepairDebit, child.attempt.attempt_id) is None
    resumed = await control(factory, child, "resume", pause_id=pause.receipt_id)
    assert resumed.status == ("decision_pending" if phase == "pending" else "queued")


@pytest.mark.integration
@pytest.mark.parametrize("phase", ["pending", "running", "late"])
async def test_cancel_paused_task_never_requires_a_provider_retry(session_factory, tmp_path, phase):
    if phase == "pending":
        factory, _, _, child = await scope_case(session_factory, tmp_path)
        pause = await control(factory, child, "pause")
    else:
        factory, _, child, executor, proof = await active_case(session_factory, tmp_path)
        pause = await control(factory, child, "pause")
        if phase == "late":
            await finish_client(factory, child, proof)
            await executor.settle(child, scope_result(child, proof))
            assert (await SubscriptionTaskControlService(factory).reconcile_all()).stopped == 1
    cancelled = await control(factory, child, "cancel")
    assert cancelled.status == ("cancel_requested" if phase == "running" else "cancelled")
    if phase == "running":
        await finish_client(factory, child, proof)
        await executor.settle(child, scope_result(child, proof))
        assert (await SubscriptionTaskControlService(factory).reconcile_all()).stopped == 1
    with pytest.raises(TaskControlConflict):
        await control(factory, child, "resume", pause_id=pause.receipt_id)
    async with factory() as work:
        assert (await work.session.get(SubscriptionTask, child.task.task_id)).state == "terminal"
        assert await work.session.get(SubscriptionRepairDebit, child.attempt.attempt_id) is None
        assert (await work.session.get(SubscriptionScheduledTask, child.task.task_id)).repairs == 0


@pytest.mark.integration
async def test_periodic_stop_recovery_skips_a_locked_run_and_finishes_other_work(
    session_factory, tmp_path
):
    from forge.persistence.models.run import Run

    factory, _, child, executor, proof = await active_case(session_factory, tmp_path / "first")
    await control(factory, child, "pause")
    await finish_client(factory, child, proof)
    await executor.settle(child, scope_result(child, proof))
    second_factory, _, other, executor, proof = await active_case(
        session_factory, tmp_path / "second"
    )
    await control(second_factory, other, "pause")
    await finish_client(second_factory, other, proof)
    await executor.settle(other, scope_result(other, proof))
    async with factory() as holder:
        await holder.session.get(Run, child.task.run_id, with_for_update=True)
        report = await asyncio.wait_for(
            SubscriptionTaskControlService(factory).reconcile_all(), timeout=3
        )
        assert report.stopped == 1 and report.deferred == 1
    assert (await SubscriptionTaskControlService(factory).reconcile_all()).stopped == 1


@pytest.mark.integration
async def test_two_recovery_workers_complete_one_stop_without_duplicate_debits(
    session_factory, tmp_path
):
    factory, _, child, executor, proof = await active_case(session_factory, tmp_path)
    await control(factory, child, "cancel")
    await finish_client(factory, child, proof)
    await executor.settle(child, scope_result(child, proof))
    results = await asyncio.gather(
        SubscriptionTaskControlService(factory).reconcile_all(),
        SubscriptionTaskControlService(factory).reconcile_all(),
    )
    assert sum(result.stopped for result in results) == 1
    async with factory() as work:
        assert (await work.session.get(SubscriptionTask, child.task.task_id)).state == "terminal"
        assert await work.session.get(SubscriptionRepairDebit, child.attempt.attempt_id) is None


@pytest.mark.integration
@pytest.mark.parametrize("when", ["before_pause", "before_cancel"])
async def test_expired_running_lease_can_still_be_stopped_by_its_operator(
    session_factory, tmp_path, when
):
    factory, _, child, executor, proof = await active_case(session_factory, tmp_path)
    if when == "before_cancel":
        await control(factory, child, "pause")
    async with factory() as work:
        scheduled = await work.session.get(SubscriptionScheduledTask, child.task.task_id)
        scheduled.lease_expires_at = datetime(2000, 1, 1, tzinfo=UTC)
        await work.commit()
    async with factory() as work:
        assert await work.scheduler.claim_ready("expiry-scan", timedelta(seconds=30)) is None
        await work.commit()
    request = await control(factory, child, "pause" if when == "before_pause" else "cancel")
    assert request.status == ("pause_requested" if when == "before_pause" else "cancel_requested")
    await finish_client(factory, child, proof)
    await executor.settle(child, scope_result(child, proof))
    assert (await SubscriptionTaskControlService(factory).reconcile_all()).stopped == 1


@pytest.mark.integration
async def test_handoff_resume_requires_a_new_observation_version(
    session_factory, tmp_path, monkeypatch
):
    from forge.persistence.repositories.subscription_handoff_fence import (
        PostgresSubscriptionHandoffFence,
    )
    from test_subscription_handoff_application import application_case

    factory, _, child, application, old, evidence = await application_case(
        session_factory, tmp_path, monkeypatch
    )
    pause = await control(factory, child, "pause")
    with pytest.raises(SubscriptionDecisionError):
        await application.apply_handoff(old, evidence)
    await control(factory, child, "resume", pause_id=pause.receipt_id)
    with pytest.raises(SubscriptionDecisionError):
        await application.apply_handoff(old, evidence)
    async with factory() as work:
        assert await PostgresSubscriptionHandoffFence(work.session).release(old)
        await work.commit()
    fresh = await application.begin_handoff_observation(child.attempt.attempt_id, uuid4())
    assert fresh.proposal.task_version == old.proposal.task_version + 2
    assert (await application.apply_handoff(fresh, evidence)).accepted
    assert (await application.apply_handoff(fresh, evidence)).replayed


@pytest.mark.integration
async def test_task_stop_migration_refuses_to_discard_operator_evidence(
    session_factory, tmp_path, test_database_url, alembic_config_factory
):
    from alembic import command
    from forge.persistence.models.subscription_task_stops import SubscriptionTaskStop

    factory, _, _, child = await scope_case(session_factory, tmp_path)
    pause = await control(factory, child, "pause")
    with pytest.raises(RuntimeError, match="cannot discard retained"):
        await asyncio.to_thread(
            command.downgrade, alembic_config_factory(test_database_url), "20260912_0019"
        )
    async with factory() as work:
        assert (await work.session.get(SubscriptionTaskStop, pause.receipt_id)).state == "paused"
