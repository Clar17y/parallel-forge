"""Checkpoint interruption preserves repair capacity after settled provider work."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from forge.application.ports.subscription_decisions import SubscriptionDecisionError
from forge.application.ports.subscription_gateway import (
    SubscriptionFailure,
    SubscriptionInvocationResult,
)
from forge.application.services.auth import AuthenticatedActor
from forge.application.services.subscription_decision_recovery import SubscriptionDecisionRecovery
from forge.application.services.subscription_decisions import SubscriptionDecisionApplication
from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
from forge.application.services.task_usage_controls import RunTaskUsageControls
from forge.application.services.task_usage_monitor import (
    RunTaskUsageMonitor,
    run_checkpoint_quiescent,
)
from forge.domain.provider_quota import QuotaExhaustion
from forge.domain.subscription import HandoffStatus, TaskHandoff
from forge.domain.subscription_quota import QuotaPolicy
from forge.domain.task_usage_contract import OwnerCommand, RouteCohort, RouteMember
from forge.persistence.models.execution import RunCommand
from forge.persistence.models.scheduling import SubscriptionScheduledTask
from forge.persistence.queries.task_usage_totals import TaskUsageTotalsQuery
from forge.persistence.repositories.commands import PostgresCommandRepository
from forge.persistence.repositories.subscription import SubscriptionConflict
from forge.persistence.unit_of_work import PostgresUnitOfWork
from sqlalchemy import select, text
from subscription_launch_fixture import record_stopped_launch
from test_subscription_delegation_application import delegation_case
from test_subscription_execution_constraints import _admitted
from test_subscription_preparation import preparation_case
from test_subscription_usage import _known, _reservation


@pytest.mark.integration
async def test_post_plan_primary_invocation_preserves_mixed_case_scope(
    session_factory, tmp_path
) -> None:
    factory, _, command, prepare, _ = await preparation_case(
        session_factory, tmp_path, plan_scope=("README.md",)
    )
    async with factory() as work:
        await prepare.execute(command, work)
    executor = SubscriptionDecisionExecutor(factory)
    admission = await executor.admit_next("post-plan-primary", _reservation())
    assert admission is not None and admission.task.owned_paths == ("README.md",)
    async with factory() as work:
        context = await work.subscription_execution.invocation_context(admission)
        assert context.worktree_id
    async with session_factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE subscription_scheduled_tasks SET owned_paths=ARRAY['elsewhere'] WHERE task_id=:task"
            ),
            {"task": admission.task.task_id},
        )
    async with factory() as work:
        with pytest.raises(SubscriptionConflict, match="current context differs"):
            await work.subscription_execution.invocation_context(admission)


@pytest.mark.integration
async def test_checkpoint_interruption_queues_without_repair_debit(
    session_factory,
    persisted_run,
) -> None:
    executor, admission = await _admitted(session_factory, persisted_run)
    unit, checkpoint = uuid4(), uuid4()
    route_digest = RouteCohort.from_members(
        (
            RouteMember(
                role="primary",
                provider="p",
                model="m",
                ordinal=0,
            ),
        )
    ).fingerprint
    async with session_factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO task_usage_work_units (id, project_id, subject_kind, subject_id, run_id, admitted_phase_id, configured_route_digest, configured_route_members, effective_route_digest, effective_route_members, source_kind, source_id, subscription_attempt_id, subscription_task_id, agent_execution_id, schema_version) VALUES (:unit, :project, 'run', :run, :run, 'implement', :digest, CAST(:members AS jsonb), :digest, CAST(:members AS jsonb), 'subscription_attempt', :attempt, :attempt, :task, NULL, 1)"
            ),
            {
                "unit": unit,
                "project": persisted_run.project_id,
                "run": persisted_run.id,
                "digest": route_digest,
                "members": '[{"role":"primary","provider":"p","model":"m","ordinal":0}]',
                "attempt": admission.attempt.attempt_id,
                "task": admission.task.task_id,
            },
        )
        await session.execute(
            text(
                "INSERT INTO task_usage_policy_revisions (project_id, revision, mode, warning_multiplier, checkpoint_multiplier, history_limit, minimum_comparables, actor_id) VALUES (:project, 1, 'checkpoint', 1.5, 2, 50, 20, :actor)"
            ),
            {"project": persisted_run.project_id, "actor": uuid4()},
        )
        await session.execute(
            text(
                "INSERT INTO task_usage_baselines (work_unit_id, project_id, subject_kind, subject_id, policy_revision, history_digest, snapshot_digest, estimator_version, window_limit, minimum_comparables, cohort_digest, frozen_at, schema_version, sample_count, status, reference_tokens, warning_threshold_tokens, checkpoint_threshold_tokens, frozen_payload) VALUES (:unit, :project, 'run', :run, 1, :history, :snapshot, 'median_p90_v1', 50, 20, :cohort, now(), 1, 0, 'manual', 25, 37.5, 50, '{}'::jsonb)"
            ),
            {
                "unit": unit,
                "project": persisted_run.project_id,
                "run": persisted_run.id,
                "history": "c" * 64,
                "snapshot": "b" * 64,
                "cohort": route_digest,
            },
        )
        await session.execute(
            text(
                "INSERT INTO task_usage_observations (id, project_id, work_unit_id, event_id, sequence, counter_kind, scope, input_tokens, final, observed_at, schema_version) VALUES (:id, :project, :unit, 'event', 1, 'cumulative', 'source', 100, false, now(), 1)"
            ),
            {"id": uuid4(), "project": persisted_run.project_id, "unit": unit},
        )
        await session.execute(
            text(
                "INSERT INTO task_usage_checkpoints (id, project_id, work_unit_id, baseline_work_unit_id, subject_kind, subject_id, policy_revision, reference_digest, trigger_event_id, state, version, generation, process_settled) VALUES (:checkpoint, :project, :unit, :unit, 'run', :run, 1, :digest, 'event', 'checkpoint_requested', 0, 1, false)"
            ),
            {
                "checkpoint": checkpoint,
                "project": persisted_run.project_id,
                "unit": unit,
                "run": persisted_run.id,
                "digest": "b" * 64,
            },
        )
    result = SubscriptionInvocationResult(
        attempt=admission.attempt,
        failure=SubscriptionFailure.INTERRUPTED,
        telemetry=_known(input_tokens=5),
    )
    settled = await executor.settle(admission, result)
    assert settled.disposition == "usage_checkpoint_deferred"
    async with PostgresUnitOfWork(session_factory) as work:
        usage = await work.subscription_budget.usage(persisted_run.id)
        assert usage.consumed.provider_attempts == 1
        assert usage.consumed.repairs == 0
        scheduled = await work.session.get(SubscriptionScheduledTask, admission.task.task_id)
        assert scheduled is not None and scheduled.state == "queued" and scheduled.repairs == 0
    sweep = RunTaskUsageMonitor(session_factory, TaskUsageTotalsQuery(session_factory))
    finalized = await sweep.reconcile_final_sources()
    assert len(finalized) == 1 and finalized[0].checkpoint_id == checkpoint
    assert await sweep.reconcile_final_sources() == ()


@pytest.mark.integration
async def test_settled_pending_decision_does_not_prevent_checkpoint_quiescence(
    session_factory,
    persisted_run,
) -> None:
    executor, admission = await _admitted(session_factory, persisted_run)
    async with session_factory() as session, session.begin():
        await session.execute(
            text("UPDATE runs SET state='IMPLEMENTING' WHERE id=:run"), {"run": persisted_run.id}
        )
    launch_proof = await record_stopped_launch(session_factory, admission)
    result = SubscriptionInvocationResult(
        attempt=admission.attempt,
        launch_proof=launch_proof,
        telemetry=_known(input_tokens=5),
        decision=TaskHandoff(
            run_id=persisted_run.id,
            task_id=admission.task.task_id,
            attempt_id=admission.attempt.attempt_id,
            status=HandoffStatus.COMPLETED,
            candidate_tree_digest="a" * 64,
            evidence_receipt_ids=("pending-checkpoint",),
        ),
    )
    assert (await executor.settle(admission, result)).disposition == "decision_pending"
    async with session_factory() as session, session.begin():
        from forge.persistence.repositories.runs import PostgresRunRepository
        from forge.persistence.repositories.subscription_resumption import (
            _pending_decision_is_admissible,
            _source,
        )

        run = await PostgresRunRepository(session).get(persisted_run.id)
        source = await _source(
            session, run, admission.attempt.attempt_id, historical=False, pending_decision=True
        )
        proof = await PostgresRunRepository(session).prove_quiescent(persisted_run.id)
        assert _pending_decision_is_admissible(source, run, phase=run.state), (run.state, proof)
        assert await run_checkpoint_quiescent(session, persisted_run.id)
    async with session_factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE subscription_client_launches SET state='started' WHERE attempt_id=:attempt"
            ),
            {"attempt": admission.attempt.attempt_id},
        )
        assert not await run_checkpoint_quiescent(session, persisted_run.id)
        await session.execute(
            text(
                "UPDATE subscription_client_launches SET state='terminal' WHERE attempt_id=:attempt"
            ),
            {"attempt": admission.attempt.attempt_id},
        )
        await session.execute(
            text(
                "UPDATE subscription_attempt_results SET result_digest=:digest WHERE attempt_id=:attempt"
            ),
            {"digest": "0" * 64, "attempt": admission.attempt.attempt_id},
        )
        assert not await run_checkpoint_quiescent(session, persisted_run.id)


@pytest.mark.integration
async def test_pending_delegation_survives_checkpoint_and_dispatches_after_continuation(
    session_factory,
    tmp_path,
) -> None:
    factory, admission, children, _ = await delegation_case(session_factory, tmp_path)
    run_id = admission.attempt.run_id
    async with session_factory() as session:
        prepare_command_id = await session.scalar(
            select(RunCommand.id).where(
                RunCommand.run_id == run_id,
                RunCommand.command_type == "prepare_worktree",
            )
        )
    assert prepare_command_id is not None
    await PostgresCommandRepository(session_factory).complete(
        prepare_command_id, worker_id="prepare-worker"
    )
    async with factory() as work:
        run = await work.runs.get(run_id)
        attempts_before = (await work.subscription_budget.usage(run_id)).consumed.provider_attempts
    unit, checkpoint = uuid4(), uuid4()
    route_digest = RouteCohort.from_members(
        (
            RouteMember(
                role="primary",
                provider="p",
                model="m",
                ordinal=0,
            ),
        )
    ).fingerprint
    async with session_factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO task_usage_work_units (id, project_id, subject_kind, subject_id, run_id, admitted_phase_id, configured_route_digest, configured_route_members, effective_route_digest, effective_route_members, source_kind, source_id, subscription_attempt_id, subscription_task_id, schema_version) VALUES (:unit, :project, 'run', :run, :run, 'implement', :digest, CAST(:members AS jsonb), :digest, CAST(:members AS jsonb), 'subscription_attempt', :attempt, :attempt, :task, 1)"
            ),
            {
                "unit": unit,
                "project": run.project_id,
                "run": run_id,
                "digest": route_digest,
                "members": '[{"role":"primary","provider":"p","model":"m","ordinal":0}]',
                "attempt": admission.attempt.attempt_id,
                "task": admission.task.task_id,
            },
        )
        await session.execute(
            text(
                "INSERT INTO task_usage_policy_revisions (project_id, revision, mode, warning_multiplier, checkpoint_multiplier, history_limit, minimum_comparables, actor_id) VALUES (:project, 1, 'checkpoint', 1.5, 2, 50, 20, :actor)"
            ),
            {"project": run.project_id, "actor": uuid4()},
        )
        await session.execute(
            text(
                "INSERT INTO task_usage_baselines (work_unit_id, project_id, subject_kind, subject_id, policy_revision, history_digest, snapshot_digest, estimator_version, window_limit, minimum_comparables, cohort_digest, frozen_at, schema_version, sample_count, status, reference_tokens, warning_threshold_tokens, checkpoint_threshold_tokens, frozen_payload) VALUES (:unit, :project, 'run', :run, 1, :history, :snapshot, 'median_p90_v1', 50, 20, :cohort, now(), 1, 0, 'manual', 25, 37.5, 50, '{}'::jsonb)"
            ),
            {
                "unit": unit,
                "project": run.project_id,
                "run": run_id,
                "history": "c" * 64,
                "snapshot": "b" * 64,
                "cohort": route_digest,
            },
        )
        await session.execute(
            text(
                "INSERT INTO task_usage_observations (id, project_id, work_unit_id, event_id, sequence, counter_kind, scope, input_tokens, final, observed_at, schema_version) VALUES (:id, :project, :unit, 'event', 1, 'cumulative', 'source', 100, false, now(), 1)"
            ),
            {"id": uuid4(), "project": run.project_id, "unit": unit},
        )
        await session.execute(
            text(
                "INSERT INTO task_usage_checkpoints (id, project_id, work_unit_id, baseline_work_unit_id, subject_kind, subject_id, policy_revision, reference_digest, trigger_event_id, state, version, generation, process_settled) VALUES (:checkpoint, :project, :unit, :unit, 'run', :run, 1, :digest, 'event', 'paused', 2, 1, true)"
            ),
            {
                "checkpoint": checkpoint,
                "project": run.project_id,
                "unit": unit,
                "run": run_id,
                "digest": "b" * 64,
            },
        )
    application = SubscriptionDecisionApplication(factory)
    with pytest.raises(SubscriptionDecisionError, match="usage checkpoint"):
        await application.apply_delegation(admission.attempt.attempt_id)
    recovery = SubscriptionDecisionRecovery(factory, object())
    assert (await recovery.reconcile_all()).deferred >= 1
    clock = [datetime.now(UTC)]
    reset = clock[0] + timedelta(minutes=10)
    quota_factory = lambda: PostgresUnitOfWork(session_factory, quota_clock=lambda: clock[0])
    key = QuotaPolicy().key_for(children[0].route.effective)
    owner_id = uuid4()
    async with quota_factory() as work:
        quota_before = await work.quota.report_exhaustion(
            key,
            QuotaExhaustion(clock[0], "provider_usage_exhausted", reset),
            idempotency_key="checkpoint-quota",
            actor_id=owner_id,
        )
        await work.commit()
    controls = RunTaskUsageControls(session_factory, TaskUsageTotalsQuery(session_factory))
    receipt, resumed = await controls.apply(
        run_id=run_id,
        actor=AuthenticatedActor(actor_id=owner_id, actor_class="operator", session_id=uuid4()),
        command=OwnerCommand(
            command_id=uuid4(),
            checkpoint_id=checkpoint,
            expected_version=2,
            idempotency_key="continue-with-quota",
            action="continue",
            actor_id=owner_id,
            continue_mode="without_usage_pauses",
        ),
    )
    assert not receipt.replayed and resumed.state.value == "resumed"
    async with quota_factory() as work:
        assert await work.quota.status(key) == quota_before
    assert (await recovery.reconcile_all()).applied >= 1
    assert (await application.apply_delegation(admission.attempt.attempt_id)).replayed
    async with factory() as work:
        usage = await work.subscription_budget.usage(run_id)
        assert usage.consumed.provider_attempts == attempts_before
        assert usage.consumed.repairs == 0
    executor = SubscriptionDecisionExecutor(quota_factory)
    assert await executor.admit_next("quota-still-blocked", _reservation()) is None
    clock[0] = reset
    child = await executor.admit_next("post-checkpoint-child", _reservation())
    assert child is not None
    await executor.settle(
        child,
        SubscriptionInvocationResult(
            attempt=child.attempt, failure=SubscriptionFailure.PROTOCOL, telemetry=_known()
        ),
    )
    async with factory() as work:
        usage = await work.subscription_budget.usage(run_id)
        assert usage.consumed.provider_attempts == attempts_before + 1
        assert usage.consumed.repairs == 0
