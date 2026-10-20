"""A settled failed primary needs an explicit bounded owner continuation."""

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from forge.application.ports.subscription_gateway import (
    SubscriptionFailure,
    SubscriptionInvocationResult,
)
from forge.application.services.auth import AuthenticatedActor
from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
from forge.application.services.subscription_recovery import SubscriptionRecoveryService
from forge.domain.subscription import AttemptTelemetry, TaskBudget, UnknownTelemetryPolicy
from forge.domain.subscription_recovery import (
    RecoveryAction,
    RecoveryApplyRequest,
    RecoveryPreviewRequest,
)
from forge.persistence.models.execution import Approval, RunCommand
from forge.persistence.models.run import Run
from forge.persistence.models.scheduling import (
    SubscriptionScheduledEffect,
    SubscriptionScheduledTask,
    SubscriptionSchedulerRun,
)
from forge.persistence.models.subscription import (
    SubscriptionAttempt,
    SubscriptionClientLaunch,
    SubscriptionEnvelope,
    SubscriptionTask,
)
from forge.persistence.models.subscription_recovery import (
    SubscriptionRecoveryReceipt,
    SubscriptionRecoveryWorker,
)
from forge.persistence.models.subscription_results import (
    SubscriptionAttemptResult,
    SubscriptionRepairDebit,
)
from forge.persistence.models.subscription_usage import SubscriptionAttemptConsumption
from forge.persistence.repositories.commands import PostgresCommandRepository
from forge.persistence.repositories.subscription_recovery import RecoveryConflict
from sqlalchemy import select, text
from subscription_launch_fixture import record_stopped_launch
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_preparation import preparation_case
from test_subscription_usage import _reservation


async def failed_primary(session_factory, tmp_path, *, budget=None, pools=False):
    factory, _, command, preparation, _ = await preparation_case(
        session_factory,
        tmp_path,
        primary_budget=budget
        or TaskBudget(
            max_provider_attempts=8,
            max_repairs=3,
            unknown_telemetry_policy=UnknownTelemetryPolicy(max_uncertain_attempts=5),
        ),
    )
    async with factory() as work:
        await preparation.execute(command, work)
    await PostgresCommandRepository(session_factory).complete(
        command.id, worker_id=command.lease_owner
    )
    admission = await SubscriptionDecisionExecutor(factory).admit_next(
        "failed-primary", _reservation()
    )
    assert admission is not None
    proof = await record_stopped_launch(session_factory, admission)
    result = SubscriptionInvocationResult(
        attempt=admission.attempt,
        failure=SubscriptionFailure.OUTAGE,
        telemetry=AttemptTelemetry(input_tokens=13, output_tokens=5, duration_ms=100),
        launch_proof=proof,
    )
    settlement = await SubscriptionDecisionExecutor(factory).settle(admission, result)
    assert settlement.accepted and settlement.disposition == "failed"
    async with factory() as work:
        if pools:
            await work.subscription.initialize_budget(
                admission.task.run_id, None, admission.task.budget
            )
            await work.subscription.initialize_budget(
                admission.task.run_id, admission.task.task_id, admission.task.budget
            )
        work.session.add(
            SubscriptionRecoveryWorker(
                worker_id=f"failed-primary-{uuid4()}",
                contract_version=1,
                observed_at=datetime.now(UTC),
            )
        )
        await work.commit()
    return factory, admission


@pytest.mark.integration
async def test_owner_can_retry_stopped_failed_primary_without_rewriting_result(
    session_factory,
    tmp_path,
):
    factory, admission = await failed_primary(session_factory, tmp_path)
    identity = {
        "run_id": admission.task.run_id,
        "task_id": admission.task.task_id,
        "attempt_id": admission.attempt.attempt_id,
    }
    service = SubscriptionRecoveryService(factory)
    actor = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())
    async with factory() as work:
        run = await work.session.get(Run, identity["run_id"])
        preserved = Path(run.worktree_path) / "preserved-candidate.txt"
        preserved.parent.mkdir(parents=True, exist_ok=True)
        preserved.write_text("committed work followed by unfinished edits", encoding="utf-8")
        result = await work.session.get(SubscriptionAttemptResult, identity["attempt_id"])
        before = deepcopy(
            (result.result_payload, result.result_digest, result.accepted, result.disposition)
        )
        consumption = await work.session.get(SubscriptionAttemptConsumption, identity["attempt_id"])
        usage_before = deepcopy(
            (consumption.observed, consumption.charged, consumption.unknown_fields)
        )
        assert consumption.unknown_fields
        assert (await work.session.get(SubscriptionTask, identity["task_id"])).state == "terminal"
        source = await work.session.get(SubscriptionScheduledTask, identity["task_id"])
        worktree = source.worktree_id
    assert (
        await SubscriptionDecisionExecutor(factory).admit_next("no-auto-retry", _reservation())
        is None
    )
    preview = await service.preview(
        **identity,
        actor=actor,
        request=RecoveryPreviewRequest(action="retry_failed_attempt"),
    )
    assert preview.eligible, preview.reason_code
    assert (preview.budget_impact.provider_attempts, preview.budget_impact.repair_units) == (1, 1)
    request = RecoveryApplyRequest(
        action="retry_failed_attempt", preview_token=preview.preview_token
    )
    # A retry does not approve or observe source contents. Edits made after the
    # signed preview are retained for the fresh invocation's own source capture.
    preserved.write_text("intervening unfinished edits", encoding="utf-8")
    receipt, replay = await asyncio.gather(
        *(
            service.apply(**identity, actor=actor, idempotency_key="retry-failed", request=request)
            for _ in range(2)
        )
    )
    assert replay == receipt
    assert preserved.read_text(encoding="utf-8") == "intervening unfinished edits"
    with pytest.raises(RecoveryConflict):
        await service.apply(
            **identity, actor=actor, idempotency_key="different-key", request=request
        )
    async with factory() as work:
        result = await work.session.get(SubscriptionAttemptResult, identity["attempt_id"])
        assert (
            result.result_payload,
            result.result_digest,
            result.accepted,
            result.disposition,
        ) == before
        source = await work.session.get(SubscriptionScheduledTask, identity["task_id"])
        assert source.state == "queued" and source.repairs == 1
        assert source.worktree_id == worktree
        consumption = await work.session.get(SubscriptionAttemptConsumption, identity["attempt_id"])
        assert (
            consumption.observed,
            consumption.charged,
            consumption.unknown_fields,
        ) == usage_before
        assert len((await work.session.scalars(select(SubscriptionRepairDebit))).all()) == 1
        assert len((await work.session.scalars(select(SubscriptionRecoveryReceipt))).all()) == 1
    next_attempt = await SubscriptionDecisionExecutor(factory).admit_next(
        "owner-retry", _reservation()
    )
    assert next_attempt is not None and next_attempt.task == admission.task
    assert next_attempt.attempt.attempt_id != admission.attempt.attempt_id
    assert next_attempt.attempt.attempt_number == admission.attempt.attempt_number + 1
    from forge.persistence.queries.subscription_tasks import SubscriptionTaskQuery

    page = await SubscriptionTaskQuery(session_factory).attempts(
        identity["run_id"], identity["task_id"]
    )
    old = next(item for item in page["attempts"] if item["attempt_id"] == identity["attempt_id"])
    assert old["recovery"]["resolution"] == "retained"
    assert old["recovery"]["eligible_actions"] == []


@pytest.mark.integration
async def test_current_failed_primary_is_visible_with_owner_action(session_factory, tmp_path):
    from forge.persistence.queries.subscription_tasks import SubscriptionTaskQuery

    _, admission = await failed_primary(session_factory, tmp_path)
    query = SubscriptionTaskQuery(session_factory)
    tasks = await query.tasks(admission.task.run_id)
    task = next(item for item in tasks["tasks"] if item["task_id"] == admission.task.task_id)
    assert tasks["recovery_attention"] and task["recovery_attention"]
    assert task["recovery_reason_code"] == "attempt_failed_outage"
    page = await query.attempts(admission.task.run_id, admission.task.task_id)
    attempt = next(
        item for item in page["attempts"] if item["attempt_id"] == admission.attempt.attempt_id
    )
    assert attempt["recovery"]["classification"] == "provider_failure:outage"
    assert attempt["recovery"]["eligible_actions"] == ["retry_failed_attempt"]


@pytest.mark.integration
@pytest.mark.parametrize(
    "drift",
    [
        "run_version",
        "task_version",
        "worktree",
        "candidate",
        "approval",
        "route",
        "graph",
        "envelope",
        "result",
        "newer_attempt",
        "live_lease",
        "missing_launch",
        "uncertain_launch",
        "mismatched_launch",
        "unsettled_effect",
        "paused",
        "cancelled",
        "task_pause",
        "task_cancel",
        "pending_stop",
        "budget",
        "worker",
        "foreign_actor",
    ],
)
async def test_failed_retry_revalidates_current_authority_without_partial_effects(
    session_factory,
    tmp_path,
    drift,
):
    factory, admission = await failed_primary(session_factory, tmp_path)
    identity = {
        "run_id": admission.task.run_id,
        "task_id": admission.task.task_id,
        "attempt_id": admission.attempt.attempt_id,
    }
    actor = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())
    service = SubscriptionRecoveryService(factory)
    preview = await service.preview(
        **identity,
        actor=actor,
        request=RecoveryPreviewRequest(action=RecoveryAction.RETRY_FAILED_ATTEMPT),
    )
    assert preview.eligible, preview.reason_code
    async with factory() as work:
        run = await work.session.get(Run, identity["run_id"])
        task = await work.session.get(SubscriptionTask, identity["task_id"])
        scheduled = await work.session.get(SubscriptionScheduledTask, identity["task_id"])
        attempt = await work.session.get(SubscriptionAttempt, identity["attempt_id"])
        if drift == "run_version":
            run.version += 1
        elif drift == "task_version":
            task.version += 1
        elif drift == "worktree":
            run.worktree_path = str(tmp_path / "other-worktree")
        elif drift == "candidate":
            (await work.session.get(SubscriptionSchedulerRun, run.id)).candidate_epoch += 1
        elif drift == "approval":
            approval = await work.session.scalar(select(Approval).where(Approval.run_id == run.id))
            approval.invalidated_at = datetime.now(UTC)
        elif drift == "route":
            scheduled.provider = "other-provider"
        elif drift == "graph":
            scheduled.dependency_task_ids = [uuid4()]
        elif drift == "envelope":
            envelope = await work.session.get(SubscriptionEnvelope, run.id)
            envelope.payload = {**envelope.payload, "unexpected": True}
        elif drift == "result":
            result = await work.session.get(SubscriptionAttemptResult, attempt.id)
            result.result_digest = "0" * 64
        elif drift == "newer_attempt":
            work.session.add(
                SubscriptionAttempt(
                    id=uuid4(),
                    run_id=run.id,
                    task_row_id=task.id,
                    attempt_number=attempt.attempt_number + 1,
                    idempotency_key="newer",
                    route_payload=attempt.route_payload,
                    status="queued",
                )
            )
        elif drift == "live_lease":
            await work.session.execute(text("SET LOCAL forge.recovery_contract_version = '1'"))
            scheduled.state = "leased"
            scheduled.lease_owner = "another-worker"
            scheduled.lease_expires_at = datetime.now(UTC) + timedelta(minutes=1)
        elif drift in {"missing_launch", "uncertain_launch", "mismatched_launch"}:
            launch = await work.session.scalar(
                select(SubscriptionClientLaunch).where(
                    SubscriptionClientLaunch.attempt_id == attempt.id
                )
            )
            if drift == "missing_launch":
                await work.session.delete(launch)
            elif drift == "uncertain_launch":
                launch.state = "uncertain"
            else:
                launch.process_start_token = "other-process"
        elif drift == "unsettled_effect":
            work.session.add(
                SubscriptionScheduledEffect(
                    id=uuid4(),
                    run_id=run.id,
                    task_id=task.id,
                    lease_owner=attempt.lease_owner,
                    lease_generation=attempt.lease_generation,
                    candidate_epoch=attempt.candidate_epoch,
                    state="reconciling",
                )
            )
        elif drift in {"paused", "cancelled"}:
            run.state = "PAUSED" if drift == "paused" else "CANCELLED"
            if drift == "paused":
                run.suspended_state = "IMPLEMENTING"
                run.suspension_kind = "PAUSE"
                run.suspension_context = {}
                run.suspension_context_schema_version = 1
        elif drift == "task_pause":
            task.pause_requested = True
        elif drift == "task_cancel":
            scheduled.cancel_requested = True
        elif drift == "pending_stop":
            work.session.add(
                RunCommand(
                    run_id=run.id,
                    idempotency_key="pending-stop",
                    command_type="cancel",
                    expected_run_version=run.version,
                    actor_id=actor.actor_id,
                    payload={},
                )
            )
        elif drift == "budget":
            scheduled.max_repairs = scheduled.repairs
        elif drift == "worker":
            for worker in await work.session.scalars(select(SubscriptionRecoveryWorker)):
                worker.observed_at = datetime.now(UTC) - timedelta(minutes=5)
        await work.commit()
    if drift == "foreign_actor":
        actor = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())
    with pytest.raises(RecoveryConflict):
        await service.apply(
            **identity,
            actor=actor,
            idempotency_key=f"drift-{drift}",
            request=RecoveryApplyRequest(
                action=preview.action, preview_token=preview.preview_token
            ),
        )
    async with factory() as work:
        assert await work.session.get(SubscriptionRepairDebit, identity["attempt_id"]) is None
        assert await work.session.scalar(select(SubscriptionRecoveryReceipt.id)) is None
        assert (await work.session.get(SubscriptionTask, identity["task_id"])).state == "terminal"


@pytest.mark.integration
async def test_failed_retry_budget_refusal_is_visible_and_does_not_clear_unknown_usage(
    session_factory,
    tmp_path,
):
    from forge.persistence.queries.subscription_tasks import SubscriptionTaskQuery

    factory, admission = await failed_primary(
        session_factory, tmp_path, budget=TaskBudget(max_provider_attempts=2, max_repairs=0)
    )
    page = await SubscriptionTaskQuery(session_factory).attempts(
        admission.task.run_id, admission.task.task_id
    )
    latest = next(
        item for item in page["attempts"] if item["attempt_id"] == admission.attempt.attempt_id
    )
    assert latest["recovery"]["reason_code"] == "budget_exhausted"
    assert latest["recovery"]["eligible_actions"] == ["retry_failed_attempt_with_allowance"]
    async with factory() as work:
        consumption = await work.session.get(
            SubscriptionAttemptConsumption, admission.attempt.attempt_id
        )
        assert consumption.unknown_fields and consumption.uncertain
        assert await work.session.get(SubscriptionRepairDebit, admission.attempt.attempt_id) is None


@pytest.mark.integration
@pytest.mark.parametrize("pools", [False, True])
async def test_owner_can_add_one_finite_retry_allowance_preserving_unknown_usage(
    session_factory,
    tmp_path,
    pools,
):
    from dataclasses import replace

    from forge.domain.subscription import decode_subscription_record
    from forge.persistence.models.execution import RunEvent

    budget = TaskBudget(max_provider_attempts=2, max_repairs=0)
    factory, admission = await failed_primary(session_factory, tmp_path, budget=budget, pools=pools)
    identity = {
        "run_id": admission.task.run_id,
        "task_id": admission.task.task_id,
        "attempt_id": admission.attempt.attempt_id,
    }
    actor = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())
    service = SubscriptionRecoveryService(factory)
    default = await service.preview(
        **identity, actor=actor, request=RecoveryPreviewRequest(action="retry_failed_attempt")
    )
    assert not default.eligible and default.reason_code == "budget_exhausted"
    async with factory() as work:
        usage_before = await work.subscription_budget.usage(admission.task.run_id)
        result = await work.session.get(SubscriptionAttemptResult, identity["attempt_id"])
        result_before = deepcopy(result.result_payload)
    preview = await service.preview(
        **identity,
        actor=actor,
        request=RecoveryPreviewRequest(action="retry_failed_attempt_with_allowance"),
    )
    assert preview.eligible, preview.reason_code
    assert any("uncertain" in warning for warning in preview.changes)
    request = RecoveryApplyRequest(action=preview.action, preview_token=preview.preview_token)
    receipts = await asyncio.gather(
        *(
            service.apply(**identity, actor=actor, idempotency_key="one-allowance", request=request)
            for _ in range(2)
        )
    )
    assert receipts[0] == receipts[1]
    async with factory() as work:
        contract = decode_subscription_record(
            (await work.session.get(SubscriptionTask, identity["task_id"])).payload
        )
        assert (
            replace(
                contract.budget,
                max_provider_attempts=budget.max_provider_attempts,
                max_repairs=budget.max_repairs,
                unknown_telemetry_policy=budget.unknown_telemetry_policy,
            )
            == budget
        )
        assert contract.budget.max_provider_attempts == 3
        assert contract.budget.max_repairs == 1
        assert contract.budget.unknown_telemetry_policy.max_uncertain_attempts == 2
        usage_after = await work.subscription_budget.usage(admission.task.run_id)
        assert usage_after.uncertain_attempts == usage_before.uncertain_attempts == 1
        assert usage_after.consumed.cost_minor == usage_before.consumed.cost_minor == 20
        consumption = await work.session.get(SubscriptionAttemptConsumption, identity["attempt_id"])
        assert consumption.observed["cost_minor"] is None
        assert "cost_minor" in consumption.unknown_fields
        assert usage_after.consumed.input_tokens == usage_before.consumed.input_tokens
        assert usage_after.consumed.repairs == usage_before.consumed.repairs + 1
        assert (
            await work.session.get(SubscriptionAttemptResult, identity["attempt_id"])
        ).result_payload == result_before
        events = (
            await work.session.scalars(
                select(RunEvent).where(
                    RunEvent.run_id == identity["run_id"],
                    RunEvent.event_type == "subscription_retry_allowance_extended",
                )
            )
        ).all()
        assert len(events) == 1 and events[0].actor_id == actor.actor_id
        assert events[0].payload["warnings"] and events[0].payload["scopes"]
    next_attempt = await SubscriptionDecisionExecutor(factory).admit_next(
        "allowance-retry", _reservation()
    )
    assert next_attempt is not None and next_attempt.task.budget == contract.budget
    assert next_attempt.attempt.attempt_number == admission.attempt.attempt_number + 1
    with pytest.raises(RecoveryConflict):
        await service.apply(
            **identity, actor=actor, idempotency_key="cannot-reuse", request=request
        )
    proof = await record_stopped_launch(session_factory, next_attempt)
    settled = await SubscriptionDecisionExecutor(factory).settle(
        next_attempt,
        SubscriptionInvocationResult(
            attempt=next_attempt.attempt,
            failure=SubscriptionFailure.OUTAGE,
            telemetry=AttemptTelemetry(input_tokens=1, output_tokens=1, duration_ms=100),
            launch_proof=proof,
        ),
    )
    assert settled.disposition == "failed"
    fresh_identity = {**identity, "attempt_id": next_attempt.attempt.attempt_id}
    refused = await service.preview(
        **fresh_identity, actor=actor, request=RecoveryPreviewRequest(action="retry_failed_attempt")
    )
    assert not refused.eligible and refused.reason_code == "budget_exhausted"
    new_authority = await service.preview(
        **fresh_identity,
        actor=actor,
        request=RecoveryPreviewRequest(action="retry_failed_attempt_with_allowance"),
    )
    assert new_authority.eligible, new_authority.reason_code
    assert new_authority.preview_token != preview.preview_token


@pytest.mark.integration
@pytest.mark.parametrize("drift", ["pool", "usage", "stop", "debit_failure"])
async def test_count_allowance_stale_snapshot_rolls_back_all_limits_and_debits(
    session_factory,
    tmp_path,
    drift,
    monkeypatch,
):
    from dataclasses import replace

    from forge.domain.subscription import decode_subscription_record, encode_subscription_record
    from forge.persistence.models.subscription import SubscriptionBudgetPool
    from forge.persistence.models.subscription_recovery import SubscriptionContractRevision

    factory, admission = await failed_primary(
        session_factory,
        tmp_path,
        budget=TaskBudget(max_provider_attempts=2, max_repairs=0),
        pools=True,
    )
    identity = {
        "run_id": admission.task.run_id,
        "task_id": admission.task.task_id,
        "attempt_id": admission.attempt.attempt_id,
    }
    actor = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())
    service = SubscriptionRecoveryService(factory)
    preview = await service.preview(
        **identity,
        actor=actor,
        request=RecoveryPreviewRequest(action="retry_failed_attempt_with_allowance"),
    )
    assert preview.eligible
    async with factory() as work:
        original = deepcopy((await work.session.get(SubscriptionTask, identity["task_id"])).payload)
        revisions = len((await work.session.scalars(select(SubscriptionContractRevision))).all())
        if drift == "pool":
            row = await work.session.scalar(
                select(SubscriptionBudgetPool).where(
                    SubscriptionBudgetPool.run_id == identity["run_id"],
                    SubscriptionBudgetPool.task_row_id.is_(None),
                )
            )
            pool = decode_subscription_record(row.payload)
            row.payload = encode_subscription_record(
                replace(pool, total_budget=replace(pool.total_budget, max_provider_attempts=3))
            )
        elif drift == "usage":
            consumption = await work.session.get(
                SubscriptionAttemptConsumption, identity["attempt_id"]
            )
            consumption.charged = {**consumption.charged, "duration_ms": 200}
        elif drift == "stop":
            run = await work.session.get(Run, identity["run_id"])
            work.session.add(
                RunCommand(
                    run_id=run.id,
                    idempotency_key="stop-allowance",
                    command_type="pause",
                    expected_run_version=run.version,
                    actor_id=actor.actor_id,
                    payload={},
                )
            )
        await work.commit()
        pools_before = {
            row.id: deepcopy(row.payload)
            for row in await work.session.scalars(select(SubscriptionBudgetPool))
        }
    if drift == "debit_failure":
        from forge.persistence.repositories.subscription_budget import (
            PostgresSubscriptionBudgetRepository,
        )

        async def refuse(*args, **kwargs):
            return False

        monkeypatch.setattr(PostgresSubscriptionBudgetRepository, "try_debit_repair", refuse)
    with pytest.raises(RecoveryConflict):
        await service.apply(
            **identity,
            actor=actor,
            idempotency_key="stale-allowance",
            request=RecoveryApplyRequest(
                action=preview.action, preview_token=preview.preview_token
            ),
        )
    async with factory() as work:
        assert (await work.session.get(SubscriptionTask, identity["task_id"])).payload == original
        assert (
            len((await work.session.scalars(select(SubscriptionContractRevision))).all())
            == revisions
        )
        assert await work.session.get(SubscriptionRepairDebit, identity["attempt_id"]) is None
        assert await work.session.scalar(select(SubscriptionRecoveryReceipt.id)) is None
        assert {
            row.id: row.payload
            for row in await work.session.scalars(select(SubscriptionBudgetPool))
        } == pools_before


@pytest.mark.integration
@pytest.mark.parametrize("dimension", ["duration_ms", "cost_minor", "input_tokens", "policy"])
async def test_count_allowance_cannot_waive_other_limits(session_factory, tmp_path, dimension):
    from dataclasses import replace

    from forge.domain.subscription import decode_subscription_record, encode_subscription_record
    from forge.persistence.models.subscription import SubscriptionBudgetPool

    factory, admission = await failed_primary(
        session_factory,
        tmp_path,
        budget=TaskBudget(max_provider_attempts=2, max_repairs=0),
        pools=True,
    )
    async with factory() as work:
        if dimension == "policy":
            consumption = await work.session.get(
                SubscriptionAttemptConsumption, admission.attempt.attempt_id
            )
            consumption.policy_violations = ["unknown cost telemetry is disallowed by policy"]
        else:
            row = await work.session.scalar(
                select(SubscriptionBudgetPool).where(
                    SubscriptionBudgetPool.run_id == admission.task.run_id,
                    SubscriptionBudgetPool.task_row_id.is_(None),
                )
            )
            pool = decode_subscription_record(row.payload)
            change = {
                "duration_ms": {"max_duration_seconds": 1},
                "cost_minor": {"max_cost_minor": 19},
                "input_tokens": {"max_input_tokens": 12},
            }[dimension]
            row.payload = encode_subscription_record(
                replace(pool, total_budget=replace(pool.total_budget, **change))
            )
        await work.commit()
    actor = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())
    preview = await SubscriptionRecoveryService(factory).preview(
        run_id=admission.task.run_id,
        task_id=admission.task.task_id,
        attempt_id=admission.attempt.attempt_id,
        actor=actor,
        request=RecoveryPreviewRequest(action="retry_failed_attempt_with_allowance"),
    )
    assert not preview.eligible and preview.reason_code == "budget_not_extendable"
    async with factory() as work:
        assert await work.session.get(SubscriptionRepairDebit, admission.attempt.attempt_id) is None
