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
        work.session.add(SubscriptionRecoveryWorker(
            worker_id=f"racing-{uuid4()}", contract_version=1,
            observed_at=datetime.now(UTC),
        ))
        work.session.add(SubscriptionApplicationDiagnostic(
            attempt_id=evidence.producer.attempt_id, run_id=evidence.producer.run_id,
            task_id=evidence.producer.task_id, classification="temporary",
            reason_code="application_infrastructure", resolution="attention",
            failed_applications=4, first_failure_at=datetime.now(UTC),
            last_failure_at=datetime.now(UTC),
        ))
        await work.commit()
    actor = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())
    service = SubscriptionRecoveryService(factory)
    preview = await service.preview(
        run_id=evidence.producer.run_id, task_id=evidence.producer.task_id,
        attempt_id=evidence.producer.attempt_id, actor=actor,
        request=RecoveryPreviewRequest(action=RecoveryAction.RETRY_APPLICATION),
    )
    assert preview.eligible

    async def apply_operator():
        try:
            return await service.apply(
                run_id=evidence.producer.run_id, task_id=evidence.producer.task_id,
                attempt_id=evidence.producer.attempt_id, actor=actor,
                idempotency_key="racing-retry", request=RecoveryApplyRequest(
                    action=preview.action, preview_token=preview.preview_token,
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
async def test_plan_settled_during_pause_keeps_legal_decision_and_usage(
    session_factory, tmp_path
):
    from forge.persistence.models.subscription_results import SubscriptionAttemptResult
    factory, admission = await proposal_case(
        session_factory, tmp_path, pause_before_settle=True
    )
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
        work.session.add(SubscriptionRecoveryWorker(
            worker_id=f"planning-{uuid4()}", contract_version=1, observed_at=datetime.now(UTC)
        ))
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
        run_id=evidence.producer.run_id, task_id=evidence.producer.task_id,
        attempt_id=evidence.producer.attempt_id, actor=actor,
        request=RecoveryPreviewRequest(action=RecoveryAction.RETRY_APPLICATION),
    )
    assert preview.eligible, preview.reason_code
    assert preview.budget_impact.provider_attempts == preview.budget_impact.repair_units == 0
    await service.apply(
        run_id=evidence.producer.run_id, task_id=evidence.producer.task_id,
        attempt_id=evidence.producer.attempt_id, actor=actor, idempotency_key="same-plan",
        request=RecoveryApplyRequest(
            action=preview.action, preview_token=preview.preview_token,
            reason="Artifact storage recovered",
        ),
    )
    monkeypatch.setattr(store, "put_bytes", original_put)
    assert (await recovery.reconcile_all()).applied == 1
    async with factory() as work:
        run = await work.runs.get(evidence.producer.run_id)
        assert run.state is RunState.AWAITING_PLAN_APPROVAL
        assert await work.session.scalar(select(func.count()).select_from(SubscriptionAttempt)) == 1
