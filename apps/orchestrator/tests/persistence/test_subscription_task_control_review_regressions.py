"""Independent-review regressions for path authority and stopped capacity."""

import asyncio
import os
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from forge.application.services.subscription_decision_recovery import SubscriptionDecisionRecovery
from forge.application.services.subscription_decisions import SubscriptionDecisionApplication
from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
from forge.domain.scheduling import SchedulerCapacityPolicy
from forge.persistence.models.api import ApiMutation
from forge.persistence.models.run import Run
from forge.persistence.models.scheduling import SubscriptionScheduledTask
from forge.persistence.models.subscription import SubscriptionClientLaunch, SubscriptionTask
from forge.persistence.models.subscription_recovery import SubscriptionApplicationDiagnostic
from forge.persistence.models.subscription_results import SubscriptionAttemptResult
from forge.persistence.models.subscription_task_stops import SubscriptionTaskStop
from sqlalchemy import select
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_delegation_application import delegation_case
from test_subscription_resume_boundaries import prepared_case
from test_subscription_scope_application import scope_case
from test_subscription_task_control_recovery import control
from test_subscription_usage import _reservation


@pytest.mark.integration
@pytest.mark.parametrize("prior", ["clean", "future", "legacy_attention"])
async def test_pending_resume_retries_exact_result_and_ignores_stale_control_failure(
    session_factory, tmp_path, prior
):
    factory, _, _, child = await scope_case(session_factory, tmp_path)
    attempt_id = child.attempt.attempt_id
    async with factory() as work:
        result = await work.session.get(SubscriptionAttemptResult, attempt_id)
        original = result.result_digest, deepcopy(result.result_payload)
        usage = await work.subscription_budget.usage(child.task.run_id, child.task.task_id)
        observed = await work.subscription_recovery.due_version(attempt_id)
        assert observed is not None
        scheduled = await work.session.get(SubscriptionScheduledTask, child.task.task_id)
        scheduled.pause_requested = True
        await work.session.flush()
        assert await work.subscription_recovery.due_version(attempt_id) is None
        scheduled.pause_requested = False
        if prior == "future":
            work.session.add(
                SubscriptionApplicationDiagnostic(
                    attempt_id=attempt_id,
                    run_id=child.task.run_id,
                    task_id=child.task.task_id,
                    classification="temporary",
                    reason_code="application_infrastructure",
                    resolution="scheduled",
                    failed_applications=1,
                    first_failure_at=datetime.now(UTC),
                    last_failure_at=datetime.now(UTC),
                    next_retry_at=datetime.now(UTC) + timedelta(hours=1),
                )
            )
        await work.commit()
    pause = await control(factory, child, "pause")
    async with factory() as work:
        if prior == "legacy_attention":
            work.session.add(
                SubscriptionApplicationDiagnostic(
                    attempt_id=attempt_id,
                    run_id=child.task.run_id,
                    task_id=child.task.task_id,
                    classification="unsupported",
                    reason_code="application_invariant",
                    resolution="attention",
                    failed_applications=1,
                    first_failure_at=datetime.now(UTC),
                    last_failure_at=datetime.now(UTC),
                )
            )
        await work.commit()
    async with factory() as work:
        assert await work.subscription_recovery.due_version(attempt_id) is None
        await work.subscription_recovery.record_failure(
            attempt_id,
            classification="unsupported",
            reason_code="application_invariant",
            observed_task_version=observed,
        )
        await work.subscription_recovery.reject_role_violation(
            attempt_id, "stale_role_check", observed_task_version=observed
        )
        await work.commit()
    async with factory() as work:
        diagnostic = await work.session.get(SubscriptionApplicationDiagnostic, attempt_id)
        assert (
            (diagnostic is None)
            if prior == "clean"
            else diagnostic.classification == ("temporary" if prior == "future" else "unsupported")
        )
    assert (await SubscriptionDecisionRecovery(factory, object()).reconcile_all()).applied == 0
    resume = await control(factory, child, "resume", pause_id=pause.receipt_id)
    assert resume.status == "decision_pending"
    async with factory() as work:
        diagnostic = await work.session.get(SubscriptionApplicationDiagnostic, attempt_id)
        if prior != "clean":
            assert diagnostic.resolution == "scheduled"
            assert diagnostic.next_retry_at <= datetime.now(UTC)
        assert await work.subscription_recovery.due_version(attempt_id) == resume.task_version
        await work.subscription_recovery.record_failure(
            attempt_id,
            classification="unsupported",
            reason_code="application_invariant",
            observed_task_version=observed,
        )
        await work.subscription_recovery.reject_role_violation(
            attempt_id, "stale_role_check", observed_task_version=observed
        )
        await work.commit()
    assert (await SubscriptionDecisionRecovery(factory, object()).reconcile_all()).applied == 1
    assert (await SubscriptionDecisionRecovery(factory, object()).reconcile_all()).applied == 0
    async with factory() as work:
        result = await work.session.get(SubscriptionAttemptResult, attempt_id)
        assert (result.result_digest, result.result_payload) == original
        assert result.accepted
        assert await work.subscription_budget.usage(child.task.run_id, child.task.task_id) == usage


@pytest.mark.integration
@pytest.mark.skipif(os.name != "nt", reason="Windows paths are case-insensitive")
async def test_mixed_case_owned_paths_support_all_task_controls(session_factory, tmp_path):
    factory, parent, children, _ = await delegation_case(
        session_factory,
        tmp_path,
        lambda child, _: (replace(child, owned_paths=("Apps/MixedCase.py",)),),
        plan_scope=("apps",),
    )
    assert (
        await SubscriptionDecisionApplication(factory).apply_delegation(parent.attempt.attempt_id)
    ).accepted
    child = SimpleNamespace(task=children[0])
    pause = await control(factory, child, "pause")
    assert pause.status == "paused"
    assert (await control(factory, child, "resume", pause_id=pause.receipt_id)).status == "queued"
    assert (await control(factory, child, "cancel")).status == "cancelled"
    async with factory() as work:
        assert (await work.session.get(SubscriptionTask, child.task.task_id)).state == "terminal"


@pytest.mark.integration
@pytest.mark.parametrize("change", ["settlement", "audit", "launch"])
async def test_capacity_counts_pauses_without_verified_stop_evidence(
    session_factory, tmp_path, change
):
    factory, _, _, child = await scope_case(session_factory, tmp_path)
    pause = await control(factory, child, "pause")
    async with factory() as work:
        assert await work.scheduler._active_count() == 0
        if change == "settlement":
            (await work.session.get(SubscriptionTaskStop, pause.receipt_id)).settlement_digest = (
                "f" * 64
            )
        elif change == "audit":
            mutation = await work.session.get(ApiMutation, pause.receipt_id)
            payload = deepcopy(mutation.response_payload)
            payload["receipt"]["reason"] = "Unverified replacement"
            mutation.response_payload = payload
        else:
            await work.session.delete(
                await work.session.scalar(
                    select(SubscriptionClientLaunch).where(
                        SubscriptionClientLaunch.attempt_id == child.attempt.attempt_id,
                    )
                )
            )
        await work.commit()
    async with factory() as work:
        assert await work.scheduler._active_count() == 1
        assert await work.scheduler._active_count(run_id=child.task.run_id) == 1
        assert await work.scheduler._active_count(provider=child.task.route.effective.provider) == 1


@pytest.mark.integration
@pytest.mark.parametrize("invalid", [False, True])
async def test_capacity_checks_a_paused_other_run_without_waiting_for_its_lock(
    session_factory, tmp_path, invalid
):
    factory, _, _, child = await scope_case(session_factory, tmp_path / "stopped")
    pause = await control(factory, child, "pause")
    other_factory, _, other_run, _ = await prepared_case(session_factory, tmp_path / "eligible")
    async with factory() as work:
        await work.scheduler.configure_capacity(
            SchedulerCapacityPolicy(version=2, global_limit=1, run_limit=1, provider_limit=1)
        )
        if invalid:
            (await work.session.get(SubscriptionTaskStop, pause.receipt_id)).settlement_digest = (
                "f" * 64
            )
        await work.commit()
    async with factory() as locked:
        await locked.session.get(Run, child.task.run_id, with_for_update=True)
        await locked.session.get(SubscriptionTaskStop, pause.receipt_id, with_for_update=True)
        admitted = await asyncio.wait_for(
            SubscriptionDecisionExecutor(other_factory).admit_next("other-run", _reservation()), 5
        )
    if invalid:
        assert admitted is None
    else:
        assert admitted is not None and admitted.task.run_id == other_run
