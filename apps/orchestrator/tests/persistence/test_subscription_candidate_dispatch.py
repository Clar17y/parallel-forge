"""Restart recovery discovers both pending and durably prepared selections."""

from dataclasses import replace
from uuid import uuid4

import pytest
from forge.application.ports.worktrees import GitWorkingTreeSnapshot
from forge.application.services.auth import AuthenticatedActor
from forge.application.services.subscription_candidate import (
    SubscriptionCandidateApplication,
    SubscriptionCandidateInspection,
)
from forge.application.services.subscription_decision_recovery import SubscriptionDecisionRecovery
from forge.application.services.subscription_decisions import SubscriptionDecisionApplication
from forge.application.services.subscription_recovery import SubscriptionRecoveryService
from forge.domain.subscription_recovery import (
    RecoveryAction,
    RecoveryApplyRequest,
    RecoveryPreviewRequest,
)
from forge.persistence.models.subscription import SubscriptionAttempt
from forge.persistence.models.subscription_recovery import SubscriptionRecoveryWorker
from forge.persistence.models.subscription_results import SubscriptionAttemptResult
from sqlalchemy import func, select
from test_scheduler_acceptance import (
    _remove_disposable_subscription_rows,  # noqa: F401
    _route,
)
from test_subscription_review_selection import selection_case


@pytest.mark.integration
@pytest.mark.parametrize("phase", ["pending", "prepared", "observed"])
async def test_recovery_resumes_review_selection_from_every_durable_phase(
    session_factory, tmp_path, phase
):
    snapshot = GitWorkingTreeSnapshot(head_sha="b" * 40, base_sha="a" * 40, files=(), changed_paths=())
    factory, primary, _ = await selection_case(
        session_factory, tmp_path, tree_digest=snapshot.candidate_tree_digest
    )

    async def capture(proposal):
        return snapshot

    if phase == "prepared":
        await SubscriptionDecisionApplication(factory).prepare_review_selection(primary.attempt.attempt_id)
    elif phase == "observed":
        await SubscriptionCandidateInspection(factory, capture).inspect(primary.attempt.attempt_id)
    async with factory() as work:
        count = await work.session.scalar(select(func.count()).select_from(SubscriptionAttempt))
        pending = await work.subscription_decisions.pending_applications(None, 100)
        assert [(item.attempt_id, item.kind.value) for item in pending] == [
            (primary.attempt.attempt_id, "review_selection")
        ]
    unsupported = await SubscriptionDecisionRecovery(factory, object()).reconcile_all()
    assert (unsupported.applied, unsupported.deferred, unsupported.unsupported) == (0, 0, 1)
    recovery = SubscriptionDecisionRecovery(
        factory, object(), page_size=1, candidates=SubscriptionCandidateApplication(factory, capture)
    )
    report = await recovery.reconcile_all()
    assert (report.applied, report.deferred, report.unsupported) == (1, 0, 0)
    assert (await recovery.reconcile_all()).applied == 0
    async with factory() as work:
        result = await work.session.get(SubscriptionAttemptResult, primary.attempt.attempt_id)
        assert result.disposition == "review_selected"
        assert await work.session.scalar(select(func.count()).select_from(SubscriptionAttempt)) == count


@pytest.mark.integration
async def test_candidate_recovery_defers_failed_git_and_resumes_after_restart(session_factory, tmp_path):
    snapshot = GitWorkingTreeSnapshot(head_sha="b" * 40, base_sha="a" * 40, files=(), changed_paths=())
    factory, primary, _ = await selection_case(
        session_factory, tmp_path, tree_digest=snapshot.candidate_tree_digest
    )

    async def unavailable(proposal):
        raise OSError("snapshot unavailable")

    recovery = SubscriptionDecisionRecovery(
        factory, object(), candidates=SubscriptionCandidateApplication(factory, unavailable)
    )
    report = await recovery.reconcile_all()
    assert (report.applied, report.deferred, report.unsupported) == (0, 1, 0)
    async with factory() as work:
        result = await work.session.get(SubscriptionAttemptResult, primary.attempt.attempt_id)
        assert result.accepted and result.disposition == "candidate_prepared"

    async def capture(proposal):
        return snapshot

    from datetime import UTC, datetime, timedelta

    from forge.persistence.models.subscription_recovery import SubscriptionApplicationDiagnostic

    async with factory() as work:
        diagnostic = await work.session.get(
            SubscriptionApplicationDiagnostic, primary.attempt.attempt_id
        )
        diagnostic.next_retry_at = datetime.now(UTC) - timedelta(seconds=1)
        await work.commit()

    restarted = SubscriptionDecisionRecovery(
        factory, object(), candidates=SubscriptionCandidateApplication(factory, capture)
    )
    report = await restarted.reconcile_all()
    assert (report.applied, report.deferred, report.unsupported) == (1, 0, 0)


@pytest.mark.integration
async def test_prepared_candidate_exhaustion_retries_remaining_application_stage(
    session_factory, tmp_path
):
    from datetime import UTC, datetime, timedelta

    from forge.persistence.models.subscription_recovery import SubscriptionApplicationDiagnostic

    snapshot = GitWorkingTreeSnapshot(head_sha="b" * 40, base_sha="a" * 40, files=(), changed_paths=())
    factory, primary, _ = await selection_case(
        session_factory, tmp_path, tree_digest=snapshot.candidate_tree_digest
    )
    async with factory() as work:
        work.session.add(SubscriptionRecoveryWorker(
            worker_id=f"candidate-{uuid4()}", contract_version=1, observed_at=datetime.now(UTC)
        ))
        await work.commit()
    async def unavailable(proposal):
        raise OSError("temporary snapshot failure")
    recovery = SubscriptionDecisionRecovery(
        factory, object(), candidates=SubscriptionCandidateApplication(factory, unavailable)
    )
    for index in range(4):
        assert (await recovery.reconcile_all()).deferred == 1
        if index < 3:
            async with factory() as work:
                diagnostic = await work.session.get(
                    SubscriptionApplicationDiagnostic, primary.attempt.attempt_id
                )
                diagnostic.next_retry_at = datetime.now(UTC) - timedelta(seconds=1)
                await work.commit()
    async with factory() as work:
        result = await work.session.get(SubscriptionAttemptResult, primary.attempt.attempt_id)
        diagnostic = await work.session.get(
            SubscriptionApplicationDiagnostic, primary.attempt.attempt_id
        )
        assert result.disposition == "candidate_prepared" and diagnostic.resolution == "attention"
    actor = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())
    service = SubscriptionRecoveryService(factory)
    preview = await service.preview(
        run_id=primary.attempt.run_id, task_id=primary.attempt.task_id,
        attempt_id=primary.attempt.attempt_id, actor=actor,
        request=RecoveryPreviewRequest(action=RecoveryAction.RETRY_APPLICATION),
    )
    assert preview.eligible, preview.reason_code
    await service.apply(
        run_id=primary.attempt.run_id, task_id=primary.attempt.task_id,
        attempt_id=primary.attempt.attempt_id, actor=actor, idempotency_key="candidate-stage",
        request=RecoveryApplyRequest(
            action=preview.action, preview_token=preview.preview_token,
            reason="Snapshot service recovered",
        ),
    )
    async def capture(proposal):
        return snapshot
    resumed = SubscriptionDecisionRecovery(
        factory, object(), candidates=SubscriptionCandidateApplication(factory, capture)
    )
    assert (await resumed.reconcile_all()).applied == 1
    async with factory() as work:
        result = await work.session.get(SubscriptionAttemptResult, primary.attempt.attempt_id)
        assert result.disposition == "review_selected"


@pytest.mark.integration
async def test_candidate_recovery_applies_bounded_route_rejection_without_git(
    session_factory, tmp_path
):
    factory, primary, _ = await selection_case(
        session_factory, tmp_path, review_required=True,
        selection_mutate=lambda decision, admission: replace(
            decision, reviewer_route=_route("unapproved")
        ),
    )

    async def capture(proposal):
        raise AssertionError("an unapproved route must not inspect Git")

    recovery = SubscriptionDecisionRecovery(
        factory, object(), candidates=SubscriptionCandidateApplication(factory, capture)
    )
    report = await recovery.reconcile_all()
    assert (report.applied, report.deferred, report.unsupported) == (1, 0, 0)
    assert (await recovery.reconcile_all()).applied == 0
    async with factory() as work:
        result = await work.session.get(SubscriptionAttemptResult, primary.attempt.attempt_id)
        assert result.disposition == "decision_repair_queued" and not result.accepted
