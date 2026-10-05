"""Recovery resumes final acceptance through verification and queue admission."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from forge.application.ports.worktrees import GitWorkingTreeSnapshot
from forge.application.services.auth import AuthenticatedActor
from forge.application.services.subscription_acceptance import SubscriptionAcceptanceInspection
from forge.application.services.subscription_acceptance_dispatch import (
    SubscriptionAcceptanceDispatch,
)
from forge.application.services.subscription_acceptance_receipts import (
    SubscriptionAcceptanceReceiptVerification,
)
from forge.application.services.subscription_decision_recovery import SubscriptionDecisionRecovery
from forge.application.services.subscription_recovery import SubscriptionRecoveryService
from forge.artifacts.filesystem import FilesystemArtifactStore
from forge.domain.subscription_recovery import (
    RecoveryAction,
    RecoveryApplyRequest,
    RecoveryPreviewRequest,
)
from forge.persistence.models.execution import RunEvent
from forge.persistence.models.subscription import SubscriptionTask
from forge.persistence.models.subscription_recovery import (
    SubscriptionApplicationDiagnostic,
    SubscriptionRecoveryWorker,
)
from forge.persistence.models.subscription_results import SubscriptionAttemptResult
from sqlalchemy import select
from test_scheduler_acceptance import (
    _remove_disposable_subscription_rows,  # noqa: F401 - pytest fixture
)
from test_subscription_acceptance_dispatch import dispatch_case
from test_subscription_acceptance_preparation import acceptance_case


@pytest.mark.integration
async def test_first_acceptance_preparation_records_io_failure_and_retries_same_source(
    session_factory, tmp_path, monkeypatch
):
    factory, primary, _ = await acceptance_case(session_factory, tmp_path)
    attempt_id = primary.attempt.attempt_id
    async with factory() as work:
        result = await work.session.get(SubscriptionAttemptResult, attempt_id)
        original = result.result_digest, deepcopy(result.result_payload)
        usage = await work.subscription_budget.usage(primary.task.run_id)
        observed = await work.subscription_recovery.due_version(attempt_id)
        assert observed is not None
    calls = 0

    async def snapshot(proposal):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("temporary Git snapshot outage")
        return GitWorkingTreeSnapshot(
            head_sha=proposal.review.candidate.head_sha,
            base_sha=proposal.worktree.base_sha,
            files=(),
            changed_paths=(),
        )

    async def unavailable_receipt(_attempt_id):
        return None

    store = FilesystemArtifactStore(tmp_path / "artifacts")
    receipts = SubscriptionAcceptanceReceiptVerification(factory, store, SimpleNamespace())
    monkeypatch.setattr(receipts, "verify", unavailable_receipt)
    service = SubscriptionAcceptanceDispatch(factory, store, snapshot, receipts)
    recovery = SubscriptionDecisionRecovery(factory, store, acceptance=service)
    assert (await recovery.reconcile_all()).deferred == 1
    async with factory() as work:
        result = await work.session.get(SubscriptionAttemptResult, attempt_id)
        task = await work.session.get(SubscriptionTask, primary.task.task_id)
        diagnostic = await work.session.get(SubscriptionApplicationDiagnostic, attempt_id)
        assert result.accepted and result.disposition == "acceptance_prepared"
        assert task.version == observed + 1
        assert diagnostic.classification == "temporary" and diagnostic.failed_applications == 1
        assert diagnostic.resolution == "scheduled"
        diagnostic.next_retry_at = datetime.now(UTC) - timedelta(seconds=1)
        await work.commit()
    assert (await recovery.reconcile_all()).deferred == 1
    assert calls == 2
    async with factory() as work:
        result = await work.session.get(SubscriptionAttemptResult, attempt_id)
        diagnostic = await work.session.get(SubscriptionApplicationDiagnostic, attempt_id)
        assert diagnostic.classification == "prerequisite" and diagnostic.failed_applications == 2
        assert diagnostic.reason_code == "acceptance_receipt_unavailable"
        assert (result.result_digest, result.result_payload) == original
        assert await work.subscription_budget.usage(primary.task.run_id) == usage


@pytest.mark.integration
@pytest.mark.parametrize("phase", ["prepared", "observed", "verified"])
async def test_recovery_queues_acceptance_validation_and_leaves_pending_scan(
    session_factory, tmp_path, phase
):
    factory, proposal, service, _ = await dispatch_case(session_factory, tmp_path)
    if phase != "prepared":
        await SubscriptionAcceptanceInspection(factory, service._snapshot).inspect(
            proposal.attempt_id
        )
    if phase == "verified":
        assert await service._receipts.verify(proposal.attempt_id) is not None
    async with factory() as work:
        pending = await work.subscription_decisions.pending_applications(None, 100)
        assert [(item.attempt_id, item.kind.value) for item in pending] == [
            (proposal.attempt_id, "final_acceptance")
        ]
    unconfigured = await SubscriptionDecisionRecovery(factory, service._store).reconcile_all()
    assert (unconfigured.applied, unconfigured.deferred, unconfigured.unsupported) == (0, 0, 1)
    recovery = SubscriptionDecisionRecovery(factory, service._store, acceptance=service)
    report = await recovery.reconcile_all()
    assert (report.applied, report.deferred, report.unsupported) == (1, 0, 0)
    assert (await recovery.reconcile_all()).applied == 0
    async with factory() as work:
        assert await work.subscription_decisions.pending_applications(None, 100) == ()


@pytest.mark.integration
async def test_acceptance_prerequisite_can_retry_same_prepared_result(
    session_factory, tmp_path, monkeypatch
):
    factory, proposal, service, _ = await dispatch_case(session_factory, tmp_path)
    original_verify = service._receipts.verify

    async def unavailable(_attempt_id):
        return None

    monkeypatch.setattr(service._receipts, "verify", unavailable)
    recovery = SubscriptionDecisionRecovery(factory, service._store, acceptance=service)
    for index in range(4):
        # A new reconciler instance reads the persisted failure count after a
        # worker restart; scans alone cannot reset the bounded schedule.
        recovery = SubscriptionDecisionRecovery(factory, service._store, acceptance=service)
        assert (await recovery.reconcile_all()).deferred == 1
        if index < 3:
            async with factory() as work:
                diagnostic = await work.session.get(
                    SubscriptionApplicationDiagnostic, proposal.attempt_id
                )
                assert diagnostic.resolution == "scheduled"
                diagnostic.next_retry_at = datetime.now(UTC) - timedelta(seconds=1)
                await work.commit()
    assert (await recovery.reconcile_all()).deferred == 0
    async with factory() as work:
        diagnostic = await work.session.get(SubscriptionApplicationDiagnostic, proposal.attempt_id)
        assert diagnostic.classification == "prerequisite"
        assert diagnostic.reason_code == "acceptance_receipt_unavailable"
        assert diagnostic.resolution == "attention" and diagnostic.failed_applications == 4
        events = (
            await work.session.scalars(
                select(RunEvent)
                .where(
                    RunEvent.run_id == proposal.decision.run_id,
                    RunEvent.event_type == "run.subscription_recovery_attention_changed",
                )
                .order_by(RunEvent.sequence)
            )
        ).all()
        assert len(events) == 1
        assert events[0].payload == {
            "task_id": str(proposal.decision.task_id),
            "attempt_id": str(proposal.attempt_id),
            "attention": True,
            "reason_code": "acceptance_receipt_unavailable",
        }
        work.session.add(
            SubscriptionRecoveryWorker(
                worker_id=f"acceptance-{uuid4()}",
                contract_version=1,
                observed_at=datetime.now(UTC),
            )
        )
        await work.commit()
    from forge.persistence.queries.dashboard import DashboardQuery
    from forge.persistence.queries.subscription_tasks import SubscriptionTaskQuery

    run_view = await DashboardQuery(session_factory).run_projection(proposal.decision.run_id)
    task_view = await SubscriptionTaskQuery(session_factory).tasks(proposal.decision.run_id)
    assert run_view is not None and run_view["subscription_recovery_attention"]
    assert task_view is not None and any(
        row["task_id"] == proposal.decision.task_id and row["recovery_attention"]
        for row in task_view["tasks"]
    )
    actor = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())
    operator = SubscriptionRecoveryService(factory)
    preview = await operator.preview(
        run_id=proposal.decision.run_id,
        task_id=proposal.decision.task_id,
        attempt_id=proposal.attempt_id,
        actor=actor,
        request=RecoveryPreviewRequest(action=RecoveryAction.RETRY_APPLICATION),
    )
    assert preview.eligible, preview.reason_code
    await operator.apply(
        run_id=proposal.decision.run_id,
        task_id=proposal.decision.task_id,
        attempt_id=proposal.attempt_id,
        actor=actor,
        idempotency_key="acceptance-prerequisite",
        request=RecoveryApplyRequest(
            action=preview.action,
            preview_token=preview.preview_token,
            reason="Receipt verifier recovered",
        ),
    )
    async with factory() as work:
        events = (
            await work.session.scalars(
                select(RunEvent)
                .where(
                    RunEvent.run_id == proposal.decision.run_id,
                    RunEvent.event_type == "run.subscription_recovery_attention_changed",
                )
                .order_by(RunEvent.sequence)
            )
        ).all()
        assert len(events) == 2
        assert events[1].payload["attention"] is False
        assert events[1].sequence > events[0].sequence
    monkeypatch.setattr(service._receipts, "verify", original_verify)
    assert (await recovery.reconcile_all()).applied == 1


@pytest.mark.integration
async def test_acceptance_prerequisite_recovers_automatically_from_saved_result(
    session_factory, tmp_path, monkeypatch
):
    factory, proposal, service, _ = await dispatch_case(session_factory, tmp_path)
    original_verify = service._receipts.verify
    calls = 0

    async def one_unavailable(attempt_id):
        nonlocal calls
        calls += 1
        if calls == 1:
            return None
        return await original_verify(attempt_id)

    monkeypatch.setattr(service._receipts, "verify", one_unavailable)
    recovery = SubscriptionDecisionRecovery(factory, service._store, acceptance=service)
    assert (await recovery.reconcile_all()).deferred == 1
    async with factory() as work:
        diagnostic = await work.session.get(SubscriptionApplicationDiagnostic, proposal.attempt_id)
        assert diagnostic.resolution == "scheduled" and diagnostic.next_retry_at > datetime.now(UTC)
        diagnostic.next_retry_at = datetime.now(UTC) - timedelta(seconds=1)
        await work.commit()
    assert (await recovery.reconcile_all()).applied == 1
    async with factory() as work:
        diagnostic = await work.session.get(SubscriptionApplicationDiagnostic, proposal.attempt_id)
        assert diagnostic.resolution == "applied"
        assert (
            await work.subscription_budget.usage(proposal.decision.run_id)
        ).consumed.repairs == 0
