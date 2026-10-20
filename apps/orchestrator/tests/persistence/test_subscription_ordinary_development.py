"""Tests for ordinary development workflow path, authority, and invariants."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from forge.application.services.approved_plan import ApprovedPlanLoader
from forge.application.services.subscription_decisions import SubscriptionDecisionApplication
from forge.application.services.subscription_plan_gate import SubscriptionPlanGateService
from forge.domain.approval import ApprovalGate
from forge.domain.run import RunState
from forge.persistence.models import Approval, Run, RunCommand, RunEvent
from forge.persistence.queries.dashboard import DashboardQuery
from sqlalchemy import select
from test_scheduler_acceptance import _remove_disposable_subscription_rows, _route  # noqa: F401
from test_subscription_delegation_application import delegation_case
from test_subscription_plan_gate import proposal_case
from test_subscription_review_selection import selection_case


@pytest.mark.integration
async def test_ordinary_workflow_prepares_plan_internally_and_enqueues_prepare_worktree(
    session_factory, tmp_path
):
    """Ordinary development path (review_plan_first=False) proceeds directly to prepare_worktree."""
    factory, store, evidence, _, _ = await proposal_case(session_factory, tmp_path)
    run_id = evidence.producer.run_id

    # Configure run with ordinary workflow preferences
    async with session_factory() as session:
        run_record = await session.get(Run, run_id)
        assert run_record is not None
        session.add(
            RunEvent(
                run_id=run_id,
                sequence=1,
                run_version=0,
                event_type="run.created",
                actor_class="operator",
                actor_id=uuid4(),
                payload={},
                occurred_at=datetime.now(UTC),
            )
        )
        run_record.workflow = {
            "review_plan_first": False,
            "delegation": "auto",
            "optional_review": "auto",
        }
        await session.commit()

    service = SubscriptionPlanGateService(store, factory)
    outcome = await service.request_settled(evidence.producer.attempt_id)
    assert not outcome.replayed
    assert outcome.run_version == 1

    # Verify run transitioned to PREPARING_WORKTREE without human approval gate
    async with session_factory() as session:
        run_record = await session.get(Run, run_id)
        assert run_record is not None
        assert run_record.state == RunState.PREPARING_WORKTREE.value
        assert run_record.pending_gate is None
        assert run_record.pending_evidence_digest is None

        # Verify Approval record was created with worker actor
        approval = (await session.scalars(select(Approval).where(Approval.run_id == run_id))).one()
        assert approval.gate == "plan"
        assert approval.run_version == 0
        assert approval.policy_version == run_record.policy_version
        assert approval.invalidated_at is None

        # Verify run.plan_internally_prepared event
        events = (
            await session.scalars(
                select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.sequence)
            )
        ).all()
        prep_event = next(e for e in events if e.event_type == "run.plan_internally_prepared")
        assert prep_event.actor_class == "worker"
        assert prep_event.actor_id is None
        assert prep_event.payload["originating_actor_id"] == str(approval.authenticated_actor_id)
        assert prep_event.run_version == 1
        assert prep_event.payload["authority"] == "internal_preparation"
        assert prep_event.payload["approval_id"] == str(approval.id)

        # Verify prepare_worktree command was enqueued
        commands = (
            await session.scalars(select(RunCommand).where(RunCommand.run_id == run_id))
        ).all()
        prep_cmd = next(c for c in commands if c.command_type == "prepare_worktree")
        assert prep_cmd.expected_run_version == 1
        assert prep_cmd.actor_id == approval.authenticated_actor_id

    # Verify ApprovedPlanLoader accepts internal preparation authority
    async with factory() as work:
        loader = ApprovedPlanLoader(store)
        approved = await loader.load(work, run_id)
        assert approved.run.id == run_id
        assert approved.approval_id == approval.id
        assert approved.approval_version == 0
        assert approved.approval_actor_id == approval.authenticated_actor_id

    # Verify dashboard projection projects internal_preparation authority
    dashboard = await DashboardQuery(session_factory).run_projection(run_id)
    assert dashboard is not None
    assert dashboard["plan"]["authority"] == "internal_preparation"
    assert dashboard["run"]["workflow"]["review_plan_first"] is False

    # Verify replay succeeds idempotently
    replay_outcome = await service.request_settled(evidence.producer.attempt_id)
    assert replay_outcome.replayed
    assert replay_outcome.evidence_digest == outcome.evidence_digest


@pytest.mark.integration
async def test_legacy_missing_workflow_awaits_human_plan_approval(session_factory, tmp_path):
    """Missing workflow (historical run) preserves legacy behavior and waits for human approval."""
    factory, store, evidence, _, _ = await proposal_case(session_factory, tmp_path)
    run_id = evidence.producer.run_id

    # Ensure run.workflow is None (legacy)
    async with session_factory() as session:
        run_record = await session.get(Run, run_id)
        assert run_record is not None
        run_record.workflow = None
        await session.commit()

    service = SubscriptionPlanGateService(store, factory)
    outcome = await service.request_settled(evidence.producer.attempt_id)
    assert not outcome.replayed

    # Verify run entered AWAITING_PLAN_APPROVAL
    async with session_factory() as session:
        run_record = await session.get(Run, run_id)
        assert run_record is not None
        assert run_record.state == RunState.AWAITING_PLAN_APPROVAL.value
        assert run_record.pending_gate == ApprovalGate.PLAN.value
        assert run_record.pending_evidence_digest == outcome.evidence_digest

        # Verify no prepare_worktree command was enqueued yet
        prep_cmds = (
            await session.scalars(
                select(RunCommand).where(
                    RunCommand.run_id == run_id,
                    RunCommand.command_type == "prepare_worktree",
                )
            )
        ).all()
        assert len(prep_cmds) == 0


@pytest.mark.integration
async def test_ordinary_workflow_review_plan_first_opt_in_awaits_human_approval(
    session_factory, tmp_path
):
    """Explicit review_plan_first=True waits for human plan approval."""
    factory, store, evidence, _, _ = await proposal_case(session_factory, tmp_path)
    run_id = evidence.producer.run_id

    # Configure run with review_plan_first=True
    async with session_factory() as session:
        run_record = await session.get(Run, run_id)
        assert run_record is not None
        run_record.workflow = {
            "review_plan_first": True,
            "delegation": "auto",
            "optional_review": "auto",
        }
        await session.commit()

    service = SubscriptionPlanGateService(store, factory)
    outcome = await service.request_settled(evidence.producer.attempt_id)
    assert not outcome.replayed

    # Verify run entered AWAITING_PLAN_APPROVAL
    async with session_factory() as session:
        run_record = await session.get(Run, run_id)
        assert run_record is not None
        assert run_record.state == RunState.AWAITING_PLAN_APPROVAL.value
        assert run_record.pending_gate == ApprovalGate.PLAN.value


@pytest.mark.integration
@pytest.mark.parametrize("delegation", ["disabled", "requested"])
async def test_ordinary_workflow_disabled_delegation_rejected(session_factory, tmp_path, delegation):
    """When delegation is disabled in workflow, apply_delegation rejects child tasks."""
    factory, admission, _, _ = await delegation_case(session_factory, tmp_path)
    run_id = admission.task.run_id

    # Set delegation: disabled in run.workflow
    async with session_factory() as session:
        run_record = await session.get(Run, run_id)
        assert run_record is not None
        run_record.workflow = {
            "review_plan_first": False,
            "delegation": delegation,
            "optional_review": "auto",
        }
        await session.commit()

    application = SubscriptionDecisionApplication(factory)
    settlement = await application.apply_delegation(admission.attempt.attempt_id)
    if delegation == "disabled":
        assert not settlement.accepted
        assert settlement.disposition in ("decision_rejected", "decision_repair_queued")
    else:
        assert settlement.accepted and settlement.disposition == "delegated"


@pytest.mark.integration
async def test_ordinary_workflow_optional_review_skip_and_required_enforced(
    session_factory, tmp_path
):
    """optional_review preferences (skip / required) are enforced on ReviewSelection."""
    # 1. Test optional_review = "skip" with review_required=True -> rejected
    p1 = tmp_path / "case1"
    p1.mkdir()
    factory, admission, _ = await selection_case(session_factory, p1, review_required=True)
    run_id = admission.task.run_id
    async with session_factory() as session:
        run_record = await session.get(Run, run_id)
        assert run_record is not None
        run_record.workflow = {
            "review_plan_first": False,
            "delegation": "auto",
            "optional_review": "skip",
        }
        await session.commit()

    application = SubscriptionDecisionApplication(factory)
    settlement = await application.prepare_review_selection(admission.attempt.attempt_id)
    assert not settlement.accepted
    assert settlement.disposition in (
        "decision_rejected",
        "decision_repair_queued",
        "candidate_rejected",
    )

    # 2. Test optional_review = "required" with review_required=False -> rejected
    p2 = tmp_path / "case2"
    p2.mkdir()
    factory2, admission2, _ = await selection_case(session_factory, p2, review_required=False)
    run_id2 = admission2.task.run_id
    async with session_factory() as session:
        run_record2 = await session.get(Run, run_id2)
        assert run_record2 is not None
        run_record2.workflow = {
            "review_plan_first": False,
            "delegation": "auto",
            "optional_review": "required",
        }
        await session.commit()

    application2 = SubscriptionDecisionApplication(factory2)
    settlement2 = await application2.prepare_review_selection(admission2.attempt.attempt_id)
    assert not settlement2.accepted
    assert settlement2.disposition in (
        "decision_rejected",
        "decision_repair_queued",
        "candidate_rejected",
    )

    # 3. Test optional_review = "skip" with review_required=False -> accepted
    p3 = tmp_path / "case3"
    p3.mkdir()
    factory3, admission3, _ = await selection_case(session_factory, p3, review_required=False)
    run_id3 = admission3.task.run_id
    async with session_factory() as session:
        run_record3 = await session.get(Run, run_id3)
        assert run_record3 is not None
        run_record3.workflow = {
            "review_plan_first": False,
            "delegation": "auto",
            "optional_review": "skip",
        }
        await session.commit()

    application3 = SubscriptionDecisionApplication(factory3)
    settlement3 = await application3.prepare_review_selection(admission3.attempt.attempt_id)
    assert settlement3.accepted
    assert settlement3.disposition == "candidate_prepared"


async def ordinary_case(session_factory, tmp_path, *, review_plan_first=True, questions=()):
    from forge.application.services.auth import AuthenticatedActor
    from forge.domain.event import RunEvent as DomainEvent
    from forge.domain.workflow import WorkflowPreferences

    factory, store, evidence, _plan, validator = await proposal_case(
        session_factory,
        tmp_path,
        plan_scope=("apps",),
        material_questions=questions,
    )
    actor = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())
    prefs = WorkflowPreferences(review_plan_first=review_plan_first)
    async with factory() as work:
        run = await work.runs.get(evidence.producer.run_id)
        await work.runs.update_workflow(run.id, run.version, prefs)
        await work.events.append(
            DomainEvent(
                run_id=run.id,
                run_version=0,
                event_type="run.created",
                actor_class="operator",
                actor_id=actor.actor_id,
                payload={},
            )
        )
        await work.commit()
    outcome = await SubscriptionPlanGateService(store, factory).request_settled(
        evidence.producer.attempt_id
    )
    return factory, store, evidence, validator, actor, prefs, outcome


@pytest.mark.integration
@pytest.mark.parametrize("control", [None, "pause", "cancel"])
async def test_plan_opt_out_preserves_control_and_continues_exactly_once(
    session_factory, tmp_path, control
):
    from forge.application.services.runs import RunService
    from forge.domain.workflow import WorkflowPreferences
    from forge.persistence.repositories.runs import ConcurrencyConflict

    factory, store, evidence, validator, actor, prefs, outcome = await ordinary_case(
        session_factory, tmp_path
    )
    run_id = evidence.producer.run_id
    if control:
        async with factory() as work:
            await work.commands.enqueue(
                run_id=run_id,
                command_type=control,
                idempotency_key=f"{run_id}:{control}",
                payload={},
                expected_run_version=outcome.run_version,
                actor_id=actor.actor_id,
            )
            await work.commit()
    service = RunService(factory, plan_evidence_validator=validator)
    changed = await service.update_workflow(
        actor=actor,
        run_id=run_id,
        idempotency_key="opt-out",
        expected_run_version=outcome.run_version,
        expected_workflow=prefs,
        workflow=WorkflowPreferences(),
    )
    assert changed.state is (
        RunState.AWAITING_PLAN_APPROVAL if control else RunState.PREPARING_WORKTREE
    )
    assert changed.version == outcome.run_version + (0 if control else 1)
    assert (
        await service.update_workflow(
            actor=actor,
            run_id=run_id,
            idempotency_key="opt-out",
            expected_run_version=outcome.run_version,
            expected_workflow=prefs,
            workflow=WorkflowPreferences(),
        )
        == changed
    )
    with pytest.raises(ConcurrencyConflict):
        await service.update_workflow(
            actor=actor,
            run_id=run_id,
            idempotency_key="stale-opt-out",
            expected_run_version=outcome.run_version,
            expected_workflow=prefs,
            workflow=WorkflowPreferences(delegation="disabled"),
        )
    if control:
        from forge.application.handlers.continue_plan import ContinuePlanHandler
        from forge.application.handlers.run_controls import (
            CancelRunHandler,
            PauseRunHandler,
            ResumeRunHandler,
        )
        from forge.application.ports.commands import CommandLane
        from forge.persistence.repositories.commands import PostgresCommandRepository

        commands = PostgresCommandRepository(session_factory)
        command = await commands.claim_next(
            worker_id="control", lease_seconds=120, lane=CommandLane.CONTROL
        )
        async with factory() as work:
            await (PauseRunHandler() if control == "pause" else CancelRunHandler())(command, work)
        await commands.complete(command.id, worker_id=command.lease_owner)
        async with factory() as work:
            current = await work.runs.get(run_id)
            assert current.state is (RunState.PAUSED if control == "pause" else RunState.CANCELLED)
            if control == "pause":
                # A paused preference edit retains the causal pause version.
                edited = await work.runs.update_workflow(
                    run_id, current.version, WorkflowPreferences(delegation="disabled")
                )
                assert edited.version == current.version
                await work.commands.enqueue(
                    run_id=run_id,
                    command_type="resume",
                    payload={},
                    idempotency_key="resume-opt-out",
                    expected_run_version=current.version,
                    actor_id=actor.actor_id,
                )
            await work.commit()
        if control == "pause":
            resume = await commands.claim_next(worker_id="resume", lease_seconds=120)
            for _ in range(2):
                async with factory() as work:
                    await ResumeRunHandler(artifact_store=store)(resume, work)
            await commands.complete(resume.id, worker_id=resume.lease_owner)
            continuation = await commands.claim_next(worker_id="continue", lease_seconds=120)
            assert continuation.command_type == "continue_plan"
            for _ in range(2):
                async with factory() as work:
                    await ContinuePlanHandler(validator, store)(continuation, work)
            async with factory() as work:
                assert (await work.runs.get(run_id)).state is RunState.PREPARING_WORKTREE


@pytest.mark.integration
async def test_material_questions_block_opt_out_and_human_approval(session_factory, tmp_path):
    from forge.application.services.approvals import AuthorizationError
    from forge.application.services.runs import RunService
    from forge.domain.workflow import WorkflowPreferences
    from test_subscription_plan_gate import approve_proposal

    factory, _, evidence, validator, actor, prefs, outcome = await ordinary_case(
        session_factory, tmp_path, questions=("Which retention period is required?",)
    )
    changed = await RunService(factory, plan_evidence_validator=validator).update_workflow(
        actor=actor,
        run_id=evidence.producer.run_id,
        idempotency_key="questions-opt-out",
        expected_run_version=outcome.run_version,
        expected_workflow=prefs,
        workflow=WorkflowPreferences(),
    )
    assert (
        changed.state is RunState.AWAITING_PLAN_APPROVAL and changed.version == outcome.run_version
    )
    with pytest.raises(AuthorizationError, match="answer material questions"):
        await approve_proposal(factory, session_factory, evidence, validator, outcome)


@pytest.mark.integration
async def test_internal_authority_survives_history_window_and_later_preferences(
    session_factory, tmp_path
):
    from forge.domain.event import RunEvent as DomainEvent
    from forge.domain.workflow import WorkflowPreferences

    factory, store, evidence, _, _, _, _ = await ordinary_case(
        session_factory, tmp_path, review_plan_first=False
    )
    async with factory() as work:
        run = await work.runs.get(evidence.producer.run_id)
        await work.runs.update_workflow(
            run.id, run.version, WorkflowPreferences(review_plan_first=True)
        )
        for _ in range(55):
            await work.events.append(
                DomainEvent(
                    run_id=run.id,
                    run_version=run.version,
                    event_type="run.observed",
                    actor_class="worker",
                    payload={},
                )
            )
        await work.commit()
    projection = await DashboardQuery(session_factory).run_projection(run.id)
    assert projection["plan"]["authority"] == "internal_preparation"
    assert projection["run"]["workflow"]["review_plan_first"] is True
    async with factory() as work:
        loaded = await ApprovedPlanLoader(store).load(work, run.id)
        assert loaded.evidence.producer == evidence.producer


@pytest.mark.integration
async def test_ordinary_primary_reaches_candidate_and_validation_without_specialist(
    session_factory, tmp_path, monkeypatch
):
    from functools import partial

    import test_subscription_review_selection as selection_module
    from forge.domain.subscription import SpecialistPurpose
    from forge.persistence.models.subscription import SubscriptionTask
    from test_subscription_acceptance_dispatch import dispatch_case
    from test_subscription_preparation import preparation_case

    monkeypatch.setattr(
        selection_module, "preparation_case", partial(preparation_case, ordinary=True)
    )
    factory, proposal, service, _ = await dispatch_case(session_factory, tmp_path)
    result = await service.apply(proposal.attempt_id)
    assert result.accepted and result.disposition == "acceptance_validation_queued"
    async with factory() as work:
        run = await work.runs.get(proposal.decision.run_id)
        assert run.workflow is not None and run.state is RunState.VALIDATING
        primary = await work.subscription.get_task(run.id, proposal.decision.task_id)
        assert primary.parent_task_id is None and primary.purpose is SpecialistPurpose.PRIMARY
        tasks = (
            await work.session.scalars(
                select(SubscriptionTask).where(SubscriptionTask.run_id == run.id)
            )
        ).all()
        assert len(tasks) == 1
        approvals = (
            await work.session.scalars(select(Approval).where(Approval.run_id == run.id))
        ).all()
        assert len(approvals) == 1 and approvals[0].gate == "plan"
        assert run.pending_gate is None


@pytest.mark.integration
async def test_ordinary_failed_check_repairs_same_primary_before_local_publication_gate(
    session_factory, tmp_path, monkeypatch
):
    from functools import partial

    import test_subscription_review_selection as selection_module
    from test_subscription_preparation import preparation_case
    from test_subscription_validation_repaired_candidate import (
        test_retry_of_transient_check_failure_requires_new_acceptance,
    )

    monkeypatch.setattr(
        selection_module, "preparation_case", partial(preparation_case, ordinary=True)
    )
    await test_retry_of_transient_check_failure_requires_new_acceptance(session_factory, tmp_path)


@pytest.mark.integration
async def test_owner_answer_revises_material_question_plan_without_granting_old_gate(
    session_factory, tmp_path
):
    from datetime import timedelta

    from forge.application.services.plan_revision import PlanRevisionService
    from forge.domain.command import CommandEnvelope, CommandStatus

    factory, store, evidence, validator, actor, _, outcome = await ordinary_case(
        session_factory, tmp_path, review_plan_first=False, questions=("Which retention period?",)
    )
    revision = CommandEnvelope(
        id=uuid4(),
        run_id=evidence.producer.run_id,
        command_type="request_plan_revision",
        idempotency_key="answer-question",
        payload={"feedback": "Use 30 days. Revise the plan with that answer."},
        status=CommandStatus.LEASED,
        expected_run_version=outcome.run_version,
        actor_id=actor.actor_id,
        payload_schema_version=1,
        attempt=1,
        available_at=datetime.now(UTC),
        lease_owner="revision-test",
        lease_expires_at=datetime.now(UTC) + timedelta(seconds=120),
    )
    async with factory() as work:
        await PlanRevisionService(store, validator).execute(revision, work)
    async with factory() as work:
        run = await work.runs.get(evidence.producer.run_id)
        assert run.state is RunState.PLANNING and run.pending_gate is None
        assert run.workflow is not None and not run.workflow.review_plan_first
        assert not (
            await work.session.scalars(select(Approval).where(Approval.run_id == run.id))
        ).all()


@pytest.mark.integration
@pytest.mark.parametrize("action", ["continue", "stop"])
async def test_checkpoint_fences_opt_out_until_durable_owner_continue(
    session_factory, tmp_path, action
):
    from forge.application.handlers.continue_plan import ContinuePlanHandler
    from forge.application.services.runs import RunService
    from forge.application.services.task_usage_controls import RunTaskUsageControls
    from forge.domain.task_usage_contract import (
        CheckpointRecord,
        CheckpointState,
        OwnerCommand,
        RouteCohort,
        RouteMember,
        SourceBinding,
        SubjectKey,
        WorkUnitBinding,
    )
    from forge.domain.workflow import WorkflowPreferences
    from forge.persistence.models.task_usage import (
        TaskUsageBaseline,
        TaskUsageObservation,
        TaskUsagePolicyRevision,
    )
    from forge.persistence.queries.task_usage_totals import TaskUsageTotalsQuery
    from forge.persistence.repositories.commands import PostgresCommandRepository
    from forge.persistence.repositories.task_usage_checkpoints import (
        PostgresTaskUsageCheckpointRepository,
    )
    from forge.persistence.repositories.task_usage_observations import PostgresTaskUsageObservations

    factory, store, evidence, validator, actor, prefs, _outcome = await ordinary_case(
        session_factory, tmp_path
    )
    async with factory() as work:
        run = await work.runs.get(evidence.producer.run_id)
    cohort = RouteCohort.from_members(
        (RouteMember(role="primary", provider="test", model="test", ordinal=0),)
    )
    binding = WorkUnitBinding(
        subject=SubjectKey(project_id=run.project_id, kind="run", subject_id=run.id),
        work_unit_id=uuid4(),
        admitted_phase_id="planning",
        configured_route=cohort,
        effective_route=cohort,
        source=SourceBinding(
            kind="subscription_attempt",
            source_id=evidence.producer.attempt_id,
            parent_id=evidence.producer.task_id,
        ),
    )
    await PostgresTaskUsageObservations(session_factory).bind(binding)
    async with session_factory() as session:
        session.add(
            TaskUsagePolicyRevision(
                project_id=run.project_id,
                revision=1,
                mode="checkpoint",
                warning_multiplier=1.5,
                checkpoint_multiplier=2,
                history_limit=50,
                minimum_comparables=20,
                actor_id=actor.actor_id,
            )
        )
        await session.flush()
        session.add(
            TaskUsageObservation(
                id=uuid4(),
                project_id=run.project_id,
                work_unit_id=binding.work_unit_id,
                event_id="usage-trigger",
                sequence=1,
                counter_kind="cumulative",
                scope="source",
                input_tokens=100,
                final=False,
                observed_at=datetime.now(UTC),
                schema_version=1,
            )
        )
        session.add(
            TaskUsageBaseline(
                work_unit_id=binding.work_unit_id,
                project_id=run.project_id,
                subject_kind="run",
                subject_id=run.id,
                policy_revision=1,
                history_digest="d" * 64,
                snapshot_digest="e" * 64,
                estimator_version="median_p90_v1",
                window_limit=50,
                minimum_comparables=20,
                cohort_digest=cohort.fingerprint,
                frozen_at=datetime.now(UTC),
                schema_version=1,
                sample_count=0,
                status="manual",
                reference_tokens=25,
                warning_threshold_tokens=37.5,
                checkpoint_threshold_tokens=50,
                frozen_payload={},
            )
        )
        await session.commit()
        repository = PostgresTaskUsageCheckpointRepository(session)
        checkpoint = await repository.request(
            CheckpointRecord(
                checkpoint_id=uuid4(),
                binding=binding,
                baseline_binding=binding,
                state=CheckpointState.REQUESTED,
                policy_revision=1,
                reference_digest="e" * 64,
                trigger_event_id="usage-trigger",
            )
        )
        await repository.transition(
            checkpoint.checkpoint_id,
            expected_version=0,
            state=CheckpointState.PAUSING,
            process_settled=False,
        )
        checkpoint = await repository.transition(
            checkpoint.checkpoint_id,
            expected_version=1,
            state=CheckpointState.PAUSED,
            process_settled=True,
        )
        await session.commit()
    changed = await RunService(factory, plan_evidence_validator=validator).update_workflow(
        actor=actor,
        run_id=run.id,
        idempotency_key="checkpoint-opt-out",
        expected_run_version=run.version,
        expected_workflow=prefs,
        workflow=WorkflowPreferences(),
    )
    assert changed.state is RunState.AWAITING_PLAN_APPROVAL and changed.version == run.version
    command = OwnerCommand(
        command_id=uuid4(),
        checkpoint_id=checkpoint.checkpoint_id,
        expected_version=checkpoint.version,
        idempotency_key="checkpoint-owner",
        action=action,
        actor_id=actor.actor_id,
        continue_mode="without_usage_pauses" if action == "continue" else None,
    )
    controls = RunTaskUsageControls(session_factory, TaskUsageTotalsQuery(session_factory))
    await controls.apply(run_id=run.id, actor=actor, command=command)
    await controls.apply(run_id=run.id, actor=actor, command=command)
    commands = PostgresCommandRepository(session_factory)
    from forge.application.ports.commands import CommandLane

    continuation = await commands.claim_next(
        worker_id="checkpoint-continue",
        lease_seconds=120,
        lane=CommandLane.NORMAL if action == "continue" else CommandLane.CONTROL,
    )
    assert continuation.command_type == ("continue_plan" if action == "continue" else "cancel")
    async with factory() as work:
        if action == "continue":
            await ContinuePlanHandler(validator, store)(continuation, work)
        else:
            from forge.application.handlers.run_controls import CancelRunHandler

            await CancelRunHandler()(continuation, work)
    async with factory() as work:
        current = await work.runs.get(run.id)
        assert current.state is (
            RunState.PREPARING_WORKTREE if action == "continue" else RunState.CANCELLED
        )
        assert len(
            (
                await work.session.scalars(
                    select(RunCommand).where(
                        RunCommand.run_id == run.id, RunCommand.command_type == "continue_plan"
                    )
                )
            ).all()
        ) == (1 if action == "continue" else 0)


@pytest.mark.integration
async def test_continuation_accepts_renewal_but_rejects_substituted_authority(
    session_factory, tmp_path
):
    from dataclasses import replace

    from forge.application.handlers.continue_plan import ContinuePlanHandler
    from forge.application.ports.commands import CommandLeaseLost, CommandRecoveryRequired
    from forge.domain.workflow import WorkflowPreferences
    from forge.persistence.repositories.commands import PostgresCommandRepository

    factory, store, evidence, validator, actor, _, _ = await ordinary_case(
        session_factory, tmp_path
    )
    async with factory() as work:
        run = await work.runs.get(evidence.producer.run_id)
        await work.runs.update_workflow(run.id, run.version, WorkflowPreferences())
        await work.commands.enqueue(
            run_id=run.id,
            command_type="continue_plan",
            idempotency_key="renewed-continuation",
            payload={},
            expected_run_version=run.version,
            actor_id=actor.actor_id,
        )
        await work.commit()
    commands = PostgresCommandRepository(session_factory)
    command = await commands.claim_next(worker_id="continuation-renewal", lease_seconds=60)
    renewed = await commands.renew(command.id, worker_id=command.lease_owner, lease_seconds=120)
    assert renewed.lease_expires_at > command.lease_expires_at
    for forged in (
        replace(command, actor_id=uuid4()),
        replace(command, expected_run_version=command.expected_run_version + 1),
        replace(command, payload={"changed": True}),
    ):
        with pytest.raises((CommandRecoveryRequired, CommandLeaseLost)):
            async with factory() as work:
                await ContinuePlanHandler(validator, store)(forged, work)
    for _ in range(2):
        async with factory() as work:
            await ContinuePlanHandler(validator, store)(command, work)
    async with factory() as work:
        assert (await work.runs.get(run.id)).state is RunState.PREPARING_WORKTREE


@pytest.mark.integration
async def test_preference_update_preserves_preparation_and_committed_resume_replay(
    session_factory, tmp_path
):
    from forge.application.handlers.run_controls import ResumeRunHandler
    from forge.application.services.runs import RunService
    from forge.domain.workflow import WorkflowPreferences
    from forge.persistence.repositories.commands import PostgresCommandRepository
    from test_subscription_preparation import preparation_case
    from test_subscription_resume_controls import pause_for_resume

    factory, _evidence, preparation, service, _ = await preparation_case(
        session_factory, tmp_path, ordinary=True
    )
    run_service = RunService(factory)
    changed = await run_service.update_workflow(
        actor=type("Actor", (), {"actor_id": uuid4()})(),
        run_id=preparation.run_id,
        idempotency_key="queued-preparation-prefs",
        expected_run_version=preparation.expected_run_version,
        expected_workflow=WorkflowPreferences(),
        workflow=WorkflowPreferences(review_plan_first=True),
    )
    assert changed.version == preparation.expected_run_version
    async with factory() as work:
        await service.execute(preparation, work)
    await PostgresCommandRepository(session_factory).complete(
        preparation.id, worker_id=preparation.lease_owner
    )
    _commands, resume = await pause_for_resume(factory, session_factory, preparation.run_id)
    async with factory() as work:
        await ResumeRunHandler(artifact_store=service._approved_plans._artifacts)(resume, work)
    async with factory() as work:
        restored = await work.runs.get(preparation.run_id)
    await run_service.update_workflow(
        actor=type("Actor", (), {"actor_id": resume.actor_id})(),
        run_id=restored.id,
        idempotency_key="after-committed-resume",
        expected_run_version=restored.version,
        expected_workflow=restored.workflow,
        workflow=WorkflowPreferences(delegation="disabled"),
    )
    async with factory() as work:
        await ResumeRunHandler(artifact_store=service._approved_plans._artifacts)(resume, work)
