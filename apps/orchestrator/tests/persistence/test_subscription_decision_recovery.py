"""Recovery consumes settled evidence without another provider invocation."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from forge.application.services.auth import AuthenticatedActor
from forge.application.services.subscription_decision_recovery import SubscriptionDecisionRecovery
from forge.application.services.subscription_recovery import SubscriptionRecoveryService
from forge.domain.run import RunState
from forge.domain.subscription_recovery import (
    RecoveryAction,
    RecoveryApplyRequest,
    RecoveryPreviewRequest,
)
from forge.persistence.models.scheduling import SubscriptionScheduledTask
from forge.persistence.models.subscription import SubscriptionAttempt
from forge.persistence.models.subscription_recovery import (
    SubscriptionApplicationDiagnostic,
    SubscriptionRecoveryWorker,
)
from sqlalchemy import func, select
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_plan_gate import proposal_case


async def primary_handoff_case(session_factory, tmp_path, *, repairs=1, candidate_state="open"):
    from dataclasses import replace
    from unittest.mock import patch

    from forge.application.ports.subscription_gateway import SubscriptionInvocationResult
    from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
    from forge.domain.subscription import HandoffStatus, TaskBudget, TaskHandoff
    from subscription_launch_fixture import record_stopped_launch
    from test_subscription_preparation import preparation_case
    from test_subscription_usage import _known, _reservation

    budget = replace(TaskBudget(), max_repairs=repairs, max_provider_attempts=3)
    factory, _, command, prepare, _ = await preparation_case(
        session_factory, tmp_path, primary_budget=budget
    )
    async with factory() as work:
        await prepare.execute(command, work)
    if candidate_state == "closed":
        async with factory() as work:
            epoch = await work.scheduler.begin_candidate(command.run_id)
            await work.scheduler.close_candidate(command.run_id, epoch)
            await work.commit()
    executor = SubscriptionDecisionExecutor(factory)
    admission = await executor.admit_next("primary-handoff", _reservation())
    decision = TaskHandoff(
        run_id=admission.task.run_id,
        task_id=admission.task.task_id,
        attempt_id=admission.attempt.attempt_id,
        status=HandoffStatus.COMPLETED,
        summary="Invalid primary handoff",
        candidate_tree_digest="a" * 64,
        evidence_receipt_ids=("receipt",),
    )
    proof = await record_stopped_launch(session_factory, admission)
    result = SubscriptionInvocationResult(
        attempt=admission.attempt, decision=decision, telemetry=_known(), launch_proof=proof
    )
    # Recreate a retained result written before settlement enforced role policy.
    with patch(
        "forge.persistence.repositories.subscription_execution.decision_allowed", return_value=True
    ):
        assert (await executor.settle(admission, result)).disposition == "decision_pending"
    if candidate_state == "draining":
        async with factory() as work:
            await work.scheduler.begin_candidate(admission.task.run_id)
            await work.commit()
    return factory, admission


@pytest.mark.integration
@pytest.mark.parametrize("candidate_state", ["draining", "closed"])
async def test_primary_handoff_repair_preserves_candidate_barrier(
    session_factory, tmp_path, candidate_state
):
    from forge.application.services.subscription_decisions import SubscriptionDecisionApplication
    from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
    from forge.artifacts.filesystem import FilesystemArtifactStore
    from forge.persistence.models.scheduling import SubscriptionSchedulerRun
    from test_subscription_usage import _reservation

    factory, admission = await primary_handoff_case(
        session_factory, tmp_path, candidate_state=candidate_state
    )
    async with factory() as work:
        before = await work.session.get(SubscriptionSchedulerRun, admission.task.run_id)
        epoch = before.candidate_epoch
    report = await SubscriptionDecisionRecovery(
        factory, FilesystemArtifactStore(tmp_path / "artifacts")
    ).reconcile_all()
    assert (report.applied, report.deferred) == (1, 0)
    replay = await SubscriptionDecisionApplication(factory).reject_invalid_primary_handoff(
        admission.attempt.attempt_id
    )
    assert replay.replayed and replay.disposition == "handoff_repair_queued"
    async with factory() as work:
        scheduler = await work.session.get(SubscriptionSchedulerRun, admission.task.run_id)
        assert (scheduler.candidate_state, scheduler.candidate_epoch) == (candidate_state, epoch)
        if candidate_state == "draining":
            await work.scheduler.close_candidate(admission.task.run_id, epoch)
            await work.commit()
    retry = await SubscriptionDecisionExecutor(factory).admit_next(
        "repaired-primary", _reservation()
    )
    assert retry is not None and retry.task.task_id == admission.task.task_id
    assert retry.attempt.attempt_id != admission.attempt.attempt_id


@pytest.mark.integration
async def test_primary_handoff_draining_rejects_stale_epoch(session_factory, tmp_path):
    from forge.application.ports.subscription_decisions import SubscriptionDecisionError
    from forge.application.services.subscription_decisions import SubscriptionDecisionApplication
    from forge.persistence.models.scheduling import SubscriptionSchedulerRun
    from forge.persistence.models.subscription_results import SubscriptionAttemptResult

    factory, admission = await primary_handoff_case(
        session_factory, tmp_path, candidate_state="draining"
    )
    async with factory() as work:
        scheduler = await work.session.get(SubscriptionSchedulerRun, admission.task.run_id)
        scheduler.candidate_epoch += 1
        await work.commit()
    with pytest.raises(SubscriptionDecisionError, match="no longer current"):
        await SubscriptionDecisionApplication(factory).reject_invalid_primary_handoff(
            admission.attempt.attempt_id
        )
    async with factory() as work:
        result = await work.session.get(SubscriptionAttemptResult, admission.attempt.attempt_id)
        assert result.disposition == "decision_pending" and result.application_payload is None


@pytest.mark.integration
async def test_primary_handoff_closed_rejects_noncoordinator(session_factory, tmp_path):
    from forge.application.ports.subscription_decisions import SubscriptionDecisionError
    from forge.application.services.subscription_decisions import SubscriptionDecisionApplication
    from forge.persistence.models.subscription_results import SubscriptionAttemptResult

    factory, admission = await primary_handoff_case(
        session_factory, tmp_path, candidate_state="closed"
    )
    async with factory() as work:
        scheduled = await work.session.get(SubscriptionScheduledTask, admission.task.task_id)
        scheduled.read_only = True
        await work.commit()
    with pytest.raises(SubscriptionDecisionError, match="no longer current"):
        await SubscriptionDecisionApplication(factory).reject_invalid_primary_handoff(
            admission.attempt.attempt_id
        )
    async with factory() as work:
        result = await work.session.get(SubscriptionAttemptResult, admission.attempt.attempt_id)
        scheduled = await work.session.get(SubscriptionScheduledTask, admission.task.task_id)
        assert result.disposition == "decision_pending" and result.application_payload is None
        assert scheduled.repairs == 0


@pytest.mark.integration
async def test_draining_primary_handoff_exhaustion_allows_barrier_to_close(
    session_factory, tmp_path
):
    from forge.application.services.subscription_decisions import SubscriptionDecisionApplication
    from forge.persistence.models.scheduling import SubscriptionSchedulerRun

    factory, admission = await primary_handoff_case(
        session_factory, tmp_path, repairs=0, candidate_state="draining"
    )
    outcome = await SubscriptionDecisionApplication(factory).reject_invalid_primary_handoff(
        admission.attempt.attempt_id
    )
    assert outcome.disposition == "handoff_rejected"
    async with factory() as work:
        scheduler = await work.session.get(SubscriptionSchedulerRun, admission.task.run_id)
        epoch = scheduler.candidate_epoch
        scheduled = await work.session.get(SubscriptionScheduledTask, admission.task.task_id)
        assert scheduled.state == "terminal" and scheduled.repairs == 0
        await work.scheduler.close_candidate(admission.task.run_id, epoch)
        await work.commit()
    async with factory() as work:
        scheduler = await work.session.get(SubscriptionSchedulerRun, admission.task.run_id)
        assert scheduler.candidate_state == "closed" and scheduler.candidate_epoch == epoch + 1


@pytest.mark.integration
async def test_recovery_rejects_retained_primary_handoff_once(session_factory, tmp_path):
    from forge.artifacts.filesystem import FilesystemArtifactStore
    from forge.persistence.models.subscription_results import (
        SubscriptionAttemptResult,
        SubscriptionRepairDebit,
    )

    factory, admission = await primary_handoff_case(session_factory, tmp_path)
    async with factory() as work:
        original = await work.session.get(SubscriptionAttemptResult, admission.attempt.attempt_id)
        original_payload, original_digest = original.result_payload, original.result_digest
    recovery = SubscriptionDecisionRecovery(
        factory, FilesystemArtifactStore(tmp_path / "artifacts")
    )
    assert (await recovery.reconcile_all()).applied == 1
    assert (await recovery.reconcile_all()).applied == 0
    async with factory() as work:
        result = await work.session.get(SubscriptionAttemptResult, admission.attempt.attempt_id)
        assert result.disposition == "handoff_repair_queued" and not result.accepted
        assert result.result_payload["decision"] and result.result_digest
        assert (result.result_payload, result.result_digest) == (original_payload, original_digest)
        assert result.application_payload["reason"] == "primary_role_cannot_handoff"
        task = await work.session.get(SubscriptionScheduledTask, admission.task.task_id)
        assert task.state == "queued" and task.repairs == 1
        assert (
            await work.session.scalar(select(func.count()).select_from(SubscriptionRepairDebit))
            == 1
        )
    from forge.application.services.subscription_decisions import SubscriptionDecisionApplication

    replay = await SubscriptionDecisionApplication(factory).reject_invalid_primary_handoff(
        admission.attempt.attempt_id
    )
    assert replay.replayed and replay.disposition == "handoff_repair_queued"
    async with factory() as work:
        assert (
            await work.session.scalar(select(func.count()).select_from(SubscriptionRepairDebit))
            == 1
        )


@pytest.mark.integration
async def test_concurrent_primary_handoff_rejection_debits_one_repair(session_factory, tmp_path):
    import asyncio

    from forge.application.services.subscription_decisions import SubscriptionDecisionApplication
    from forge.persistence.models.subscription_results import SubscriptionRepairDebit

    factory, admission = await primary_handoff_case(session_factory, tmp_path)
    application = SubscriptionDecisionApplication(factory)
    outcomes = await asyncio.gather(
        application.reject_invalid_primary_handoff(admission.attempt.attempt_id),
        application.reject_invalid_primary_handoff(admission.attempt.attempt_id),
    )
    assert {outcome.replayed for outcome in outcomes} == {False, True}
    assert {outcome.disposition for outcome in outcomes} == {"handoff_repair_queued"}
    async with factory() as work:
        scheduled = await work.session.get(SubscriptionScheduledTask, admission.task.task_id)
        assert scheduled.repairs == 1
        assert (
            await work.session.scalar(select(func.count()).select_from(SubscriptionRepairDebit))
            == 1
        )


@pytest.mark.integration
async def test_primary_handoff_exhausted_repairs_stays_terminal(session_factory, tmp_path):
    from forge.application.services.subscription_decisions import SubscriptionDecisionApplication
    from forge.persistence.models.subscription_results import SubscriptionAttemptResult

    factory, admission = await primary_handoff_case(session_factory, tmp_path, repairs=0)
    outcome = await SubscriptionDecisionApplication(factory).reject_invalid_primary_handoff(
        admission.attempt.attempt_id
    )
    assert outcome.disposition == "handoff_rejected"
    async with factory() as work:
        task = await work.session.get(SubscriptionScheduledTask, admission.task.task_id)
        result = await work.session.get(SubscriptionAttemptResult, admission.attempt.attempt_id)
        assert task.state == "terminal" and task.repairs == 0
        assert result.application_payload["reason"] == "primary_role_cannot_handoff"


@pytest.mark.integration
@pytest.mark.parametrize(
    "mutation", ["pause", "cancel", "live_launch", "active_fence", "effect", "stale_task"]
)
async def test_invalid_primary_handoff_recovery_defers_conflicting_state(
    session_factory, tmp_path, mutation
):
    from uuid import uuid4

    from forge.artifacts.filesystem import FilesystemArtifactStore
    from forge.persistence.models import Run
    from forge.persistence.models.scheduling import SubscriptionScheduledEffect
    from forge.persistence.models.subscription import SubscriptionClientLaunch
    from forge.persistence.models.subscription_handoff import SubscriptionHandoffFence
    from forge.persistence.models.subscription_results import SubscriptionAttemptResult

    factory, admission = await primary_handoff_case(session_factory, tmp_path)
    async with factory() as work:
        if mutation == "pause":
            run = await work.runs.get_for_update(admission.task.run_id)
            await work.runs.pause(run.id, run.version, "run.paused", {})
        elif mutation == "cancel":
            run = await work.session.get(Run, admission.task.run_id)
            run.state = RunState.CANCELLED.value
        elif mutation == "live_launch":
            launch = await work.session.scalar(
                select(SubscriptionClientLaunch).where(
                    SubscriptionClientLaunch.attempt_id == admission.attempt.attempt_id
                )
            )
            launch.state = "uncertain"
        elif mutation == "stale_task":
            from forge.persistence.models.subscription import SubscriptionTask

            task = await work.session.get(SubscriptionTask, admission.task.task_id)
            task.version += 1
        elif mutation == "active_fence":
            scheduled = await work.session.get(SubscriptionScheduledTask, admission.task.task_id)
            result = await work.session.get(SubscriptionAttemptResult, admission.attempt.attempt_id)
            work.session.add(
                SubscriptionHandoffFence(
                    worktree_id=scheduled.worktree_id,
                    run_id=admission.task.run_id,
                    attempt_id=admission.attempt.attempt_id,
                    token=uuid4(),
                    result_digest=result.result_digest,
                    expires_at=datetime.now(UTC) + timedelta(minutes=1),
                )
            )
        else:
            scheduled = await work.session.get(SubscriptionScheduledTask, admission.task.task_id)
            work.session.add(
                SubscriptionScheduledEffect(
                    id=uuid4(),
                    run_id=admission.task.run_id,
                    task_id=admission.task.task_id,
                    lease_owner=scheduled.lease_owner,
                    lease_generation=scheduled.lease_generation,
                    candidate_epoch=admission.candidate_epoch,
                    state="admitted",
                )
            )
        await work.commit()
    recovery = SubscriptionDecisionRecovery(
        factory, FilesystemArtifactStore(tmp_path / "artifacts")
    )
    report = await recovery.reconcile_all()
    assert report.applied == 0
    assert report.deferred == (0 if mutation == "cancel" else 1)
    async with factory() as work:
        result = await work.session.get(SubscriptionAttemptResult, admission.attempt.attempt_id)
        task = await work.session.get(SubscriptionScheduledTask, admission.task.task_id)
        assert result.disposition == "decision_pending" and result.application_payload is None
        assert task.repairs == 0


@pytest.mark.integration
async def test_recovery_publishes_plan_after_original_lease_expires(session_factory, tmp_path):
    factory, store, evidence, _, _ = await proposal_case(session_factory, tmp_path)
    async with factory() as work:
        row = await work.session.get(SubscriptionScheduledTask, evidence.producer.task_id)
        row.lease_expires_at = datetime.now(UTC) - timedelta(hours=1)
        await work.commit()
    recovery = SubscriptionDecisionRecovery(factory, store)
    report = await recovery.reconcile_all()
    assert (report.applied, report.deferred, report.unsupported) == (1, 0, 0)
    assert (await recovery.reconcile_all()).applied == 0
    async with factory() as work:
        run = await work.runs.get(evidence.producer.run_id)
        assert run.state is RunState.AWAITING_PLAN_APPROVAL
        assert await work.session.scalar(select(func.count()).select_from(SubscriptionAttempt)) == 1
        assert (await work.subscription_budget.usage(run.id)).consumed.provider_attempts == 1


@pytest.mark.integration
async def test_direct_reconcile_and_operator_retry_race_leave_one_applied_plan(
    session_factory, tmp_path
):
    import asyncio

    from forge.application.services.subscription_plan_gate import SubscriptionPlanGateService
    from forge.persistence.models.subscription_recovery import SubscriptionApplicationDiagnostic
    from forge.persistence.queries.dashboard import DashboardQuery
    from forge.persistence.repositories.subscription_recovery import RecoveryConflict

    factory, store, evidence, _, _ = await proposal_case(session_factory, tmp_path)
    async with factory() as work:
        work.session.add(
            SubscriptionRecoveryWorker(
                worker_id=f"racing-{uuid4()}",
                contract_version=1,
                observed_at=datetime.now(UTC),
            )
        )
        work.session.add(
            SubscriptionApplicationDiagnostic(
                attempt_id=evidence.producer.attempt_id,
                run_id=evidence.producer.run_id,
                task_id=evidence.producer.task_id,
                classification="temporary",
                reason_code="application_infrastructure",
                resolution="attention",
                failed_applications=4,
                first_failure_at=datetime.now(UTC),
                last_failure_at=datetime.now(UTC),
            )
        )
        await work.commit()
    actor = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())
    service = SubscriptionRecoveryService(factory)
    preview = await service.preview(
        run_id=evidence.producer.run_id,
        task_id=evidence.producer.task_id,
        attempt_id=evidence.producer.attempt_id,
        actor=actor,
        request=RecoveryPreviewRequest(action=RecoveryAction.RETRY_APPLICATION),
    )
    assert preview.eligible

    async def apply_operator():
        try:
            return await service.apply(
                run_id=evidence.producer.run_id,
                task_id=evidence.producer.task_id,
                attempt_id=evidence.producer.attempt_id,
                actor=actor,
                idempotency_key="racing-retry",
                request=RecoveryApplyRequest(
                    action=preview.action,
                    preview_token=preview.preview_token,
                    reason="Retry the same result",
                ),
            )
        except RecoveryConflict:
            return None

    direct, periodic, _ = await asyncio.gather(
        SubscriptionPlanGateService(store, factory).request_settled(evidence.producer.attempt_id),
        SubscriptionDecisionRecovery(factory, store).reconcile_all(),
        apply_operator(),
    )
    assert direct.evidence_digest
    assert periodic.deferred == 0
    async with factory() as work:
        run = await work.runs.get(evidence.producer.run_id)
        assert run.state is RunState.AWAITING_PLAN_APPROVAL
        assert (await work.subscription_budget.usage(run.id)).consumed.provider_attempts == 1
    view = await DashboardQuery(session_factory).run_projection(evidence.producer.run_id)
    assert view is not None and not view["subscription_recovery_attention"]


@pytest.mark.integration
async def test_paused_source_is_deferred_without_weakening_gate(session_factory, tmp_path):
    factory, store, evidence, _, _ = await proposal_case(session_factory, tmp_path)
    async with factory() as work:
        run = await work.runs.get_for_update(evidence.producer.run_id)
        await work.runs.pause(run.id, run.version, "run.paused", {})
        await work.commit()
    report = await SubscriptionDecisionRecovery(factory, store).reconcile_all()
    assert (report.applied, report.deferred, report.unsupported) == (0, 1, 0)
    assert (await SubscriptionDecisionRecovery(factory, store).reconcile_all()).deferred == 0
    async with factory() as work:
        run = await work.runs.get(evidence.producer.run_id)
        assert run.state is RunState.PAUSED and run.pending_gate is None
        assert len(await work.subscription_decisions.pending_applications(None, 100)) == 1
        assert not await work.subscription_recovery.due(evidence.producer.attempt_id)
        diagnostic = await work.session.get(
            SubscriptionApplicationDiagnostic, evidence.producer.attempt_id
        )
        assert diagnostic.resolution == "waiting" and diagnostic.reason_code == "run_controlled"
        assert diagnostic.failed_applications == 1
        await work.runs.resume(run.id, run.version, "run.resumed", {})
        await work.commit()
    async with factory() as work:
        assert await work.subscription_recovery.due(evidence.producer.attempt_id)
    assert (await SubscriptionDecisionRecovery(factory, store).reconcile_all()).applied == 1


@pytest.mark.integration
async def test_plan_settled_during_pause_keeps_legal_decision_and_usage(session_factory, tmp_path):
    from forge.persistence.models.subscription_results import SubscriptionAttemptResult

    factory, admission = await proposal_case(session_factory, tmp_path, pause_before_settle=True)
    async with factory() as work:
        result = await work.session.get(SubscriptionAttemptResult, admission.attempt.attempt_id)
        assert result.result_payload["effective_failure"] is None
        assert result.result_payload["decision"]["type"] == "PlanOutput"
        run = await work.runs.get(admission.attempt.run_id)
        assert run.state is RunState.PAUSED
        assert (await work.subscription_budget.usage(run.id)).consumed.provider_attempts == 1
        await work.runs.resume(run.id, run.version, "run.resumed", {})
        await work.commit()
    async with factory() as work:
        run = await work.runs.get(admission.attempt.run_id)
        assert (await work.subscription_budget.usage(run.id)).consumed.provider_attempts == 1


@pytest.mark.integration
@pytest.mark.parametrize("kind", ["delegate", "wait"])
async def test_recovery_applies_existing_task_decisions_once(session_factory, tmp_path, kind):
    from forge.artifacts.filesystem import FilesystemArtifactStore
    from forge.persistence.models.subscription_results import SubscriptionAttemptResult
    from test_subscription_delegation_application import delegation_case
    from test_subscription_wait_application import settled_wait

    if kind == "delegate":
        factory, admission, _, _ = await delegation_case(session_factory, tmp_path)
    else:
        factory, _, _, admission, _ = await settled_wait(session_factory, tmp_path)
    async with factory() as work:
        count = await work.session.scalar(select(func.count()).select_from(SubscriptionAttempt))
    recovery = SubscriptionDecisionRecovery(
        factory, FilesystemArtifactStore(tmp_path / "artifacts")
    )
    report = await recovery.reconcile_all()
    assert (report.applied, report.deferred, report.unsupported) == (1, 0, 0)
    assert (await recovery.reconcile_all()).applied == 0
    async with factory() as work:
        result = await work.session.get(SubscriptionAttemptResult, admission.attempt.attempt_id)
        assert result.accepted and result.disposition == (
            "delegated" if kind == "delegate" else "waiting"
        )
        assert (
            await work.session.scalar(select(func.count()).select_from(SubscriptionAttempt))
            == count
        )


@pytest.mark.integration
async def test_pending_decision_scan_uses_keyset_pages(session_factory, tmp_path):
    from forge.application.ports.subscription_decisions import PendingDecisionKind

    first_root, second_root = tmp_path / "one", tmp_path / "two"
    first_root.mkdir()
    second_root.mkdir()
    factory, store, first, _, _ = await proposal_case(session_factory, first_root)
    _, _, second, _, _ = await proposal_case(session_factory, second_root)
    expected = sorted((first.producer.attempt_id, second.producer.attempt_id))
    async with factory() as work:
        first_page = await work.subscription_decisions.pending_applications(None, 1)
        second_page = await work.subscription_decisions.pending_applications(
            first_page[0].attempt_id, 1
        )
        assert [first_page[0].attempt_id, second_page[0].attempt_id] == expected
        assert first_page[0].kind is second_page[0].kind is PendingDecisionKind.PLAN
        assert await work.subscription_decisions.pending_applications(expected[-1], 1) == ()
    report = await SubscriptionDecisionRecovery(factory, store, page_size=1).reconcile_all()
    assert (report.applied, report.deferred, report.unsupported) == (2, 0, 0)


@pytest.mark.integration
async def test_periodic_retry_applies_plan_after_storage_recovers(
    session_factory, tmp_path, monkeypatch
):
    import asyncio
    from types import SimpleNamespace

    from forge.worker.main import _poll_decisions

    factory, store, evidence, _, _ = await proposal_case(session_factory, tmp_path)
    recovery = SubscriptionDecisionRecovery(factory, store)
    original_put = store.put_bytes

    async def unavailable(*args, **kwargs):
        raise OSError("temporary storage failure")

    monkeypatch.setattr(store, "put_bytes", unavailable)
    assert (await recovery.reconcile_all()).deferred == 1
    from forge.persistence.models.subscription_recovery import SubscriptionApplicationDiagnostic

    async with factory() as work:
        diagnostic = await work.session.get(
            SubscriptionApplicationDiagnostic, evidence.producer.attempt_id
        )
        assert diagnostic.classification == "temporary"
        assert diagnostic.next_retry_at > datetime.now(UTC)
        diagnostic.next_retry_at = datetime.now(UTC) - timedelta(seconds=1)
        await work.commit()
    monkeypatch.setattr(store, "put_bytes", original_put)
    stop = asyncio.Event()

    async def retry():
        report = await recovery.reconcile_all()
        assert report.applied == 1 and report.deferred == 0
        stop.set()
        return report

    await asyncio.wait_for(_poll_decisions(SimpleNamespace(reconcile_all=retry), stop, 0.01), 5)
    async with factory() as work:
        run = await work.runs.get(evidence.producer.run_id)
        assert run.state is RunState.AWAITING_PLAN_APPROVAL
        assert await work.session.scalar(select(func.count()).select_from(SubscriptionAttempt)) == 1
        assert (await work.subscription_budget.usage(run.id)).consumed.provider_attempts == 1


@pytest.mark.integration
async def test_exhausted_planning_application_retries_same_result_after_operator_preview(
    session_factory, tmp_path, monkeypatch
):
    factory, store, evidence, _, _ = await proposal_case(session_factory, tmp_path)
    async with factory() as work:
        work.session.add(
            SubscriptionRecoveryWorker(
                worker_id=f"planning-{uuid4()}", contract_version=1, observed_at=datetime.now(UTC)
            )
        )
        await work.commit()
    original_put = store.put_bytes

    async def unavailable(*args, **kwargs):
        raise OSError("temporary artifact outage")

    monkeypatch.setattr(store, "put_bytes", unavailable)
    recovery = SubscriptionDecisionRecovery(factory, store)
    for index in range(4):
        assert (await recovery.reconcile_all()).deferred == 1
        if index < 3:
            async with factory() as work:
                from forge.persistence.models.subscription_recovery import (
                    SubscriptionApplicationDiagnostic,
                )

                diagnostic = await work.session.get(
                    SubscriptionApplicationDiagnostic, evidence.producer.attempt_id
                )
                diagnostic.next_retry_at = datetime.now(UTC) - timedelta(seconds=1)
                await work.commit()
    assert (await recovery.reconcile_all()).deferred == 0
    actor = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())
    service = SubscriptionRecoveryService(factory)
    preview = await service.preview(
        run_id=evidence.producer.run_id,
        task_id=evidence.producer.task_id,
        attempt_id=evidence.producer.attempt_id,
        actor=actor,
        request=RecoveryPreviewRequest(action=RecoveryAction.RETRY_APPLICATION),
    )
    assert preview.eligible, preview.reason_code
    assert preview.budget_impact.provider_attempts == preview.budget_impact.repair_units == 0
    await service.apply(
        run_id=evidence.producer.run_id,
        task_id=evidence.producer.task_id,
        attempt_id=evidence.producer.attempt_id,
        actor=actor,
        idempotency_key="same-plan",
        request=RecoveryApplyRequest(
            action=preview.action,
            preview_token=preview.preview_token,
            reason="Artifact storage recovered",
        ),
    )
    monkeypatch.setattr(store, "put_bytes", original_put)
    assert (await recovery.reconcile_all()).applied == 1
    async with factory() as work:
        run = await work.runs.get(evidence.producer.run_id)
        assert run.state is RunState.AWAITING_PLAN_APPROVAL
        assert await work.session.scalar(select(func.count()).select_from(SubscriptionAttempt)) == 1
