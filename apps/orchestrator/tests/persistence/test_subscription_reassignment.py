"""Primary reassignment preserves stopped work and cumulative authority."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import test_subscription_plan_gate as plan_fixture
from forge.application.ports.subscription_decisions import SubscriptionDecisionError
from forge.application.ports.subscription_gateway import (
    SubscriptionFailure,
    SubscriptionInvocationResult,
)
from forge.application.services.subscription_decision_recovery import SubscriptionDecisionRecovery
from forge.application.services.subscription_decisions import SubscriptionDecisionApplication
from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
from forge.artifacts.filesystem import FilesystemArtifactStore
from forge.domain.provider_quota import QuotaExhaustion
from forge.domain.subscription import (
    AuthMode,
    BillingMode,
    BoundReassignDecision,
    HandoffStatus,
    ReassignDecision,
    ScopeRequestDecision,
    TaskBudget,
    TaskHandoff,
    decode_subscription_record,
)
from forge.persistence.models.scheduling import (
    SubscriptionScheduledEffect,
    SubscriptionScheduledTask,
)
from forge.persistence.models.subscription import SubscriptionClientLaunch, SubscriptionTask
from forge.persistence.models.subscription_results import (
    SubscriptionAttemptResult,
    SubscriptionRepairDebit,
)
from sqlalchemy import select
from subscription_launch_fixture import record_stopped_launch
from test_scheduler_acceptance import (  # noqa: F401
    _admit_run,
    _remove_disposable_subscription_rows,
    _route,
)
from test_subscription_delegation_application import delegation_case
from test_subscription_usage import _known, _reservation


async def reassignment_case(
    session_factory, tmp_path, monkeypatch, *, mutate=None, legacy=False, source_kind="failure"
):
    preferred, fallback = _route("preferred"), _route("fallback")

    async def admit_profile(work, run, routes, **kwargs):
        return await _admit_run(
            work, run, (routes[0], preferred), worker_fallbacks=(fallback,), **kwargs
        )

    monkeypatch.setattr(plan_fixture, "_admit_run", admit_profile)
    factory, delegated, _, _ = await delegation_case(
        session_factory,
        tmp_path,
        lambda child, parent: (
            replace(child, budget=replace(child.budget, max_provider_attempts=3)),
        ),
        primary_budget=TaskBudget(max_provider_attempts=12),
    )
    application = SubscriptionDecisionApplication(factory)
    assert (await application.apply_delegation(delegated.attempt.attempt_id)).accepted
    executor = SubscriptionDecisionExecutor(factory)
    child = await executor.admit_next("failed-child", _reservation())
    assert child is not None
    stopped = await record_stopped_launch(session_factory, child)
    failed_decision = (
        ScopeRequestDecision(
            run_id=child.task.run_id,
            task_id=child.task.task_id,
            requested_paths=("apps/shared",),
            reason="Need broader scope",
        )
        if source_kind == "scope"
        else TaskHandoff(
            run_id=child.task.run_id,
            task_id=child.task.task_id,
            attempt_id=child.attempt.attempt_id,
            status=HandoffStatus.BLOCKED,
            summary="Needs another specialist",
        )
        if source_kind == "handoff"
        else None
    )
    assert (
        await executor.settle(
            child,
            SubscriptionInvocationResult(
                attempt=child.attempt,
                failure=SubscriptionFailure.PROTOCOL if failed_decision is None else None,
                decision=failed_decision,
                telemetry=_known(),
                launch_proof=stopped,
            ),
        )
    ).disposition == {"failure": "failed", "scope": "decision_pending", "handoff": "handoff"}[
        source_kind
    ]
    if source_kind == "scope":
        assert (await application.apply_scope_request(child.attempt.attempt_id)).accepted
    primary = await executor.admit_next("reassigning-primary", _reservation())
    assert primary is not None and primary.task.task_id == delegated.task.task_id
    async with factory() as work:
        child_version = (await work.session.get(SubscriptionTask, child.task.task_id)).version
    decision = BoundReassignDecision(
        run_id=child.task.run_id,
        task_id=child.task.task_id,
        source_attempt_id=child.attempt.attempt_id,
        expected_task_version=child_version,
        new_route=fallback,
        reason="Continue the stopped task through the approved specialist",
    )
    if mutate:
        decision = mutate(decision, primary)
    if legacy:
        decision = ReassignDecision(
            run_id=decision.run_id,
            task_id=decision.task_id,
            new_route=decision.new_route,
            reason=decision.reason,
        )
    proof = await record_stopped_launch(session_factory, primary)
    assert (
        await executor.settle(
            primary,
            SubscriptionInvocationResult(
                attempt=primary.attempt, decision=decision, telemetry=_known(), launch_proof=proof
            ),
        )
    ).disposition == "decision_pending"
    return factory, application, primary, child, decision


@pytest.mark.integration
@pytest.mark.parametrize("source_kind", ["failure", "scope", "handoff"])
async def test_restart_applies_primary_reassignment_and_preserves_task_budget(
    session_factory, tmp_path, monkeypatch, source_kind
):
    factory, _, primary, child, decision = await reassignment_case(
        session_factory, tmp_path, monkeypatch, source_kind=source_kind
    )
    partial = tmp_path / "prepared" / "apps" / "feature" / "partial.py"
    partial.parent.mkdir(parents=True, exist_ok=True)
    partial.write_text("preserved partial implementation\n", encoding="utf-8")
    async with factory() as work:
        stored = await work.session.get(SubscriptionAttemptResult, primary.attempt.attempt_id)
        original = stored.result_payload, stored.result_digest
    report = await SubscriptionDecisionRecovery(
        factory, FilesystemArtifactStore(tmp_path / "artifacts")
    ).reconcile_all()
    assert report.applied == 1 and report.deferred == report.unsupported == 0
    assert partial.read_text(encoding="utf-8") == "preserved partial implementation\n"
    async with factory() as work:
        stored = await work.session.get(SubscriptionAttemptResult, primary.attempt.attempt_id)
        assert (stored.result_payload, stored.result_digest) == original
    resumed = await SubscriptionDecisionExecutor(factory).admit_next("replacement", _reservation())
    assert resumed is not None and resumed.task.task_id == child.task.task_id
    assert resumed.task.route.requested == child.task.route.requested
    assert resumed.task.route.effective == decision.new_route
    assert resumed.task.owned_paths == child.task.owned_paths
    assert resumed.task.budget == child.task.budget
    assert replace(resumed.task, route=child.task.route) == child.task
    assert resumed.attempt.attempt_number == 2
    async with factory() as work:
        parent = await work.session.get(SubscriptionTask, primary.task.task_id)
        assert parent.state == "blocked"
        usage = await work.subscription_budget.usage(child.task.run_id, child.task.task_id)
        assert usage.consumed.provider_attempts == 1 and usage.consumed.repairs == 0
        assert usage.outstanding.provider_attempts == 1
    assert (
        await SubscriptionDecisionApplication(factory).apply_reassignment(
            primary.attempt.attempt_id
        )
    ).replayed
    proof = await record_stopped_launch(session_factory, resumed)
    await SubscriptionDecisionExecutor(factory).settle(
        resumed,
        SubscriptionInvocationResult(
            attempt=resumed.attempt,
            failure=SubscriptionFailure.PROTOCOL,
            telemetry=_known(),
            launch_proof=proof,
        ),
    )
    continued = await SubscriptionDecisionExecutor(factory).admit_next(
        "primary-continues", _reservation()
    )
    assert continued is not None and continued.task.task_id == primary.task.task_id
    assert continued.task.route == primary.task.route
    assert (
        await SubscriptionDecisionApplication(factory).apply_reassignment(
            primary.attempt.attempt_id
        )
    ).replayed


@pytest.mark.integration
async def test_reassignment_rollback_and_concurrent_recovery(
    session_factory, tmp_path, monkeypatch
):
    factory, application, primary, child, decision = await reassignment_case(
        session_factory, tmp_path, monkeypatch
    )
    async with factory() as work:
        await work.subscription_decisions.apply_reassignment(primary.attempt.attempt_id)
        await work.rollback()
    async with factory() as work:
        row = await work.session.get(SubscriptionTask, child.task.task_id)
        assert row.version == decision.expected_task_version and row.state == "terminal"
        assert decode_subscription_record(row.payload) == child.task
    outcomes = await asyncio.wait_for(
        asyncio.gather(
            application.apply_reassignment(primary.attempt.attempt_id),
            SubscriptionDecisionApplication(factory).apply_reassignment(primary.attempt.attempt_id),
        ),
        10,
    )
    assert sorted(outcome.replayed for outcome in outcomes) == [False, True]
    async with factory() as work:
        row = await work.session.get(SubscriptionTask, child.task.task_id)
        assert row.version == decision.expected_task_version + 1
        usage = await work.subscription_budget.usage(child.task.run_id, child.task.task_id)
        assert usage.consumed.provider_attempts == 1 and usage.consumed.repairs == 0
        assert usage.outstanding.provider_attempts == 0


@pytest.mark.integration
@pytest.mark.parametrize(
    "change",
    [
        "pause",
        "cancel",
        "version",
        "generation",
        "paths",
        "provider",
        "leased",
        "effect",
        "child_launch",
        "parent_launch",
        "history",
    ],
)
async def test_reassignment_defers_unsafe_current_target_without_repair(
    session_factory, tmp_path, monkeypatch, change
):
    factory, application, primary, child, _ = await reassignment_case(
        session_factory, tmp_path, monkeypatch
    )
    async with factory() as work:
        row = await work.session.get(SubscriptionTask, child.task.task_id)
        scheduled = await work.session.get(SubscriptionScheduledTask, child.task.task_id)
        if change == "pause":
            row.pause_requested = True
        elif change == "cancel":
            scheduled.cancel_requested = True
        elif change == "version":
            row.version += 1
        elif change == "generation":
            scheduled.lease_generation += 1
        elif change == "paths":
            scheduled.owned_paths = ["other"]
        elif change == "provider":
            scheduled.provider = "other"
        elif change == "leased":
            row.state = "running"
            scheduled.state = "leased"
            scheduled.lease_owner = "another-worker"
        elif change == "history":
            (
                await work.session.get(SubscriptionAttemptResult, child.attempt.attempt_id)
            ).result_digest = "f" * 64
        elif change.endswith("launch"):
            attempt_id = (
                child.attempt.attempt_id if change == "child_launch" else primary.attempt.attempt_id
            )
            await work.session.delete(
                await work.session.scalar(
                    select(SubscriptionClientLaunch).where(
                        SubscriptionClientLaunch.attempt_id == attempt_id,
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
        await application.apply_reassignment(primary.attempt.attempt_id)
    async with factory() as work:
        assert await work.session.get(SubscriptionRepairDebit, primary.attempt.attempt_id) is None
        assert (
            await work.session.get(SubscriptionAttemptResult, primary.attempt.attempt_id)
        ).disposition == "decision_pending"
        assert (
            decode_subscription_record(
                (await work.session.get(SubscriptionTask, child.task.task_id)).payload
            )
            == child.task
        )


@pytest.mark.integration
@pytest.mark.parametrize(
    "change", ["route", "auth", "billing", "discard", "target", "source", "primary"]
)
async def test_invalid_reassignment_is_bounded_primary_repair(
    session_factory, tmp_path, monkeypatch, change
):
    def mutate(value, primary):
        if change == "route":
            return replace(value, new_route=_route("unapproved"))
        if change == "auth":
            return replace(value, new_route=replace(value.new_route, auth_mode=AuthMode.API_KEY))
        if change == "billing":
            return replace(
                value, new_route=replace(value.new_route, billing_mode=BillingMode.PAID_OPT_IN)
            )
        if change == "discard":
            return replace(value, preserve_partial_work=False)
        if change == "target":
            return replace(value, task_id=uuid4())
        if change == "source":
            return replace(value, source_attempt_id=uuid4())
        return replace(
            value, task_id=primary.task.task_id, source_attempt_id=primary.attempt.attempt_id
        )

    factory, application, primary, child, _ = await reassignment_case(
        session_factory, tmp_path, monkeypatch, mutate=mutate
    )
    outcome = await application.apply_reassignment(primary.attempt.attempt_id)
    assert not outcome.accepted and outcome.disposition == "decision_repair_queued"
    assert (await application.apply_reassignment(primary.attempt.attempt_id)).replayed
    async with factory() as work:
        row = await work.session.get(SubscriptionTask, child.task.task_id)
        assert row.state == "terminal" and decode_subscription_record(row.payload) == child.task
        assert (
            await work.session.get(SubscriptionRepairDebit, primary.attempt.attempt_id) is not None
        )


@pytest.mark.integration
async def test_reassignment_keeps_quota_and_admission_budgets(
    session_factory, tmp_path, monkeypatch
):
    factory, application, primary, child, decision = await reassignment_case(
        session_factory, tmp_path, monkeypatch
    )
    async with factory() as work:
        now = datetime.now(UTC)
        for route in (child.task.route.effective, decision.new_route):
            await work.quota.report_exhaustion(
                work.quota.policy.key_for(route),
                QuotaExhaustion(now, "operator_report", now + timedelta(hours=1)),
                actor_id=uuid4(),
                idempotency_key=route.provider,
            )
        await work.commit()
    await application.apply_reassignment(primary.attempt.attempt_id)
    assert (
        await SubscriptionDecisionExecutor(factory).admit_next("no-eligible-route", _reservation())
        is None
    )
    async with factory() as work:
        usage = await work.subscription_budget.usage(child.task.run_id, child.task.task_id)
        assert usage.consumed.provider_attempts == 1 and usage.consumed.repairs == 0
        assert usage.outstanding.provider_attempts == 0
        scheduled = await work.session.get(SubscriptionScheduledTask, child.task.task_id)
        assert scheduled.state == "queued" and scheduled.lease_owner is None


@pytest.mark.integration
async def test_unbound_historical_reassignment_grants_no_new_authority(
    session_factory, tmp_path, monkeypatch
):
    factory, _, _, child, _ = await reassignment_case(
        session_factory, tmp_path, monkeypatch, legacy=True
    )
    report = await SubscriptionDecisionRecovery(
        factory, FilesystemArtifactStore(tmp_path / "artifacts")
    ).reconcile_all()
    assert report.unsupported == 1 and report.applied == 0
    async with factory() as work:
        assert (await work.session.get(SubscriptionTask, child.task.task_id)).state == "terminal"


@pytest.mark.integration
async def test_reassignment_replay_checks_immutable_application_receipt(
    session_factory, tmp_path, monkeypatch
):
    factory, application, primary, _, _ = await reassignment_case(
        session_factory, tmp_path, monkeypatch
    )
    await application.apply_reassignment(primary.attempt.attempt_id)
    async with factory() as work:
        result = await work.session.get(SubscriptionAttemptResult, primary.attempt.attempt_id)
        result.application_payload = {**result.application_payload, "child_version": 999}
        await work.commit()
    with pytest.raises(SubscriptionDecisionError, match="reassignment replay"):
        await application.apply_reassignment(primary.attempt.attempt_id)


@pytest.mark.integration
async def test_old_stopped_attempt_cannot_reassign_a_newer_failure(
    session_factory, tmp_path, monkeypatch
):
    factory, application, primary, child, decision = await reassignment_case(
        session_factory, tmp_path, monkeypatch
    )
    await application.apply_reassignment(primary.attempt.attempt_id)
    executor = SubscriptionDecisionExecutor(factory)
    newer = await executor.admit_next("newer-child", _reservation())
    assert newer is not None and newer.task.task_id == child.task.task_id
    proof = await record_stopped_launch(session_factory, newer)
    await executor.settle(
        newer,
        SubscriptionInvocationResult(
            attempt=newer.attempt,
            failure=SubscriptionFailure.PROTOCOL,
            telemetry=_known(),
            launch_proof=proof,
        ),
    )
    continuation = await executor.admit_next("primary-considers-newer-failure", _reservation())
    assert continuation is not None and continuation.task.task_id == primary.task.task_id
    async with factory() as work:
        version = (await work.session.get(SubscriptionTask, child.task.task_id)).version
    stale = replace(decision, expected_task_version=version)
    proof = await record_stopped_launch(session_factory, continuation)
    await executor.settle(
        continuation,
        SubscriptionInvocationResult(
            attempt=continuation.attempt,
            decision=stale,
            telemetry=_known(),
            launch_proof=proof,
        ),
    )
    with pytest.raises(SubscriptionDecisionError, match="no longer current"):
        await application.apply_reassignment(continuation.attempt.attempt_id)
    async with factory() as work:
        row = await work.session.get(SubscriptionTask, child.task.task_id)
        assert row.version == version and row.state == "terminal"
        assert decode_subscription_record(row.payload) == newer.task
