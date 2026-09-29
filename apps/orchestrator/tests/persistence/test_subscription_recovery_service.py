"""Coupled approved preparation, invalid primary result, and guarded recovery."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from forge.agents.subscription_protocol import ProtocolError, decode_final, output_schema
from forge.application.ports.subscription_gateway import SubscriptionInvocationResult
from forge.application.services.auth import AuthenticatedActor
from forge.application.services.subscription_decision_recovery import SubscriptionDecisionRecovery
from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
from forge.application.services.subscription_recovery import SubscriptionRecoveryService
from forge.application.services.subscription_requests import SubscriptionRequestBuilder
from forge.artifacts.filesystem import FilesystemArtifactStore
from forge.domain.operation import canonical_digest
from forge.domain.plan import PlanOutput
from forge.domain.subscription import (
    AcceptanceCriterion,
    AttemptTelemetry,
    HandoffStatus,
    LogicalTaskContract,
    TaskBudget,
    TaskHandoff,
    UnknownTelemetryPolicy,
    decode_subscription_record,
    encode_subscription_record,
)
from forge.domain.subscription_recovery import (
    RecoveryAction,
    RecoveryApplyRequest,
    RecoveryPreviewRequest,
)
from forge.persistence.models.execution import Approval, RunEvent
from forge.persistence.models.project import Project, ProjectPolicyVersion
from forge.persistence.models.scheduling import SubscriptionScheduledTask, SubscriptionSchedulerRun
from forge.persistence.models.subscription import (
    SubscriptionAttempt,
    SubscriptionClientLaunch,
    SubscriptionTask,
)
from forge.persistence.models.subscription_quota import SubscriptionQuotaPool
from forge.persistence.models.subscription_recovery import (
    SubscriptionApplicationDiagnostic,
    SubscriptionContractRevision,
    SubscriptionRecoveryReceipt,
    SubscriptionRecoveryWorker,
)
from forge.persistence.models.subscription_results import (
    SubscriptionAttemptResult,
    SubscriptionRepairDebit,
)
from forge.persistence.repositories.subscription_recovery import RecoveryConflict
from sqlalchemy import select, update
from sqlalchemy.exc import DBAPIError
from subscription_launch_fixture import record_stopped_launch
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_preparation import preparation_case
from test_subscription_usage import _reservation


async def recovery_case(
    session_factory, tmp_path, *, incident_decision="handoff", defer_settlement=False
):
    """Return a disposable historical defect after the complete approval flow."""
    factory, evidence, command, preparation, _ = await preparation_case(
        session_factory,
        tmp_path,
        plan_scope=("apps",),
        primary_budget=TaskBudget(
            max_provider_attempts=5,
            max_repairs=3,
            unknown_telemetry_policy=UnknownTelemetryPolicy(max_uncertain_attempts=5),
        ),
    )
    async with factory() as work:
        await preparation.execute(command, work)
    admission = await SubscriptionDecisionExecutor(factory).admit_next(
        "legacy-primary", _reservation()
    )
    assert admission is not None
    async with factory() as work:
        task = await work.session.get(SubscriptionTask, evidence.producer.task_id)
        assert task is not None
        prepared = decode_subscription_record(task.payload)
        assert isinstance(prepared, LogicalTaskContract)
        # Emulate the older preparation path: approved paths/checks were copied,
        # while the planning-only objective remained. No revision existed then.
        stale = replace(
            prepared,
            typed_acceptance=(
                AcceptanceCriterion(
                    criterion_id="approved-plan",
                    description="Produce an evidence-bound plan for the requested task.",
                ),
            ),
            untrusted_context_refs=prepared.untrusted_context_refs[:-1],
        )
        task.payload = encode_subscription_record(stale)
        admitted = await work.session.get(SubscriptionAttempt, admission.attempt.attempt_id)
        assert admitted is not None
        admitted.task_digest = canonical_digest(task.payload)
        revision = await work.session.scalar(
            select(SubscriptionContractRevision).where(
                SubscriptionContractRevision.task_id == task.id
            )
        )
        assert revision is not None
        await work.session.delete(revision)
        work.session.add(
            SubscriptionRecoveryWorker(
                worker_id=f"fixture-{uuid4()}",
                contract_version=1,
                observed_at=datetime.now(UTC),
            )
        )
        await work.commit()
    admission = replace(admission, task=stale)
    launch_proof = await record_stopped_launch(session_factory, admission)
    decision = (
        PlanOutput(
            summary="Another plan rather than implementation",
            assumptions=(),
            affected_components=("apps",),
            steps=("Plan implementation again",),
            required_checks=("unit",),
            risks=("No code change",),
            security_considerations=(),
            dependency_changes=(),
        )
        if incident_decision == "plan"
        else TaskHandoff(
            run_id=admission.attempt.run_id,
            task_id=admission.attempt.task_id,
            attempt_id=admission.attempt.attempt_id,
            status=HandoffStatus.COMPLETED,
            summary="A second plan without implementation or validation",
            candidate_tree_digest="a" * 64,
            evidence_receipt_ids=("receipt",),
        )
    )
    result = SubscriptionInvocationResult(
        attempt=admission.attempt,
        decision=decision,
        telemetry=AttemptTelemetry(input_tokens=10, output_tokens=5, duration_ms=100),
        launch_proof=launch_proof,
    )
    settled = False

    async def settle():
        nonlocal settled
        if settled:
            raise AssertionError("recovery fixture settlement was already applied")
        settled = True
        settlement = await SubscriptionDecisionExecutor(factory).settle(admission, result)
        assert settlement.disposition == "role_rejected"
        return settlement

    case = {
        "run_id": admission.attempt.run_id,
        "task_id": admission.attempt.task_id,
        "attempt_id": admission.attempt.attempt_id,
        "factory": factory,
    }
    if defer_settlement:
        return {**case, "settle": settle}
    await settle()
    return case


async def inject_stale_contract(factory, admission):
    async with factory() as work:
        task = await work.session.get(SubscriptionTask, admission.task.task_id)
        prepared = decode_subscription_record(task.payload)
        stale = replace(
            prepared,
            typed_acceptance=(AcceptanceCriterion(
                criterion_id="approved-plan", description="Produce an evidence-bound plan."
            ),),
            untrusted_context_refs=prepared.untrusted_context_refs[:-1],
        )
        task.payload = encode_subscription_record(stale)
        attempt = await work.session.get(SubscriptionAttempt, admission.attempt.attempt_id)
        attempt.task_digest = canonical_digest(task.payload)
        revision = await work.session.scalar(select(SubscriptionContractRevision).where(
            SubscriptionContractRevision.task_id == task.id
        ))
        await work.session.delete(revision)
        work.session.add(SubscriptionRecoveryWorker(
            worker_id=f"stale-continuation-{uuid4()}", contract_version=1,
            observed_at=datetime.now(UTC),
        ))
        await work.commit()
    return replace(admission, task=stale)


async def stale_live_admission(session_factory, tmp_path, *, defer_stale=False):
    """Admit a primary under approval, optionally retaining prepared instructions first."""
    factory, evidence, command, preparation, _ = await preparation_case(
        session_factory, tmp_path, plan_scope=("apps",),
        primary_budget=TaskBudget(max_provider_attempts=8, max_repairs=3),
    )
    async with factory() as work:
        await preparation.execute(command, work)
    from forge.persistence.repositories.commands import PostgresCommandRepository

    await PostgresCommandRepository(session_factory).complete(
        command.id, worker_id=command.lease_owner
    )
    executor = SubscriptionDecisionExecutor(factory)
    admission = await executor.admit_next("historical-primary", _reservation())
    assert admission is not None
    if not defer_stale:
        admission = await inject_stale_contract(factory, admission)
    return factory, evidence, admission, preparation


@pytest.mark.integration
@pytest.mark.parametrize("receipt_mutation", ["valid", "missing", "mismatched"])
async def test_paused_stale_primary_contract_repair_uses_verified_resume_receipt(
    session_factory, tmp_path, receipt_mutation
):
    from forge.application.handlers.run_controls import ResumeRunHandler
    from forge.application.ports.subscription_gateway import SubscriptionFailure
    from test_subscription_resume_controls import pause_for_resume
    from test_subscription_usage import _known

    factory, _, admission, preparation = await stale_live_admission(session_factory, tmp_path)
    executor = SubscriptionDecisionExecutor(factory)
    proof = await record_stopped_launch(session_factory, admission)
    commands, resume = await pause_for_resume(factory, session_factory, admission.task.run_id)
    settled = await executor.settle(admission, SubscriptionInvocationResult(
        attempt=admission.attempt, failure=SubscriptionFailure.INTERRUPTED,
        telemetry=_known(), launch_proof=proof,
    ))
    assert settled.disposition == "stale"
    async with factory() as work:
        await ResumeRunHandler(artifact_store=preparation._approved_plans._artifacts)(resume, work)
    await commands.complete(resume.id, worker_id=resume.lease_owner)
    async with factory() as work:
        result = await work.session.get(SubscriptionAttemptResult, admission.attempt.attempt_id)
        original = result.result_payload, result.result_digest, result.application_payload
        assert result.disposition == "stale" and not result.accepted
        assert result.application_payload["kind"] == "paused_subscription_attempt"
        debit = await work.session.get(SubscriptionRepairDebit, admission.attempt.attempt_id)
        assert debit is not None
        if receipt_mutation == "missing":
            from sqlalchemy import null, update

            await work.session.execute(
                update(SubscriptionAttemptResult)
                .where(SubscriptionAttemptResult.attempt_id == admission.attempt.attempt_id)
                .values(application_payload=null(), application_digest=None)
            )
        elif receipt_mutation == "mismatched":
            result.application_payload = {**result.application_payload, "repairs": 99}
            result.application_digest = canonical_digest(result.application_payload)
        await work.commit()
    assert await executor.admit_next("blocked-stale-primary", _reservation()) is None
    actor = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())
    service = SubscriptionRecoveryService(factory)
    preview = await service.preview(
        run_id=admission.task.run_id, task_id=admission.task.task_id,
        attempt_id=admission.attempt.attempt_id, actor=actor,
        request=RecoveryPreviewRequest(action=RecoveryAction.REPAIR_APPROVED_PLAN_CONTRACT),
    )
    if receipt_mutation != "valid":
        assert not preview.eligible
        assert preview.reason_code == "not_stale_plan"
        return
    assert preview.eligible, preview.reason_code
    assert preview.budget_impact.repair_units == preview.budget_impact.provider_attempts == 0
    receipt = await service.apply(
        run_id=admission.task.run_id, task_id=admission.task.task_id,
        attempt_id=admission.attempt.attempt_id, actor=actor, idempotency_key="paused-queued",
        request=RecoveryApplyRequest(
            action=preview.action, preview_token=preview.preview_token,
            reason="Correct resumed approved instructions",
        ),
    )
    assert receipt.status == "applied"
    async with factory() as work:
        result = await work.session.get(SubscriptionAttemptResult, admission.attempt.attempt_id)
        assert (result.result_payload, result.result_digest, result.application_payload) == original
        preserved_debit = await work.session.get(
            SubscriptionRepairDebit, admission.attempt.attempt_id
        )
        assert preserved_debit is not None
        assert preserved_debit.next_attempt_id == debit.next_attempt_id
    continuation = await executor.admit_next("corrected-primary", _reservation())
    assert continuation is not None and continuation.task.task_id == admission.task.task_id
    assert continuation.task.typed_acceptance[0].criterion_id == "approved-implementation"


@pytest.mark.integration
@pytest.mark.parametrize("record_mutation", ["valid", "missing"])
async def test_woken_delegated_stale_primary_contract_repair_keeps_coordination(
    session_factory, tmp_path, record_mutation
):
    from forge.application.ports.subscription_gateway import SubscriptionFailure
    from forge.application.services.subscription_decisions import SubscriptionDecisionApplication
    from forge.domain.subscription import DelegateDecision, SpecialistPurpose
    from test_subscription_usage import _known

    factory, _, admission, _ = await stale_live_admission(session_factory, tmp_path)
    child = LogicalTaskContract(
        run_id=admission.task.run_id, task_id=uuid4(), parent_task_id=admission.task.task_id,
        purpose=SpecialistPurpose.ROUTINE_IMPLEMENTATION,
        route=admission.envelope.route_for(SpecialistPurpose.ROUTINE_IMPLEMENTATION),
        budget=_reservation(), max_repairs=0, owned_paths=("apps/feature",),
        typed_acceptance=(AcceptanceCriterion(
            criterion_id="behavior", description="Implement the approved behavior"
        ),),
    )
    decision = DelegateDecision(
        run_id=admission.task.run_id, parent_task_id=admission.task.task_id,
        child_tasks=(child,), rationale="Bounded work",
    )
    executor = SubscriptionDecisionExecutor(factory)
    assert (await executor.settle(admission, SubscriptionInvocationResult(
        attempt=admission.attempt, decision=decision, telemetry=_known(),
        launch_proof=await record_stopped_launch(session_factory, admission),
    ))).disposition == "decision_pending"
    assert (await SubscriptionDecisionApplication(factory).apply_delegation(
        admission.attempt.attempt_id
    )).disposition == "delegated"
    child_admission = await executor.admit_next("delegated-child", _reservation())
    assert child_admission is not None and child_admission.task.task_id == child.task_id
    assert (await executor.settle(child_admission, SubscriptionInvocationResult(
        attempt=child_admission.attempt, failure=SubscriptionFailure.PROTOCOL,
        telemetry=_known(),
    ))).disposition == "failed"
    assert await executor.admit_next("blocked-stale-parent", _reservation()) is None
    if record_mutation == "missing":
        from forge.persistence.models.subscription import SubscriptionDecisionRecord

        async with factory() as work:
            record = await work.session.scalar(select(SubscriptionDecisionRecord).where(
                SubscriptionDecisionRecord.attempt_id == admission.attempt.attempt_id
            ))
            assert record is not None
            await work.session.delete(record)
            await work.commit()
    actor = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())
    service = SubscriptionRecoveryService(factory)
    preview = await service.preview(
        run_id=admission.task.run_id, task_id=admission.task.task_id,
        attempt_id=admission.attempt.attempt_id, actor=actor,
        request=RecoveryPreviewRequest(action=RecoveryAction.REPAIR_APPROVED_PLAN_CONTRACT),
    )
    if record_mutation == "missing":
        assert not preview.eligible and preview.reason_code == "not_stale_plan"
        return
    assert preview.eligible, preview.reason_code
    assert preview.budget_impact.repair_units == preview.budget_impact.provider_attempts == 0
    await service.apply(
        run_id=admission.task.run_id, task_id=admission.task.task_id,
        attempt_id=admission.attempt.attempt_id, actor=actor,
        idempotency_key="woken-delegation-contract",
        request=RecoveryApplyRequest(
            action=preview.action, preview_token=preview.preview_token,
            reason="Correct woken approved instructions",
        ),
    )
    continued = await executor.admit_next("corrected-parent", _reservation())
    assert continued is not None and continued.task.task_id == admission.task.task_id
    assert continued.task.typed_acceptance[0].criterion_id == "approved-implementation"


@pytest.mark.integration
async def test_woken_wait_stale_primary_contract_repair_keeps_selected_child(
    session_factory, tmp_path
):
    from forge.application.ports.subscription_gateway import SubscriptionFailure
    from forge.application.services.subscription_decisions import SubscriptionDecisionApplication
    from forge.domain.subscription import DelegateDecision, SpecialistPurpose, WaitDecision
    from test_subscription_usage import _known

    factory, _, admission, _ = await stale_live_admission(
        session_factory, tmp_path, defer_stale=True
    )
    children = tuple(LogicalTaskContract(
        run_id=admission.task.run_id, task_id=uuid4(), parent_task_id=admission.task.task_id,
        purpose=SpecialistPurpose.ROUTINE_IMPLEMENTATION,
        route=admission.envelope.route_for(SpecialistPurpose.ROUTINE_IMPLEMENTATION),
        budget=_reservation(), max_repairs=0, owned_paths=(f"apps/child-{index}",),
        typed_acceptance=(AcceptanceCriterion(
            criterion_id="behavior", description="Implement the approved behavior"
        ),),
    ) for index in range(2))
    executor = SubscriptionDecisionExecutor(factory)
    application = SubscriptionDecisionApplication(factory)
    await executor.settle(admission, SubscriptionInvocationResult(
        attempt=admission.attempt,
        decision=DelegateDecision(
            run_id=admission.task.run_id, parent_task_id=admission.task.task_id,
            child_tasks=children, rationale="Bounded work",
        ), telemetry=_known(),
        launch_proof=await record_stopped_launch(session_factory, admission),
    ))
    assert (await application.apply_delegation(admission.attempt.attempt_id)).accepted
    first = await executor.admit_next("first-child", _reservation())
    assert first is not None and first.task.task_id in {child.task_id for child in children}
    await executor.settle(first, SubscriptionInvocationResult(
        attempt=first.attempt, failure=SubscriptionFailure.PROTOCOL, telemetry=_known(),
    ))
    selected = next(child for child in children if child.task_id != first.task.task_id)
    resumed_parent = await executor.admit_next("primary-waits", _reservation())
    chosen = None
    if resumed_parent is not None and resumed_parent.task.task_id == selected.task_id:
        chosen = resumed_parent
        resumed_parent = await executor.admit_next("primary-waits-next", _reservation())
    assert resumed_parent is not None and resumed_parent.task.task_id == admission.task.task_id
    resumed_parent = await inject_stale_contract(factory, resumed_parent)
    await executor.settle(resumed_parent, SubscriptionInvocationResult(
        attempt=resumed_parent.attempt,
        decision=WaitDecision(
            run_id=admission.task.run_id, task_id=admission.task.task_id,
            waiting_on_task_ids=(selected.task_id,), reason="Wait for selected child",
        ), telemetry=_known(),
        launch_proof=await record_stopped_launch(session_factory, resumed_parent),
    ))
    assert (await application.apply_wait(resumed_parent.attempt.attempt_id)).accepted
    chosen = chosen or await executor.admit_next("selected-child", _reservation())
    assert chosen is not None and chosen.task.task_id == selected.task_id
    await executor.settle(chosen, SubscriptionInvocationResult(
        attempt=chosen.attempt, failure=SubscriptionFailure.PROTOCOL, telemetry=_known(),
    ))
    assert await executor.admit_next("blocked-stale-wait", _reservation()) is None
    actor = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())
    service = SubscriptionRecoveryService(factory)
    preview = await service.preview(
        run_id=admission.task.run_id, task_id=admission.task.task_id,
        attempt_id=resumed_parent.attempt.attempt_id, actor=actor,
        request=RecoveryPreviewRequest(action=RecoveryAction.REPAIR_APPROVED_PLAN_CONTRACT),
    )
    assert preview.eligible, preview.reason_code
    assert preview.budget_impact.repair_units == 0
    await service.apply(
        run_id=admission.task.run_id, task_id=admission.task.task_id,
        attempt_id=resumed_parent.attempt.attempt_id, actor=actor,
        idempotency_key="woken-wait-contract", request=RecoveryApplyRequest(
            action=preview.action, preview_token=preview.preview_token,
            reason="Correct woken approved instructions",
        ),
    )
    continued = await executor.admit_next("corrected-wait-parent", _reservation())
    assert continued is not None and continued.task.task_id == admission.task.task_id
    assert continued.task.typed_acceptance[0].criterion_id == "approved-implementation"


@pytest.mark.integration
@pytest.mark.parametrize(
    "wake", ["scope_request", "operator_feedback", "operator_feedback_missing_record"]
)
async def test_queued_stale_delegation_repairs_after_nonterminal_wake(
    session_factory, tmp_path, wake
):
    from forge.application.services.subscription_decisions import SubscriptionDecisionApplication
    from forge.application.services.subscription_feedback import SubscriptionTaskFeedbackService
    from forge.domain.subscription import DelegateDecision, ScopeRequestDecision, SpecialistPurpose
    from forge.domain.subscription_feedback import SubscriptionTaskFeedbackRequest
    from test_subscription_usage import _known

    factory, _, admission, _ = await stale_live_admission(session_factory, tmp_path)
    child = LogicalTaskContract(
        run_id=admission.task.run_id, task_id=uuid4(), parent_task_id=admission.task.task_id,
        purpose=SpecialistPurpose.ROUTINE_IMPLEMENTATION,
        route=admission.envelope.route_for(SpecialistPurpose.ROUTINE_IMPLEMENTATION),
        budget=_reservation(), max_repairs=0, owned_paths=("apps/feature",),
        typed_acceptance=(AcceptanceCriterion(
            criterion_id="behavior", description="Implement the approved behavior"
        ),),
    )
    executor = SubscriptionDecisionExecutor(factory)
    application = SubscriptionDecisionApplication(factory)
    await executor.settle(admission, SubscriptionInvocationResult(
        attempt=admission.attempt, decision=DelegateDecision(
            run_id=admission.task.run_id, parent_task_id=admission.task.task_id,
            child_tasks=(child,), rationale="Bounded implementation",
        ), telemetry=_known(),
        launch_proof=await record_stopped_launch(session_factory, admission),
    ))
    assert (await application.apply_delegation(admission.attempt.attempt_id)).accepted
    if wake == "scope_request":
        worker = await executor.admit_next("scope-worker", _reservation())
        assert worker is not None and worker.task.task_id == child.task_id
        await executor.settle(worker, SubscriptionInvocationResult(
            attempt=worker.attempt, decision=ScopeRequestDecision(
                run_id=child.run_id, task_id=child.task_id,
                requested_paths=("apps/shared",), reason="Need shared interface",
            ), telemetry=_known(),
            launch_proof=await record_stopped_launch(session_factory, worker),
        ))
        assert (await application.apply_scope_request(worker.attempt.attempt_id)).accepted
    else:
        async with factory() as work:
            run = await work.runs.get(child.run_id)
            child_row = await work.session.get(SubscriptionTask, child.task_id)
            run_version, child_version = run.version, child_row.version
        feedback = await SubscriptionTaskFeedbackService(factory).submit(
            run_id=child.run_id, task_id=child.task_id,
            actor=AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4()),
            idempotency_key="wake-stale-primary", request=SubscriptionTaskFeedbackRequest(
                expected_run_version=run_version, expected_task_version=child_version,
                feedback="Keep the existing implementation and address the shared interface.",
            ),
        )
        assert feedback.status == "pending_primary"
    async with factory() as work:
        parent = await work.session.get(SubscriptionTask, admission.task.task_id)
        scheduled = await work.session.get(SubscriptionScheduledTask, parent.id)
        assert parent.state == scheduled.state == "queued"
        original_result = await work.session.get(SubscriptionAttemptResult, admission.attempt.attempt_id)
        original = original_result.result_payload, original_result.result_digest, original_result.application_payload
        usage_before = await work.subscription_budget.usage(child.run_id)
    assert await executor.admit_next("blocked-stale-wake", _reservation()) is None
    if wake == "operator_feedback_missing_record":
        from forge.persistence.models.subscription import SubscriptionDecisionRecord

        async with factory() as work:
            record = await work.session.scalar(select(SubscriptionDecisionRecord).where(
                SubscriptionDecisionRecord.attempt_id == admission.attempt.attempt_id
            ))
            assert record is not None
            await work.session.delete(record)
            await work.commit()
    actor = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())
    service = SubscriptionRecoveryService(factory)
    preview = await service.preview(
        run_id=child.run_id, task_id=admission.task.task_id,
        attempt_id=admission.attempt.attempt_id, actor=actor,
        request=RecoveryPreviewRequest(action=RecoveryAction.REPAIR_APPROVED_PLAN_CONTRACT),
    )
    if wake == "operator_feedback_missing_record":
        assert not preview.eligible and preview.reason_code == "not_stale_plan"
        return
    assert preview.eligible, preview.reason_code
    assert preview.budget_impact.repair_units == preview.budget_impact.provider_attempts == 0
    receipt = await service.apply(
        run_id=child.run_id, task_id=admission.task.task_id,
        attempt_id=admission.attempt.attempt_id, actor=actor,
        idempotency_key=f"repair-{wake}", request=RecoveryApplyRequest(
            action=preview.action, preview_token=preview.preview_token,
            reason="Correct approved instructions after wake",
        ),
    )
    assert receipt.status == "applied"
    async with factory() as work:
        result = await work.session.get(SubscriptionAttemptResult, admission.attempt.attempt_id)
        assert (result.result_payload, result.result_digest, result.application_payload) == original
        assert (await work.subscription_budget.usage(child.run_id)).consumed == usage_before.consumed
        if wake == "scope_request":
            scope_result = await work.session.get(
                SubscriptionAttemptResult, worker.attempt.attempt_id
            )
            assert scope_result.accepted and scope_result.disposition == "scope_requested"
            assert (await work.session.get(SubscriptionTask, child.task_id)).state == "blocked"
        else:
            from forge.persistence.models.subscription_feedback import SubscriptionTaskFeedback

            stored_feedback = await work.session.get(SubscriptionTaskFeedback, feedback.receipt_id)
            assert stored_feedback is not None and stored_feedback.state == "pending_primary"
    continued = await executor.admit_next("corrected-wake-parent", _reservation())
    assert continued is not None and continued.task.task_id == admission.task.task_id
    assert continued.task.typed_acceptance[0].criterion_id == "approved-implementation"


@pytest.mark.integration
@pytest.mark.parametrize("interference", ["none", "parent_proof_missing", "uncertain_launch"])
async def test_exhausted_handoff_observation_allows_one_fresh_child_step(
    session_factory, tmp_path, monkeypatch, interference
):
    from forge.application.ports.worktrees import GitWorkingTreeSnapshot
    from forge.application.services.subscription_handoff_application import (
        SubscriptionHandoffApplication,
    )
    from forge.persistence.models.subscription_handoff import SubscriptionHandoffFence
    from test_subscription_handoff_application import application_case

    factory, parent, child, application, observation, proof = await application_case(
        session_factory, tmp_path, monkeypatch, repairs=2,
        primary_budget=TaskBudget(max_provider_attempts=8, max_repairs=4),
    )
    assert await application.release_handoff_observation(observation)

    async def expire_observation(proposal):
        async with factory() as work:
            fence = await work.session.get(
                SubscriptionHandoffFence, proposal.worktree.identity.worktree_name
            )
            fence.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await work.commit()
        return GitWorkingTreeSnapshot(
            head_sha=proposal.worktree.base_sha, base_sha=proposal.worktree.base_sha,
            files=(), changed_paths=(),
        )

    class ExistingProof:
        async def assess(self, *args, **kwargs):
            return proof

    handoffs = SubscriptionHandoffApplication(factory, ExistingProof(), expire_observation)
    recovery = SubscriptionDecisionRecovery(
        factory, FilesystemArtifactStore(tmp_path / "artifacts"), handoffs=handoffs
    )
    for index in range(4):
        assert (await recovery.reconcile_all()).deferred == 1
        if index < 3:
            async with factory() as work:
                diagnostic = await work.session.get(
                    SubscriptionApplicationDiagnostic, child.attempt.attempt_id
                )
                assert diagnostic.resolution == "scheduled"
                diagnostic.next_retry_at = datetime.now(UTC) - timedelta(seconds=1)
                await work.commit()
    async with factory() as work:
        diagnostic = await work.session.get(
            SubscriptionApplicationDiagnostic, child.attempt.attempt_id
        )
        assert diagnostic.classification == "prerequisite"
        assert diagnostic.reason_code == "handoff_observation_changed"
        assert diagnostic.resolution == "attention"
        result = await work.session.get(SubscriptionAttemptResult, child.attempt.attempt_id)
        original = result.result_payload, result.result_digest
        assert result.disposition == "decision_pending" and result.application_payload is None
        work.session.add(SubscriptionRecoveryWorker(
            worker_id=f"handoff-recovery-{uuid4()}", contract_version=1,
            observed_at=datetime.now(UTC),
        ))
        if interference == "parent_proof_missing":
            from forge.persistence.models.subscription import SubscriptionDecisionRecord

            parent_record = await work.session.scalar(select(SubscriptionDecisionRecord).where(
                SubscriptionDecisionRecord.attempt_id == parent.attempt.attempt_id
            ))
            assert parent_record is not None
            await work.session.delete(parent_record)
        elif interference == "uncertain_launch":
            launch = await work.session.scalar(select(SubscriptionClientLaunch).where(
                SubscriptionClientLaunch.attempt_id == child.attempt.attempt_id
            ))
            assert launch is not None
            launch.state = "uncertain"
        await work.commit()
    actor = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())
    service = SubscriptionRecoveryService(factory)
    preview = await service.preview(
        run_id=child.task.run_id, task_id=child.task.task_id,
        attempt_id=child.attempt.attempt_id, actor=actor,
        request=RecoveryPreviewRequest(action=RecoveryAction.REJECT_AND_RETRY_STEP),
    )
    if interference != "none":
        assert not preview.eligible
        assert preview.reason_code == (
            "source_changed" if interference == "parent_proof_missing" else "effect_uncertain"
        )
        return
    assert preview.eligible, preview.reason_code
    request = RecoveryApplyRequest(
        action=preview.action, preview_token=preview.preview_token,
        reason="Discard expired observation and obtain fresh child evidence",
    )
    receipt = await service.apply(
        run_id=child.task.run_id, task_id=child.task.task_id,
        attempt_id=child.attempt.attempt_id, actor=actor,
        idempotency_key="expired-observation", request=request,
    )
    assert receipt.status == "applied"
    assert await service.apply(
        run_id=child.task.run_id, task_id=child.task.task_id,
        attempt_id=child.attempt.attempt_id, actor=actor,
        idempotency_key="expired-observation", request=request,
    ) == receipt
    async with factory() as work:
        result = await work.session.get(SubscriptionAttemptResult, child.attempt.attempt_id)
        assert (result.result_payload, result.result_digest) == original
        assert await work.session.get(SubscriptionRepairDebit, child.attempt.attempt_id)
        assert len((await work.session.scalars(select(SubscriptionRecoveryReceipt))).all()) == 1
        parent_row = await work.session.get(SubscriptionTask, parent.task.task_id)
        assert parent_row.state == "blocked"
    continued = await SubscriptionDecisionExecutor(factory).admit_next(
        "fresh-child", _reservation()
    )
    assert continued is not None and continued.task.task_id == child.task.task_id


@pytest.mark.integration
@pytest.mark.parametrize(
    ("ordering", "interference"), [
        ("during", "none"), ("before", "none"), ("earlier", "none"),
        ("during", "missing_wait"), ("during", "unknown_wait"),
        ("during", "foreign_wait"), ("during", "parent_woken"),
        ("during", "parent_admitted"), ("during", "partial_application"),
    ]
)
async def test_exhausted_handoff_under_current_wait_snapshot(
    session_factory, tmp_path, monkeypatch, ordering, interference
):
    from forge.application.ports.subscription_gateway import SubscriptionFailure
    from forge.application.ports.subscription_handoff import (
        HandoffCallProof,
        VerifiedSubscriptionHandoff,
    )
    from forge.application.ports.worktrees import GitWorkingTreeSnapshot
    from forge.application.services.subscription_decisions import SubscriptionDecisionApplication
    from forge.application.services.subscription_feedback import SubscriptionTaskFeedbackService
    from forge.application.services.subscription_handoff_application import (
        SubscriptionHandoffApplication,
    )
    from forge.domain.subscription import HandoffStatus, TaskHandoff, WaitDecision
    from forge.domain.subscription_feedback import SubscriptionTaskFeedbackRequest
    from forge.persistence.models.subscription_handoff import SubscriptionHandoffFence
    from forge.persistence.repositories.subscription_handoff_evidence import (
        PostgresSubscriptionHandoffEvidence,
    )
    from test_subscription_delegation_application import delegation_case
    from test_subscription_usage import _known

    def pair(child, _):
        if ordering == "before":
            companion = replace(child, task_id=uuid4(), owned_paths=("apps/companion",),
                                max_repairs=0)
            target = replace(child, task_id=uuid4(), owned_paths=("apps/second",),
                             dependency_task_ids=(companion.task_id,), max_repairs=2,
                             budget=replace(child.budget, max_provider_attempts=3, max_repairs=2))
            return replace(child, owned_paths=("apps/first",), max_repairs=0), companion, target
        return (
            replace(child, owned_paths=("apps/first",), max_repairs=0),
            replace(child, task_id=uuid4(), owned_paths=("apps/second",), max_repairs=2,
                    budget=replace(child.budget, max_provider_attempts=3, max_repairs=2)),
        )

    factory, parent, children, _ = await delegation_case(
        session_factory, tmp_path, pair,
        primary_budget=TaskBudget(max_provider_attempts=8, max_repairs=4),
    )
    application = SubscriptionDecisionApplication(factory)
    assert (await application.apply_delegation(parent.attempt.attempt_id)).accepted
    executor = SubscriptionDecisionExecutor(factory)
    admissions = (
        await executor.admit_next("first-child", _reservation()),
        await executor.admit_next("second-child", _reservation()),
    )
    assert all(admission is not None for admission in admissions)
    first = next(admission for admission in admissions if admission.task.task_id == children[0].task_id)
    if ordering == "before":
        assert {admission.task.task_id for admission in admissions} == {
            children[0].task_id, children[1].task_id,
        }
        companion = next(
            admission for admission in admissions if admission.task.task_id == children[1].task_id
        )
        second = None
    else:
        assert {admission.task.task_id for admission in admissions} == {
            child.task_id for child in children
        }
        second = next(
            admission for admission in admissions if admission.task.task_id == children[-1].task_id
        )
    assert (await executor.settle(first, SubscriptionInvocationResult(
        attempt=first.attempt, failure=SubscriptionFailure.PROTOCOL, telemetry=_known(),
    ))).disposition == "failed"
    waiting_parent = await executor.admit_next("parent-waits-for-running-child", _reservation())
    assert waiting_parent is not None and waiting_parent.task.task_id == parent.task.task_id
    await executor.settle(waiting_parent, SubscriptionInvocationResult(
        attempt=waiting_parent.attempt,
        decision=WaitDecision(
            run_id=parent.task.run_id, task_id=parent.task.task_id,
            waiting_on_task_ids=(children[-1].task_id,), reason="Wait for selected worker",
        ), telemetry=_known(),
        launch_proof=await record_stopped_launch(session_factory, waiting_parent),
    ))
    assert (await application.apply_wait(waiting_parent.attempt.attempt_id)).accepted
    if ordering == "before":
        assert (await executor.settle(companion, SubscriptionInvocationResult(
            attempt=companion.attempt, failure=SubscriptionFailure.PROTOCOL,
            telemetry=_known(),
        ))).disposition == "failed"
        second = await executor.admit_next("selected-after-wait", _reservation())
        assert second is not None and second.task.task_id == children[-1].task_id
    elif ordering == "earlier":
        assert (await executor.settle(second, SubscriptionInvocationResult(
            attempt=second.attempt, failure=SubscriptionFailure.PROTOCOL,
            telemetry=_known(),
        ))).disposition == "repair_queued"
        previous_second_id = second.attempt.attempt_id
        second = await executor.admit_next("selected-after-repair", _reservation())
        assert second is not None and second.task.task_id == children[-1].task_id
        assert second.attempt.attempt_id != previous_second_id
        from forge.persistence.repositories.subscription_recovery import (
            PostgresSubscriptionRecoveryRepository,
        )

        async with factory() as work:
            prior_attempt = await work.session.get(SubscriptionAttempt, previous_second_id)
            assert prior_attempt is not None
            assert not await PostgresSubscriptionRecoveryRepository(
                work.session
            )._wait_snapshot_allows(
                str(second.attempt.attempt_id), prior_attempt, matching_current=True
            )
    evidence_id = uuid4()
    handoff = TaskHandoff(
        run_id=second.task.run_id, task_id=second.task.task_id,
        attempt_id=second.attempt.attempt_id, status=HandoffStatus.COMPLETED,
        candidate_tree_digest="a" * 64, evidence_receipt_ids=(str(evidence_id),),
        summary="Ready for evidence verification",
    )
    await executor.settle(second, SubscriptionInvocationResult(
        attempt=second.attempt, decision=handoff, telemetry=_known(),
        launch_proof=await record_stopped_launch(session_factory, second),
    ))
    observation = await application.begin_handoff_observation(second.attempt.attempt_id, uuid4())
    assert await application.release_handoff_observation(observation)
    proof = VerifiedSubscriptionHandoff(
        run_id=second.task.run_id, task_id=second.task.task_id,
        attempt_id=second.attempt.attempt_id, snapshot_call_id=evidence_id,
        manifest_digest="d" * 64, candidate_tree_digest=handoff.candidate_tree_digest,
        policy_version=second.envelope.safety_policy_version,
        task_digest=canonical_digest(encode_subscription_record(second.task)),
        handoff_digest=canonical_digest(encode_subscription_record(handoff)),
        call_proofs=(HandoffCallProof(evidence_id, "e" * 64, "f" * 64),),
        artifact_proofs=(("d" * 64, "1" * 64),), checks_match_snapshot=True,
        output_digest="2" * 64, current_tree_digest="3" * 64,
    )

    async def verified(self, supplied):
        return True

    monkeypatch.setattr(PostgresSubscriptionHandoffEvidence, "verify", verified)

    async def expire(proposal):
        async with factory() as work:
            fence = await work.session.get(
                SubscriptionHandoffFence, proposal.worktree.identity.worktree_name
            )
            fence.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await work.commit()
        return GitWorkingTreeSnapshot(
            head_sha=proposal.worktree.base_sha, base_sha=proposal.worktree.base_sha,
            files=(), changed_paths=(),
        )

    class ExistingProof:
        async def assess(self, *args, **kwargs):
            return proof

    recovery = SubscriptionDecisionRecovery(
        factory, FilesystemArtifactStore(tmp_path / "artifacts"),
        handoffs=SubscriptionHandoffApplication(factory, ExistingProof(), expire),
    )
    for index in range(4):
        assert (await recovery.reconcile_all()).deferred == 1
        if index < 3:
            async with factory() as work:
                diagnostic = await work.session.get(
                    SubscriptionApplicationDiagnostic, second.attempt.attempt_id
                )
                diagnostic.next_retry_at = datetime.now(UTC) - timedelta(seconds=1)
                await work.commit()
    async with factory() as work:
        wait_result = await work.session.get(
            SubscriptionAttemptResult, waiting_parent.attempt.attempt_id
        )
        prior_debits = len((await work.session.scalars(select(SubscriptionRepairDebit))).all())
        snapshot_id = wait_result.application_payload["child_attempt_ids"][str(second.task.task_id)]
        assert snapshot_id == (
            None if ordering == "before" else
            str(previous_second_id) if ordering == "earlier" else
            str(second.attempt.attempt_id)
        )
        work.session.add(SubscriptionRecoveryWorker(
            worker_id=f"wait-current-{uuid4()}", contract_version=1,
            observed_at=datetime.now(UTC),
        ))
        if interference == "missing_wait":
            from sqlalchemy import null, update

            await work.session.execute(update(SubscriptionAttemptResult).where(
                SubscriptionAttemptResult.attempt_id == waiting_parent.attempt.attempt_id
            ).values(application_payload=null(), application_digest=None))
        elif interference in {"unknown_wait", "foreign_wait"}:
            receipt = dict(wait_result.application_payload)
            receipt["child_attempt_ids"] = {
                **receipt["child_attempt_ids"],
                str(second.task.task_id): str(
                    uuid4() if interference == "unknown_wait" else first.attempt.attempt_id
                ),
            }
            wait_result.application_payload = receipt
            wait_result.application_digest = canonical_digest(receipt)
        elif interference == "partial_application":
            child_result = await work.session.get(
                SubscriptionAttemptResult, second.attempt.attempt_id
            )
            child_result.application_payload = {"kind": "partial_handoff"}
            child_result.application_digest = canonical_digest(child_result.application_payload)
        await work.commit()
    if interference in {"parent_woken", "parent_admitted"}:
        async with factory() as work:
            run = await work.runs.get(second.task.run_id)
            child_row = await work.session.get(SubscriptionTask, second.task.task_id)
            run_version, child_version = run.version, child_row.version
        await SubscriptionTaskFeedbackService(factory).submit(
            run_id=second.task.run_id, task_id=second.task.task_id,
            actor=AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4()),
            idempotency_key="wake-current-wait", request=SubscriptionTaskFeedbackRequest(
                expected_run_version=run_version, expected_task_version=child_version,
                feedback="Return this evidence to the primary before another worker step.",
            ),
        )
        if interference == "parent_admitted":
            admitted_parent = await executor.admit_next("admitted-before-child-recovery", _reservation())
            assert admitted_parent is not None and admitted_parent.task.task_id == parent.task.task_id
    actor = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())
    service = SubscriptionRecoveryService(factory)
    preview = await service.preview(
        run_id=second.task.run_id, task_id=second.task.task_id,
        attempt_id=second.attempt.attempt_id, actor=actor,
        request=RecoveryPreviewRequest(action=RecoveryAction.REJECT_AND_RETRY_STEP),
    )
    if interference != "none":
        assert not preview.eligible
        return
    assert preview.eligible, preview.reason_code
    request = RecoveryApplyRequest(
        action=preview.action, preview_token=preview.preview_token,
        reason="Replace exhausted observation for the active waited-on child",
    )
    receipt = await service.apply(
        run_id=second.task.run_id, task_id=second.task.task_id,
        attempt_id=second.attempt.attempt_id, actor=actor,
        idempotency_key="wait-current-retry", request=request,
    )
    assert await service.apply(
        run_id=second.task.run_id, task_id=second.task.task_id,
        attempt_id=second.attempt.attempt_id, actor=actor,
        idempotency_key="wait-current-retry", request=request,
    ) == receipt
    async with factory() as work:
        assert len((await work.session.scalars(select(SubscriptionRecoveryReceipt))).all()) == 1
        assert len((await work.session.scalars(select(SubscriptionRepairDebit))).all()) == prior_debits + 1
        assert await work.session.get(SubscriptionRepairDebit, second.attempt.attempt_id)
        parent_row = await work.session.get(SubscriptionTask, parent.task.task_id)
        assert parent_row.state == "blocked"
    next_admission = await executor.admit_next("retried-wait-child", _reservation())
    assert next_admission is not None and next_admission.task.task_id == second.task.task_id


@pytest.mark.integration
async def test_role_rejection_attention_event_updates_current_projection(session_factory, tmp_path):
    from forge.persistence.queries.dashboard import DashboardQuery

    case = await recovery_case(session_factory, tmp_path, defer_settlement=True)
    projection = await DashboardQuery(session_factory).run_projection(case["run_id"])
    assert projection is not None and not projection["subscription_recovery_attention"]
    await case["settle"]()
    projection = await DashboardQuery(session_factory).run_projection(case["run_id"])
    assert projection is not None and projection["subscription_recovery_attention"]
    async with case["factory"]() as work:
        events = (await work.session.scalars(select(RunEvent).where(
            RunEvent.run_id == case["run_id"],
            RunEvent.event_type == "run.subscription_recovery_attention_changed",
        ))).all()
        assert len(events) == 1
        assert events[0].payload == {
            "task_id": str(case["task_id"]), "attempt_id": str(case["attempt_id"]),
            "attention": True, "reason_code": "approved_plan_contract_stale",
        }


@pytest.mark.integration
async def test_stale_approved_plan_with_plan_output_offers_explicit_contract_repair(
    session_factory, tmp_path
):
    case = await recovery_case(session_factory, tmp_path, incident_decision="plan")
    actor = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())
    service = SubscriptionRecoveryService(case["factory"])
    preview = await service.preview(
        run_id=case["run_id"],
        task_id=case["task_id"],
        attempt_id=case["attempt_id"],
        actor=actor,
        request=RecoveryPreviewRequest(action=RecoveryAction.REPAIR_APPROVED_PLAN_CONTRACT),
    )
    assert preview.eligible, preview.reason_code
    wrong_action = await service.preview(
        run_id=case["run_id"],
        task_id=case["task_id"],
        attempt_id=case["attempt_id"],
        actor=actor,
        request=RecoveryPreviewRequest(action=RecoveryAction.REJECT_AND_RETRY_STEP),
    )
    assert not wrong_action.eligible and wrong_action.reason_code == "invalid_result"
    await service.apply(
        run_id=case["run_id"],
        task_id=case["task_id"],
        attempt_id=case["attempt_id"],
        actor=actor,
        idempotency_key="plan-output-repair",
        request=RecoveryApplyRequest(
            action=preview.action,
            preview_token=preview.preview_token,
            reason="Correct the stale implementation objective",
        ),
    )
    async with case["factory"]() as work:
        task = await work.session.get(SubscriptionTask, case["task_id"])
        contract = decode_subscription_record(task.payload)
        assert contract.typed_acceptance[0].criterion_id == "approved-implementation"


@pytest.mark.integration
async def test_approved_preparation_builds_implementation_request_with_role_safe_schema(
    session_factory, tmp_path
):
    factory, _, command, preparation, _ = await preparation_case(
        session_factory, tmp_path, plan_scope=("apps",)
    )
    async with factory() as work:
        await preparation.execute(command, work)
    admission = await SubscriptionDecisionExecutor(factory).admit_next(
        "modern-primary", _reservation()
    )
    assert admission is not None
    request = await SubscriptionRequestBuilder(factory).build(admission)
    assert request.task.typed_acceptance[0].criterion_id == "approved-implementation"
    context = request.untrusted_context["approved_implementation"]
    assert context is not None
    assert context["plan_digest"] and context["approval_id"]
    choices = output_schema(request)["properties"]["decision"]["anyOf"]
    kinds = {choice["properties"]["kind"]["const"] for choice in choices}
    assert "handoff" not in kinds and "scope_request" not in kinds
    with pytest.raises(ProtocolError):
        decode_final({"kind": "handoff"}, request)


@pytest.mark.integration
async def test_repair_preview_is_read_only_and_apply_is_idempotent(session_factory, tmp_path):
    case = await recovery_case(session_factory, tmp_path)
    actor = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())
    service = SubscriptionRecoveryService(case["factory"])
    preview = await service.preview(
        run_id=case["run_id"],
        task_id=case["task_id"],
        attempt_id=case["attempt_id"],
        actor=actor,
        request=RecoveryPreviewRequest(action=RecoveryAction.REPAIR_APPROVED_PLAN_CONTRACT),
    )
    assert preview.eligible, preview.reason_code
    async with case["factory"]() as work:
        result = await work.session.get(SubscriptionAttemptResult, case["attempt_id"])
        assert result.disposition == "role_rejected"
        assert await work.session.get(SubscriptionRepairDebit, case["attempt_id"]) is None
        assert (
            await work.session.scalar(
                select(SubscriptionContractRevision).where(
                    SubscriptionContractRevision.task_id == case["task_id"]
                )
            )
            is None
        )
    request = RecoveryApplyRequest(
        action=RecoveryAction.REPAIR_APPROVED_PLAN_CONTRACT,
        preview_token=preview.preview_token,
        reason="Recover the approved implementation",
    )
    receipt = await service.apply(
        run_id=case["run_id"],
        task_id=case["task_id"],
        attempt_id=case["attempt_id"],
        actor=actor,
        idempotency_key="repair-1",
        request=request,
    )
    replay = await service.apply(
        run_id=case["run_id"],
        task_id=case["task_id"],
        attempt_id=case["attempt_id"],
        actor=actor,
        idempotency_key="repair-1",
        request=request,
    )
    assert receipt == replay and receipt.status == "applied"
    async with case["factory"]() as work:
        task = await work.session.get(SubscriptionTask, case["task_id"])
        contract = decode_subscription_record(task.payload)
        assert isinstance(contract, LogicalTaskContract)
        assert contract.typed_acceptance[0].criterion_id == "approved-implementation"
        assert await work.session.get(SubscriptionRepairDebit, case["attempt_id"]) is not None


@pytest.mark.integration
async def test_preview_signature_actor_and_source_version_are_bound(session_factory, tmp_path):
    case = await recovery_case(session_factory, tmp_path)
    actor = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())
    other = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())
    service = SubscriptionRecoveryService(case["factory"])
    preview = await service.preview(
        run_id=case["run_id"],
        task_id=case["task_id"],
        attempt_id=case["attempt_id"],
        actor=actor,
        request=RecoveryPreviewRequest(action=RecoveryAction.REPAIR_APPROVED_PLAN_CONTRACT),
    )
    request = RecoveryApplyRequest(
        action=preview.action,
        preview_token=preview.preview_token,
        reason="Repair the approved plan objective",
    )

    async def apply(actor, request, key):
        return await service.apply(
            run_id=case["run_id"],
            task_id=case["task_id"],
            attempt_id=case["attempt_id"],
            actor=actor,
            idempotency_key=key,
            request=request,
        )

    with pytest.raises(RecoveryConflict):
        await apply(other, request, "wrong-actor")
    with pytest.raises(RecoveryConflict):
        await apply(
            actor,
            request.model_copy(
                update={
                    "preview_token": preview.preview_token[:-1]
                    + ("0" if preview.preview_token[-1] != "0" else "1")
                }
            ),
            "forged",
        )
    async with case["factory"]() as work:
        task = await work.session.get(SubscriptionTask, case["task_id"])
        task.version += 1
        await work.commit()
    with pytest.raises(RecoveryConflict):
        await apply(actor, request, "stale")
    async with case["factory"]() as work:
        assert await work.session.scalar(select(SubscriptionRecoveryReceipt.id)) is None
        assert await work.session.get(SubscriptionRepairDebit, case["attempt_id"]) is None


@pytest.mark.integration
async def test_pause_and_worker_expiry_prevent_recovery(session_factory, tmp_path):
    case = await recovery_case(session_factory, tmp_path)
    actor = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())
    service = SubscriptionRecoveryService(case["factory"])
    request = RecoveryPreviewRequest(action=RecoveryAction.REPAIR_APPROVED_PLAN_CONTRACT)
    preview = await service.preview(
        run_id=case["run_id"],
        task_id=case["task_id"],
        attempt_id=case["attempt_id"],
        actor=actor,
        request=request,
    )
    assert preview.eligible
    async with case["factory"]() as work:
        task = await work.session.get(SubscriptionTask, case["task_id"])
        task.pause_requested = True
        await work.commit()
    with pytest.raises(RecoveryConflict):
        await service.apply(
            run_id=case["run_id"],
            task_id=case["task_id"],
            attempt_id=case["attempt_id"],
            actor=actor,
            idempotency_key="paused",
            request=RecoveryApplyRequest(
                action=request.action,
                preview_token=preview.preview_token,
                reason="Pause took precedence",
            ),
        )
    async with case["factory"]() as work:
        task = await work.session.get(SubscriptionTask, case["task_id"])
        task.pause_requested = False
        workers = (await work.session.scalars(select(SubscriptionRecoveryWorker))).all()
        for worker in workers:
            worker.observed_at = datetime.now(UTC) - timedelta(minutes=5)
        await work.commit()
    blocked = await service.preview(
        run_id=case["run_id"],
        task_id=case["task_id"],
        attempt_id=case["attempt_id"],
        actor=actor,
        request=request,
    )
    assert not blocked.eligible and blocked.reason_code == "worker_unavailable"


@pytest.mark.integration
async def test_recovery_key_concurrency_debits_only_once(session_factory, tmp_path):
    import asyncio

    case = await recovery_case(session_factory, tmp_path)
    actor = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())
    service = SubscriptionRecoveryService(case["factory"])
    preview = await service.preview(
        run_id=case["run_id"],
        task_id=case["task_id"],
        attempt_id=case["attempt_id"],
        actor=actor,
        request=RecoveryPreviewRequest(action=RecoveryAction.REPAIR_APPROVED_PLAN_CONTRACT),
    )
    request = RecoveryApplyRequest(
        action=preview.action,
        preview_token=preview.preview_token,
        reason="One authorized correction",
    )

    async def apply():
        return await service.apply(
            run_id=case["run_id"],
            task_id=case["task_id"],
            attempt_id=case["attempt_id"],
            actor=actor,
            idempotency_key="one-key",
            request=request,
        )

    first, second = await asyncio.gather(apply(), apply())
    assert first == second
    with pytest.raises(RecoveryConflict):
        await service.apply(
            run_id=case["run_id"],
            task_id=case["task_id"],
            attempt_id=case["attempt_id"],
            actor=actor,
            idempotency_key="one-key",
            request=request.model_copy(
                update={
                    "reason": "Different content",
                }
            ),
        )
    async with case["factory"]() as work:
        debits = (await work.session.scalars(select(SubscriptionRepairDebit))).all()
        receipts = (await work.session.scalars(select(SubscriptionRecoveryReceipt))).all()
        scheduled = await work.session.get(SubscriptionScheduledTask, case["task_id"])
        assert len(debits) == len(receipts) == 1
        assert scheduled.repairs == 1 and scheduled.state == "queued"


@pytest.mark.integration
@pytest.mark.parametrize("drift", ["candidate", "approval", "policy", "budget"])
async def test_recovery_revalidates_authority_and_budget(session_factory, tmp_path, drift):
    case = await recovery_case(session_factory, tmp_path)
    actor = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())
    service = SubscriptionRecoveryService(case["factory"])
    preview = await service.preview(
        run_id=case["run_id"],
        task_id=case["task_id"],
        attempt_id=case["attempt_id"],
        actor=actor,
        request=RecoveryPreviewRequest(action=RecoveryAction.REPAIR_APPROVED_PLAN_CONTRACT),
    )
    assert preview.eligible
    async with case["factory"]() as work:
        if drift == "candidate":
            row = await work.session.get(SubscriptionSchedulerRun, case["run_id"])
            row.candidate_epoch += 1
        elif drift == "approval":
            row = await work.session.scalar(
                select(Approval).where(Approval.run_id == case["run_id"])
            )
            row.invalidated_at = datetime.now(UTC)
        elif drift == "policy":
            run = await work.runs.get(case["run_id"])
            row = await work.session.get(Project, run.project_id)
            current = await work.session.get(
                ProjectPolicyVersion, (run.project_id, run.policy_version)
            )
            work.session.add(
                ProjectPolicyVersion(
                    project_id=run.project_id,
                    version=run.policy_version + 1,
                    policy_digest=current.policy_digest,
                    document_schema_version=current.document_schema_version,
                    document=current.document,
                )
            )
            await work.session.flush()
            row.current_policy_version += 1
        else:
            row = await work.session.get(SubscriptionScheduledTask, case["task_id"])
            row.max_repairs = row.repairs
        await work.commit()
    with pytest.raises(RecoveryConflict):
        await service.apply(
            run_id=case["run_id"],
            task_id=case["task_id"],
            attempt_id=case["attempt_id"],
            actor=actor,
            idempotency_key=f"drift-{drift}",
            request=RecoveryApplyRequest(
                action=preview.action,
                preview_token=preview.preview_token,
                reason="Changed authority",
            ),
        )
    async with case["factory"]() as work:
        assert await work.session.get(SubscriptionRepairDebit, case["attempt_id"]) is None
        assert await work.session.scalar(select(SubscriptionRecoveryReceipt.id)) is None


@pytest.mark.integration
async def test_recovery_transaction_rolls_back_debit_and_rejection_on_failure(
    session_factory, tmp_path, monkeypatch
):
    from forge.persistence.repositories.subscription_recovery import (
        PostgresSubscriptionRecoveryRepository,
    )

    case = await recovery_case(session_factory, tmp_path)
    actor = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())
    service = SubscriptionRecoveryService(case["factory"])
    preview = await service.preview(
        run_id=case["run_id"],
        task_id=case["task_id"],
        attempt_id=case["attempt_id"],
        actor=actor,
        request=RecoveryPreviewRequest(action=RecoveryAction.REPAIR_APPROVED_PLAN_CONTRACT),
    )

    async def fail_after_debit(*args):
        raise RuntimeError("injected transaction failure")

    monkeypatch.setattr(
        PostgresSubscriptionRecoveryRepository, "_repair_contract", fail_after_debit
    )
    with pytest.raises(RuntimeError, match="injected transaction failure"):
        await service.apply(
            run_id=case["run_id"],
            task_id=case["task_id"],
            attempt_id=case["attempt_id"],
            actor=actor,
            idempotency_key="crash-before-commit",
            request=RecoveryApplyRequest(
                action=preview.action,
                preview_token=preview.preview_token,
                reason="Rollback proof",
            ),
        )
    async with case["factory"]() as work:
        result = await work.session.get(SubscriptionAttemptResult, case["attempt_id"])
        assert result.disposition == "role_rejected"
        assert await work.session.get(SubscriptionRepairDebit, case["attempt_id"]) is None
        assert await work.session.scalar(select(SubscriptionRecoveryReceipt.id)) is None


@pytest.mark.integration
@pytest.mark.parametrize("new_state", ["leased", "reconciling", "terminal"])
async def test_older_attempt_cannot_recover_after_newer_attempt_exists(
    session_factory, tmp_path, new_state
):
    case = await recovery_case(session_factory, tmp_path)
    actor = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())
    service = SubscriptionRecoveryService(case["factory"])
    old = await service.preview(
        run_id=case["run_id"],
        task_id=case["task_id"],
        attempt_id=case["attempt_id"],
        actor=actor,
        request=RecoveryPreviewRequest(action=RecoveryAction.REPAIR_APPROVED_PLAN_CONTRACT),
    )
    assert old.eligible
    await service.apply(
        run_id=case["run_id"],
        task_id=case["task_id"],
        attempt_id=case["attempt_id"],
        actor=actor,
        idempotency_key="first-repair",
        request=RecoveryApplyRequest(
            action=old.action,
            preview_token=old.preview_token,
            reason="One repair",
        ),
    )
    newer = await SubscriptionDecisionExecutor(case["factory"]).admit_next(
        "new-primary", _reservation()
    )
    assert newer is not None and newer.attempt.attempt_id != case["attempt_id"]
    if new_state != "leased":
        async with case["factory"]() as work:
            scheduled = await work.session.get(SubscriptionScheduledTask, case["task_id"])
            attempt = await work.session.get(SubscriptionAttempt, newer.attempt.attempt_id)
            scheduled.state = new_state
            attempt.status = new_state
            await work.commit()
    async with case["factory"]() as work:
        before = await work.session.get(SubscriptionAttempt, newer.attempt.attempt_id)
        before_status = before.status
        before_result = await work.session.get(SubscriptionAttemptResult, newer.attempt.attempt_id)
        receipt_count = len((await work.session.scalars(select(SubscriptionRecoveryReceipt))).all())
    for action in RecoveryAction:
        stale = await service.preview(
            run_id=case["run_id"],
            task_id=case["task_id"],
            attempt_id=case["attempt_id"],
            actor=actor,
            request=RecoveryPreviewRequest(action=action),
        )
        assert not stale.eligible and stale.reason_code == "source_changed"
    with pytest.raises(RecoveryConflict):
        await service.apply(
            run_id=case["run_id"],
            task_id=case["task_id"],
            attempt_id=case["attempt_id"],
            actor=actor,
            idempotency_key=f"stale-{new_state}",
            request=RecoveryApplyRequest(
                action=old.action,
                preview_token=old.preview_token,
                reason="Stale attempt",
            ),
        )
    async with case["factory"]() as work:
        newer_after = await work.session.get(SubscriptionAttempt, newer.attempt.attempt_id)
        assert newer_after.status == before_status
        assert (
            await work.session.get(SubscriptionAttemptResult, newer.attempt.attempt_id)
            == before_result
        )
        assert (
            len((await work.session.scalars(select(SubscriptionRecoveryReceipt))).all())
            == receipt_count
        )


@pytest.mark.integration
async def test_database_rejects_old_worker_acquisition_of_revised_contract(
    session_factory, tmp_path
):
    factory, evidence, command, preparation, _ = await preparation_case(
        session_factory, tmp_path, plan_scope=("apps",)
    )
    async with factory() as work:
        await preparation.execute(command, work)
    with pytest.raises(DBAPIError, match="compatible worker"):
        async with factory() as work:
            await work.session.execute(
                update(SubscriptionScheduledTask)
                .where(SubscriptionScheduledTask.task_id == evidence.producer.task_id)
                .values(
                    state="leased",
                    lease_owner="old-worker",
                    lease_generation=1,
                    lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
                )
            )
            await work.commit()
    admission = await SubscriptionDecisionExecutor(factory).admit_next(
        "compatible-worker", _reservation()
    )
    assert admission is not None and admission.task.task_id == evidence.producer.task_id


@pytest.mark.integration
async def test_database_rejects_old_worker_acquisition_of_recovered_task(session_factory, tmp_path):
    case = await recovery_case(session_factory, tmp_path)
    actor = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())
    service = SubscriptionRecoveryService(case["factory"])
    preview = await service.preview(
        run_id=case["run_id"],
        task_id=case["task_id"],
        attempt_id=case["attempt_id"],
        actor=actor,
        request=RecoveryPreviewRequest(action=RecoveryAction.REPAIR_APPROVED_PLAN_CONTRACT),
    )
    await service.apply(
        run_id=case["run_id"],
        task_id=case["task_id"],
        attempt_id=case["attempt_id"],
        actor=actor,
        idempotency_key="compatibility",
        request=RecoveryApplyRequest(
            action=preview.action,
            preview_token=preview.preview_token,
            reason="Compatible recovery",
        ),
    )
    # Isolate the receipt fence from the contract-revision fence.
    async with case["factory"]() as work:
        revision = await work.session.scalar(
            select(SubscriptionContractRevision).where(
                SubscriptionContractRevision.task_id == case["task_id"]
            )
        )
        assert revision is not None
        await work.session.delete(revision)
        await work.commit()
    with pytest.raises(DBAPIError, match="compatible worker"):
        async with case["factory"]() as work:
            await work.session.execute(
                update(SubscriptionScheduledTask)
                .where(SubscriptionScheduledTask.task_id == case["task_id"])
                .values(
                    state="leased",
                    lease_owner="old-worker",
                    lease_generation=2,
                    lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
                )
            )
            await work.commit()


@pytest.mark.integration
async def test_database_fences_automatic_role_correction_without_revision_or_receipt(
    session_factory, tmp_path
):
    factory, evidence, command, preparation, _ = await preparation_case(
        session_factory,
        tmp_path,
        plan_scope=("apps",),
        primary_budget=TaskBudget(
            max_provider_attempts=5,
            max_repairs=3,
            unknown_telemetry_policy=UnknownTelemetryPolicy(max_uncertain_attempts=5),
        ),
    )
    async with factory() as work:
        await preparation.execute(command, work)
        revision = await work.session.scalar(
            select(SubscriptionContractRevision).where(
                SubscriptionContractRevision.task_id == evidence.producer.task_id
            )
        )
        assert revision is not None
        await work.session.delete(revision)
        await work.commit()
    executor = SubscriptionDecisionExecutor(factory)
    first = await executor.admit_next("current-worker", _reservation())
    assert first is not None
    proof = await record_stopped_launch(session_factory, first)
    settled = await executor.settle(
        first,
        SubscriptionInvocationResult(
            attempt=first.attempt,
            decision=TaskHandoff(
                run_id=first.attempt.run_id,
                task_id=first.attempt.task_id,
                attempt_id=first.attempt.attempt_id,
                status=HandoffStatus.COMPLETED,
                summary="Invalid primary handoff",
                candidate_tree_digest="a" * 64,
                evidence_receipt_ids=("receipt",),
            ),
            telemetry=AttemptTelemetry(input_tokens=10, output_tokens=5, duration_ms=100),
            launch_proof=proof,
        ),
    )
    assert settled.disposition == "role_correction_queued"
    with pytest.raises(DBAPIError, match="compatible worker"):
        async with factory() as work:
            await work.session.execute(
                update(SubscriptionScheduledTask)
                .where(SubscriptionScheduledTask.task_id == evidence.producer.task_id)
                .values(
                    state="leased",
                    lease_owner="old-worker",
                    lease_generation=2,
                    lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
                )
            )
            await work.commit()
    second = await executor.admit_next("current-worker", _reservation())
    assert second is not None and second.attempt.attempt_id != first.attempt.attempt_id


@pytest.mark.integration
async def test_same_primary_role_violation_gets_one_automatic_correction(session_factory, tmp_path):
    factory, evidence, command, preparation, _ = await preparation_case(
        session_factory,
        tmp_path,
        plan_scope=("apps",),
        primary_budget=TaskBudget(
            max_provider_attempts=5,
            max_repairs=3,
            unknown_telemetry_policy=UnknownTelemetryPolicy(max_uncertain_attempts=5),
        ),
    )
    async with factory() as work:
        await preparation.execute(command, work)
    executor = SubscriptionDecisionExecutor(factory)
    dispositions = []
    attempt_ids = []
    for index in range(2):
        admission = await executor.admit_next(f"primary-{index}", _reservation())
        assert admission is not None
        proof = await record_stopped_launch(session_factory, admission)
        settled = await executor.settle(
            admission,
            SubscriptionInvocationResult(
                attempt=admission.attempt,
                decision=TaskHandoff(
                    run_id=admission.attempt.run_id,
                    task_id=admission.attempt.task_id,
                    attempt_id=admission.attempt.attempt_id,
                    status=HandoffStatus.COMPLETED,
                    summary="Wrong role decision",
                    candidate_tree_digest="a" * 64,
                    evidence_receipt_ids=("receipt",),
                ),
                telemetry=AttemptTelemetry(input_tokens=10, output_tokens=5, duration_ms=100),
                launch_proof=proof,
            ),
        )
        dispositions.append(settled.disposition)
        attempt_ids.append(admission.attempt.attempt_id)
    assert dispositions == ["role_correction_queued", "role_rejected"]
    async with factory() as work:
        scheduled = await work.session.get(SubscriptionScheduledTask, evidence.producer.task_id)
        assert scheduled.repairs == 1
        assert scheduled.state == "terminal"
        first_diagnostic = await work.session.get(SubscriptionApplicationDiagnostic, attempt_ids[0])
        second_diagnostic = await work.session.get(
            SubscriptionApplicationDiagnostic, attempt_ids[1]
        )
        assert first_diagnostic.resolution == "rejected"
        assert second_diagnostic.resolution == "attention"


@pytest.mark.integration
async def test_queued_historical_stale_contract_is_repaired_before_provider_admission(
    session_factory, tmp_path
):
    factory, evidence, command, preparation, _ = await preparation_case(
        session_factory, tmp_path, plan_scope=("apps",)
    )
    async with factory() as work:
        await preparation.execute(command, work)
        task = await work.session.get(SubscriptionTask, evidence.producer.task_id)
        prepared = decode_subscription_record(task.payload)
        stale = replace(
            prepared,
            typed_acceptance=(
                AcceptanceCriterion(
                    criterion_id="approved-plan",
                    description="Produce an evidence-bound plan for the requested task.",
                ),
            ),
            untrusted_context_refs=prepared.untrusted_context_refs[:-1],
        )
        original_payload = encode_subscription_record(stale)
        task.payload = original_payload
        revision = await work.session.scalar(
            select(SubscriptionContractRevision).where(
                SubscriptionContractRevision.task_id == task.id
            )
        )
        await work.session.delete(revision)
        work.session.add(
            SubscriptionRecoveryWorker(
                worker_id=f"queued-fixture-{uuid4()}",
                contract_version=1,
                observed_at=datetime.now(UTC),
            )
        )
        await work.commit()
    executor = SubscriptionDecisionExecutor(factory)
    assert await executor.admit_next("current-worker", _reservation()) is None
    actor = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())
    service = SubscriptionRecoveryService(factory)
    preview = await service.preview(
        run_id=evidence.producer.run_id,
        task_id=evidence.producer.task_id,
        attempt_id=evidence.producer.attempt_id,
        actor=actor,
        request=RecoveryPreviewRequest(action=RecoveryAction.REPAIR_APPROVED_PLAN_CONTRACT),
    )
    assert preview.eligible, preview.reason_code
    assert preview.budget_impact.provider_attempts == preview.budget_impact.repair_units == 0
    await service.apply(
        run_id=evidence.producer.run_id,
        task_id=evidence.producer.task_id,
        attempt_id=evidence.producer.attempt_id,
        actor=actor,
        idempotency_key="queued-contract",
        request=RecoveryApplyRequest(
            action=preview.action,
            preview_token=preview.preview_token,
            reason="Correct approved objective before execution",
        ),
    )
    async with factory() as work:
        revision = await work.session.scalar(
            select(SubscriptionContractRevision).where(
                SubscriptionContractRevision.task_id == evidence.producer.task_id
            )
        )
        result = await work.session.get(SubscriptionAttemptResult, evidence.producer.attempt_id)
        assert revision.original_contract_payload == original_payload
        assert revision.original_contract_digest == canonical_digest(original_payload)
        assert result.accepted and result.disposition == "plan_approval"
        assert await work.session.get(SubscriptionRepairDebit, evidence.producer.attempt_id) is None
    admission = await executor.admit_next("current-worker", _reservation())
    assert admission is not None
    request = await SubscriptionRequestBuilder(factory).build(admission)
    assert request.task.typed_acceptance[0].criterion_id == "approved-implementation"
    assert request.untrusted_context["approved_implementation"] is not None


@pytest.mark.integration
@pytest.mark.parametrize("failure", ["interrupted", "quota"])
async def test_latest_queued_stale_contract_repair_preserves_settled_source(
    session_factory, tmp_path, failure
):
    from forge.application.ports.subscription_gateway import SubscriptionFailure
    from test_subscription_usage import _known

    factory, evidence, command, preparation, _ = await preparation_case(
        session_factory, tmp_path, plan_scope=("apps",),
        primary_budget=TaskBudget(max_provider_attempts=5, max_repairs=3),
    )
    async with factory() as work:
        await preparation.execute(command, work)
    executor = SubscriptionDecisionExecutor(factory)
    admission = await executor.admit_next("legacy-primary", _reservation())
    assert admission is not None
    async with factory() as work:
        task = await work.session.get(SubscriptionTask, evidence.producer.task_id)
        prepared = decode_subscription_record(task.payload)
        stale = replace(
            prepared,
            typed_acceptance=(AcceptanceCriterion(
                criterion_id="approved-plan", description="Produce an evidence-bound plan."
            ),),
            untrusted_context_refs=prepared.untrusted_context_refs[:-1],
        )
        original_payload = encode_subscription_record(stale)
        task.payload = original_payload
        attempt = await work.session.get(SubscriptionAttempt, admission.attempt.attempt_id)
        attempt.task_digest = canonical_digest(original_payload)
        revision = await work.session.scalar(select(SubscriptionContractRevision).where(
            SubscriptionContractRevision.task_id == task.id
        ))
        await work.session.delete(revision)
        work.session.add(SubscriptionRecoveryWorker(
            worker_id=f"queued-latest-{uuid4()}", contract_version=1,
            observed_at=datetime.now(UTC),
        ))
        await work.commit()
    admission = replace(admission, task=stale)
    settled = await executor.settle(admission, SubscriptionInvocationResult(
        attempt=admission.attempt,
        failure=SubscriptionFailure.INTERRUPTED if failure == "interrupted" else SubscriptionFailure.QUOTA,
        telemetry=_known(),
        launch_proof=await record_stopped_launch(session_factory, admission),
    ))
    assert settled.disposition == ("repair_queued" if failure == "interrupted" else "quota_deferred")
    async with factory() as work:
        result = await work.session.get(SubscriptionAttemptResult, admission.attempt.attempt_id)
        original_result = result.result_payload, result.result_digest, result.disposition
        task = await work.session.get(SubscriptionTask, evidence.producer.task_id)
        original_version = task.version
        if failure == "interrupted":
            work.session.add(SubscriptionApplicationDiagnostic(
                attempt_id=admission.attempt.attempt_id,
                run_id=admission.attempt.run_id, task_id=admission.attempt.task_id,
                classification="temporary", reason_code="old_reason", resolution="waiting",
                failed_applications=1, first_failure_at=datetime.now(UTC),
                last_failure_at=datetime.now(UTC),
            ))
        await work.commit()
    assert await executor.admit_next("current-worker", _reservation()) is None
    assert await executor.admit_next("current-worker", _reservation()) is None
    async with factory() as work:
        task = await work.session.get(SubscriptionTask, evidence.producer.task_id)
        diagnostic = await work.session.get(SubscriptionApplicationDiagnostic, admission.attempt.attempt_id)
        assert task.version == original_version + 1
        assert diagnostic.reason_code == "approved_plan_contract_stale"
        assert diagnostic.resolution == "attention"
        attention_events = (await work.session.scalars(select(RunEvent).where(
            RunEvent.run_id == admission.attempt.run_id,
            RunEvent.event_type == "run.subscription_recovery_attention_changed",
            RunEvent.payload["attempt_id"].astext == str(admission.attempt.attempt_id),
        ).order_by(RunEvent.sequence))).all()
        assert len(attention_events) == 1 and attention_events[0].payload["attention"] is True
    actor = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())
    service = SubscriptionRecoveryService(factory)
    preview = await service.preview(
        run_id=admission.attempt.run_id, task_id=admission.attempt.task_id,
        attempt_id=admission.attempt.attempt_id, actor=actor,
        request=RecoveryPreviewRequest(action=RecoveryAction.REPAIR_APPROVED_PLAN_CONTRACT),
    )
    assert preview.eligible, preview.reason_code
    assert preview.budget_impact.provider_attempts == preview.budget_impact.repair_units == 0
    if failure == "quota":
        async with factory() as work:
            launch = await work.session.scalar(select(SubscriptionClientLaunch).where(
                SubscriptionClientLaunch.attempt_id == admission.attempt.attempt_id
            ))
            launch.state = "uncertain"
            await work.commit()
        uncertain = await service.preview(
            run_id=admission.attempt.run_id, task_id=admission.attempt.task_id,
            attempt_id=admission.attempt.attempt_id, actor=actor,
            request=RecoveryPreviewRequest(action=RecoveryAction.REPAIR_APPROVED_PLAN_CONTRACT),
        )
        assert not uncertain.eligible and uncertain.reason_code == "effect_uncertain"
        async with factory() as work:
            launch = await work.session.scalar(select(SubscriptionClientLaunch).where(
                SubscriptionClientLaunch.attempt_id == admission.attempt.attempt_id
            ))
            launch.state = "terminal"
            task = await work.session.get(SubscriptionTask, evidence.producer.task_id)
            task.version += 1
            await work.commit()
        with pytest.raises(RecoveryConflict):
            await service.apply(
                run_id=admission.attempt.run_id, task_id=admission.attempt.task_id,
                attempt_id=admission.attempt.attempt_id, actor=actor,
                idempotency_key="queued-quota-stale-preview",
                request=RecoveryApplyRequest(
                    action=preview.action, preview_token=preview.preview_token,
                    reason="Version changed",
                ),
            )
        preview = await service.preview(
            run_id=admission.attempt.run_id, task_id=admission.attempt.task_id,
            attempt_id=admission.attempt.attempt_id, actor=actor,
            request=RecoveryPreviewRequest(action=RecoveryAction.REPAIR_APPROVED_PLAN_CONTRACT),
        )
        assert preview.eligible
    receipt = await service.apply(
        run_id=admission.attempt.run_id, task_id=admission.attempt.task_id,
        attempt_id=admission.attempt.attempt_id, actor=actor,
        idempotency_key=f"queued-{failure}",
        request=RecoveryApplyRequest(
            action=preview.action, preview_token=preview.preview_token,
            reason="Correct queued approved objective",
        ),
    )
    assert receipt.status == "applied"
    assert await service.apply(
        run_id=admission.attempt.run_id, task_id=admission.attempt.task_id,
        attempt_id=admission.attempt.attempt_id, actor=actor,
        idempotency_key=f"queued-{failure}",
        request=RecoveryApplyRequest(
            action=preview.action, preview_token=preview.preview_token,
            reason="Correct queued approved objective",
        ),
    ) == receipt
    async with factory() as work:
        revision = await work.session.scalar(select(SubscriptionContractRevision).where(
            SubscriptionContractRevision.task_id == evidence.producer.task_id
        ))
        result = await work.session.get(SubscriptionAttemptResult, admission.attempt.attempt_id)
        assert revision.original_contract_payload == original_payload
        assert (result.result_payload, result.result_digest, result.disposition) == original_result
        debit = await work.session.get(SubscriptionRepairDebit, admission.attempt.attempt_id)
        assert (debit is not None) == (failure == "interrupted")
        assert await work.session.scalar(select(SubscriptionRecoveryReceipt.id)) is not None
        scheduled = await work.session.get(SubscriptionScheduledTask, evidence.producer.task_id)
        assert scheduled.state == "queued"
        attention_events = (await work.session.scalars(select(RunEvent).where(
            RunEvent.run_id == admission.attempt.run_id,
            RunEvent.event_type == "run.subscription_recovery_attention_changed",
            RunEvent.payload["attempt_id"].astext == str(admission.attempt.attempt_id),
        ).order_by(RunEvent.sequence))).all()
        assert len(attention_events) == 2
        assert attention_events[1].payload["attention"] is False
    if failure == "quota":
        # Advance this disposable pool's next probe time to model a quota reset;
        # contract correction itself must not override provider quota authority.
        async with factory() as work:
            pool = await work.session.scalar(select(SubscriptionQuotaPool).where(
                SubscriptionQuotaPool.blocked.is_(True)
            ))
            assert pool is not None
            pool.next_eligible_at = datetime.now(UTC) - timedelta(seconds=1)
            await work.commit()
    continuation = await executor.admit_next("current-worker", _reservation())
    assert continuation is not None
    assert continuation.attempt.attempt_number == admission.attempt.attempt_number + 1
    assert continuation.task.typed_acceptance[0].criterion_id == "approved-implementation"


@pytest.mark.integration
async def test_retained_historical_pending_result_reconciles_then_repairs_once(
    session_factory, tmp_path
):
    case = await recovery_case(session_factory, tmp_path)
    async with case["factory"]() as work:
        result = await work.session.get(SubscriptionAttemptResult, case["attempt_id"])
        scheduled = await work.session.get(SubscriptionScheduledTask, case["task_id"])
        attempt = await work.session.get(SubscriptionAttempt, case["attempt_id"])
        task = await work.session.get(SubscriptionTask, case["task_id"])
        original_payload, original_digest = result.result_payload, result.result_digest
        result.disposition = "decision_pending"
        result.application_payload = None
        result.application_digest = None
        result.accepted = False
        scheduled.state = "reconciling"
        attempt.status = "reconciling"
        task.state = "reconciling"
        diagnostic = await work.session.get(SubscriptionApplicationDiagnostic, case["attempt_id"])
        if diagnostic is not None:
            await work.session.delete(diagnostic)
        await work.commit()
    report = await SubscriptionDecisionRecovery(
        case["factory"], FilesystemArtifactStore(tmp_path / "artifacts")
    ).reconcile_all()
    assert report.deferred == 1
    async with case["factory"]() as work:
        result = await work.session.get(SubscriptionAttemptResult, case["attempt_id"])
        diagnostic = await work.session.get(SubscriptionApplicationDiagnostic, case["attempt_id"])
        assert result.result_payload == original_payload and result.result_digest == original_digest
        assert result.disposition == "role_rejected" and not result.accepted
        assert diagnostic.resolution == "attention"
    actor = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())
    service = SubscriptionRecoveryService(case["factory"])
    preview = await service.preview(
        run_id=case["run_id"], task_id=case["task_id"], attempt_id=case["attempt_id"],
        actor=actor,
        request=RecoveryPreviewRequest(action=RecoveryAction.REPAIR_APPROVED_PLAN_CONTRACT),
    )
    assert preview.eligible, preview.reason_code
    receipt = await service.apply(
        run_id=case["run_id"], task_id=case["task_id"], attempt_id=case["attempt_id"],
        actor=actor, idempotency_key="historical-reconcile", request=RecoveryApplyRequest(
            action=preview.action, preview_token=preview.preview_token,
            reason="Repair retained historical result",
        ),
    )
    assert receipt.status == "applied"
    async with case["factory"]() as work:
        scheduled = await work.session.get(SubscriptionScheduledTask, case["task_id"])
        assert scheduled.state == "queued" and scheduled.repairs == 1
        result = await work.session.get(SubscriptionAttemptResult, case["attempt_id"])
        assert result.result_payload == original_payload and result.result_digest == original_digest
        assert await work.session.get(SubscriptionRepairDebit, case["attempt_id"]) is not None
    admission = await SubscriptionDecisionExecutor(case["factory"]).admit_next(
        "current-worker", _reservation()
    )
    assert admission is not None
    request = await SubscriptionRequestBuilder(case["factory"]).build(admission)
    assert request.task.typed_acceptance[0].criterion_id == "approved-implementation"


@pytest.mark.integration
async def test_attention_projection_excludes_cancelled_and_superseded_sources(
    session_factory, tmp_path
):
    from forge.persistence.queries.dashboard import DashboardQuery
    from forge.persistence.queries.subscription_tasks import SubscriptionTaskQuery

    case = await recovery_case(session_factory, tmp_path)
    async with case["factory"]() as work:
        diagnostic = await work.session.get(SubscriptionApplicationDiagnostic, case["attempt_id"])
        diagnostic.resolution = "attention"
        await work.commit()

    async def visible() -> tuple[bool, bool]:
        run_view = await DashboardQuery(session_factory).run_projection(case["run_id"])
        task_view = await SubscriptionTaskQuery(session_factory).tasks(case["run_id"])
        assert run_view is not None and task_view is not None
        return (
            run_view["subscription_recovery_attention"],
            task_view["tasks"][0]["recovery_attention"],
        )

    assert await visible() == (True, True)
    async with case["factory"]() as work:
        task = await work.session.get(SubscriptionTask, case["task_id"])
        task.cancel_requested = True
        await work.commit()
    assert await visible() == (False, False)
    async with case["factory"]() as work:
        task = await work.session.get(SubscriptionTask, case["task_id"])
        task.cancel_requested = False
        await work.commit()
    actor = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())
    service = SubscriptionRecoveryService(case["factory"])
    preview = await service.preview(
        run_id=case["run_id"], task_id=case["task_id"], attempt_id=case["attempt_id"],
        actor=actor, request=RecoveryPreviewRequest(action=RecoveryAction.REPAIR_APPROVED_PLAN_CONTRACT),
    )
    assert preview.eligible
    await service.apply(
        run_id=case["run_id"], task_id=case["task_id"], attempt_id=case["attempt_id"],
        actor=actor, idempotency_key="attention-test", request=RecoveryApplyRequest(
            action=preview.action, preview_token=preview.preview_token, reason="Repair stale result"
        ),
    )
    newer = await SubscriptionDecisionExecutor(case["factory"]).admit_next(
        "current-worker", _reservation()
    )
    assert newer is not None
    async with case["factory"]() as work:
        diagnostic = await work.session.get(SubscriptionApplicationDiagnostic, case["attempt_id"])
        diagnostic.resolution = "attention"
        await work.commit()
    assert await visible() == (False, False)


@pytest.mark.integration
@pytest.mark.parametrize(
    ("accepted_failure", "safe_apply"),
    [(True, False), (False, False), (False, True)],
)
async def test_specialist_recovery_refuses_accepted_failure_and_superseded_child(
    session_factory, tmp_path, accepted_failure, safe_apply, monkeypatch
):
    from forge.application.ports.subscription_gateway import SubscriptionFailure
    from forge.application.services.subscription_decisions import SubscriptionDecisionApplication
    from forge.domain.subscription import SpecialistPurpose, WaitDecision
    from forge.persistence.repositories.scheduling import PostgresSchedulingRepository
    from test_subscription_delegation_application import delegation_case
    from test_subscription_usage import _known

    def three_children(child, _):
        target_id = uuid4()
        return (
            replace(child, owned_paths=("apps/sentinel",)),
            replace(child, task_id=target_id, owned_paths=("apps/target",),
                    dependency_task_ids=(child.task_id,)),
            replace(child, task_id=uuid4(), owned_paths=("apps/dependent",),
                    dependency_task_ids=(target_id,)),
        )

    factory, parent, children, _ = await delegation_case(
        session_factory, tmp_path, primary_budget=TaskBudget(max_provider_attempts=8, max_repairs=5),
        mutate=None if accepted_failure else three_children,
    )
    delegation = await SubscriptionDecisionApplication(factory).apply_delegation(parent.attempt.attempt_id)
    assert delegation.accepted and delegation.disposition == "delegated"
    if not accepted_failure:
        # The delegation fixture defaults to a zero-repair child. Give this
        # isolated child room for one automatic correction and operator action
        # so the authority check, rather than the budget cap, is exercised.
        async with factory() as work:
            child_row = await work.session.get(SubscriptionTask, children[1].task_id)
            child_schedule = await work.session.get(SubscriptionScheduledTask, children[1].task_id)
            child_contract = decode_subscription_record(child_row.payload)
            child_row.payload = encode_subscription_record(
                replace(
                    child_contract,
                    max_repairs=3,
                    budget=replace(child_contract.budget, max_provider_attempts=4, max_repairs=3),
                )
            )
            child_schedule.max_repairs = 3
            work.session.add(SubscriptionRecoveryWorker(
                worker_id=f"specialist-fixture-{uuid4()}", contract_version=1,
                observed_at=datetime.now(UTC),
            ))
            await work.commit()
    executor = SubscriptionDecisionExecutor(factory)
    child = await executor.admit_next("specialist", _reservation())
    assert child is not None and child.task.purpose is SpecialistPurpose.ROUTINE_IMPLEMENTATION
    actor = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())
    service = SubscriptionRecoveryService(factory)
    if accepted_failure:
        failed = await executor.settle(
            child,
            SubscriptionInvocationResult(
                attempt=child.attempt, failure=SubscriptionFailure.PROTOCOL, telemetry=_known()
            ),
        )
        assert failed.accepted and failed.disposition == "failed"
        refused = await service.preview(
            run_id=child.attempt.run_id, task_id=child.task.task_id,
            attempt_id=child.attempt.attempt_id, actor=actor,
            request=RecoveryPreviewRequest(action=RecoveryAction.REJECT_AND_RETRY_STEP),
        )
        assert not refused.eligible
        async with factory() as work:
            assert await work.session.get(SubscriptionRepairDebit, child.attempt.attempt_id) is None
        return

    assert child.task.task_id == children[0].task_id
    assert (await executor.settle(child, SubscriptionInvocationResult(
        attempt=child.attempt, failure=SubscriptionFailure.PROTOCOL, telemetry=_known(),
    ))).disposition == "failed"
    waiting_parent = await executor.admit_next("waiting-parent", _reservation())
    assert waiting_parent is not None and waiting_parent.task.task_id == parent.task.task_id
    wait_result = await executor.settle(waiting_parent, SubscriptionInvocationResult(
        attempt=waiting_parent.attempt,
        decision=WaitDecision(
            run_id=parent.attempt.run_id, task_id=parent.task.task_id,
            waiting_on_task_ids=(children[2].task_id,), reason="Wait for dependent outcome",
        ),
        telemetry=_known(), launch_proof=await record_stopped_launch(session_factory, waiting_parent),
    ))
    assert wait_result.disposition == "decision_pending"
    assert (await SubscriptionDecisionApplication(factory).apply_wait(
        waiting_parent.attempt.attempt_id
    )).disposition == "waiting"
    child = await executor.admit_next("target-specialist", _reservation())
    assert child is not None and child.task.task_id == children[1].task_id

    # The parent explicitly waits for a different child, so no consumer has
    # advanced through this target's unusable result yet.
    plan = PlanOutput(
        summary="Unauthorized plan", assumptions=(), affected_components=("apps",),
        steps=("Plan instead of implement",), required_checks=("unit",), risks=("No implementation",),
        security_considerations=(), dependency_changes=(),
    )
    proof = await record_stopped_launch(session_factory, child)
    first = await executor.settle(
        child, SubscriptionInvocationResult(
            attempt=child.attempt, decision=plan, telemetry=_known(), launch_proof=proof
        ),
    )
    assert first.disposition == "role_correction_queued"
    correction = await executor.admit_next("specialist-correction", _reservation())
    assert correction is not None and correction.task.task_id == child.task.task_id
    proof = await record_stopped_launch(session_factory, correction)
    second = await executor.settle(
        correction, SubscriptionInvocationResult(
            attempt=correction.attempt, decision=plan, telemetry=_known(), launch_proof=proof
        ),
    )
    assert second.disposition == "role_rejected"
    allowed = await service.preview(
        run_id=correction.attempt.run_id, task_id=correction.task.task_id,
        attempt_id=correction.attempt.attempt_id, actor=actor,
        request=RecoveryPreviewRequest(action=RecoveryAction.REJECT_AND_RETRY_STEP),
    )
    assert allowed.eligible, allowed.reason_code
    if safe_apply:
        request = RecoveryApplyRequest(
            action=allowed.action, preview_token=allowed.preview_token,
            reason="Retry stopped child while parent waits on another child",
        )
        applied = await service.apply(
            run_id=correction.attempt.run_id, task_id=correction.task.task_id,
            attempt_id=correction.attempt.attempt_id, actor=actor,
            idempotency_key="safe-child-retry", request=request,
        )
        assert applied.status == "applied"
        assert await service.apply(
            run_id=correction.attempt.run_id, task_id=correction.task.task_id,
            attempt_id=correction.attempt.attempt_id, actor=actor,
            idempotency_key="safe-child-retry", request=request,
        ) == applied
        async with factory() as work:
            parent_row = await work.session.get(SubscriptionTask, parent.task.task_id)
            parent_schedule = await work.session.get(SubscriptionScheduledTask, parent.task.task_id)
            target = await work.session.get(SubscriptionScheduledTask, children[1].task_id)
            dependent = await work.session.get(SubscriptionScheduledTask, children[2].task_id)
            assert parent_row.state == parent_schedule.state == "blocked"
            assert target.state == dependent.state == "queued"
            assert await work.session.get(SubscriptionRepairDebit, correction.attempt.attempt_id)
            assert len((await work.session.scalars(select(SubscriptionRecoveryReceipt))).all()) == 1
        revived = await executor.admit_next("revived-target", _reservation())
        assert revived is not None and revived.task.task_id == children[1].task_id
        assert revived.attempt.attempt_number == correction.attempt.attempt_number + 1
        async with factory() as work:
            parent_row = await work.session.get(SubscriptionTask, parent.task.task_id)
            dependent = await work.session.get(SubscriptionScheduledTask, children[2].task_id)
            assert parent_row.state == "blocked" and dependent.state == "queued"
        return
    claimed, release = asyncio.Event(), asyncio.Event()
    original_claim = PostgresSchedulingRepository.claim_execution_ready

    async def hold_dependent_claim(self, *args, **kwargs):
        lease = await original_claim(self, *args, **kwargs)
        if lease is not None and lease.task_id == children[2].task_id:
            claimed.set()
            await release.wait()
        return lease

    monkeypatch.setattr(PostgresSchedulingRepository, "claim_execution_ready", hold_dependent_claim)
    admission_task = asyncio.create_task(executor.admit_next("other-specialist", _reservation()))
    try:
        await asyncio.wait_for(claimed.wait(), 5)
        recovery_task = asyncio.create_task(service.apply(
            run_id=correction.attempt.run_id, task_id=correction.task.task_id,
            attempt_id=correction.attempt.attempt_id, actor=actor,
            idempotency_key="stale-child-after-dependent-claim",
            request=RecoveryApplyRequest(
                action=allowed.action, preview_token=allowed.preview_token,
                reason="Dependent is admitting",
            ),
        ))
        await asyncio.sleep(0.05)
        assert not recovery_task.done()  # both transitions serialize on the run lock
    finally:
        release.set()
    other = await admission_task
    with pytest.raises(RecoveryConflict):
        await recovery_task
    monkeypatch.setattr(PostgresSchedulingRepository, "claim_execution_ready", original_claim)
    assert other is not None and other.task.task_id == children[2].task_id
    admitted_dependent = await service.preview(
        run_id=correction.attempt.run_id, task_id=correction.task.task_id,
        attempt_id=correction.attempt.attempt_id, actor=actor,
        request=RecoveryPreviewRequest(action=RecoveryAction.REJECT_AND_RETRY_STEP),
    )
    assert not admitted_dependent.eligible and admitted_dependent.reason_code == "source_changed"
    failed_other = await executor.settle(other, SubscriptionInvocationResult(
        attempt=other.attempt, failure=SubscriptionFailure.PROTOCOL, telemetry=_known(),
    ))
    assert failed_other.disposition == "failed"
    # The parent has been woken by the terminal children, even before its next
    # attempt exists. A formerly eligible preview must now be stale at apply.
    woken = await service.preview(
        run_id=correction.attempt.run_id, task_id=correction.task.task_id,
        attempt_id=correction.attempt.attempt_id, actor=actor,
        request=RecoveryPreviewRequest(action=RecoveryAction.REJECT_AND_RETRY_STEP),
    )
    assert not woken.eligible and woken.reason_code == "source_changed"
    with pytest.raises(RecoveryConflict):
        await service.apply(
            run_id=correction.attempt.run_id, task_id=correction.task.task_id,
            attempt_id=correction.attempt.attempt_id, actor=actor,
            idempotency_key="stale-child-after-parent-wake",
            request=RecoveryApplyRequest(
                action=allowed.action, preview_token=allowed.preview_token,
                reason="Parent advanced",
            ),
        )
    resumed_parent = await executor.admit_next("resumed-parent", _reservation())
    assert resumed_parent is not None and resumed_parent.task.task_id == parent.task.task_id
    leased = await service.preview(
        run_id=correction.attempt.run_id, task_id=correction.task.task_id,
        attempt_id=correction.attempt.attempt_id, actor=actor,
        request=RecoveryPreviewRequest(action=RecoveryAction.REJECT_AND_RETRY_STEP),
    )
    assert not leased.eligible and leased.reason_code == "source_changed"
    pending = await executor.settle(resumed_parent, SubscriptionInvocationResult(
        attempt=resumed_parent.attempt,
        decision=WaitDecision(
            run_id=resumed_parent.attempt.run_id,
            task_id=resumed_parent.attempt.task_id,
            waiting_on_task_ids=(children[0].task_id,),
            reason="Parent resumed after child outcome",
        ),
        telemetry=_known(), launch_proof=await record_stopped_launch(session_factory, resumed_parent),
    ))
    assert pending.disposition == "decision_pending"
    unapplied = await service.preview(
        run_id=correction.attempt.run_id, task_id=correction.task.task_id,
        attempt_id=correction.attempt.attempt_id, actor=actor,
        request=RecoveryPreviewRequest(action=RecoveryAction.REJECT_AND_RETRY_STEP),
    )
    assert not unapplied.eligible and unapplied.reason_code == "source_changed"
    async with factory() as work:
        assert await work.session.get(SubscriptionRepairDebit, correction.attempt.attempt_id) is None
        assert await work.session.scalar(select(SubscriptionRecoveryReceipt.id)) is None


@pytest.mark.integration
async def test_pending_current_run_stop_fences_recovery_preview_and_apply(session_factory, tmp_path):
    from forge.persistence.models.execution import RunCommand
    from forge.persistence.models.run import Run

    case = await recovery_case(session_factory, tmp_path)
    actor = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())
    service = SubscriptionRecoveryService(case["factory"])
    initial = await service.preview(
        run_id=case["run_id"], task_id=case["task_id"], attempt_id=case["attempt_id"],
        actor=actor, request=RecoveryPreviewRequest(action=RecoveryAction.REPAIR_APPROVED_PLAN_CONTRACT),
    )
    assert initial.eligible
    async with case["factory"]() as work:
        run = await work.session.get(Run, case["run_id"])
        work.session.add(RunCommand(
            run_id=run.id, idempotency_key=f"stop-before-recovery-{uuid4()}",
            command_type="cancel", expected_run_version=run.version,
            actor_id=actor.actor_id, payload={},
        ))
        await work.commit()
    blocked = await service.preview(
        run_id=case["run_id"], task_id=case["task_id"], attempt_id=case["attempt_id"],
        actor=actor, request=RecoveryPreviewRequest(action=RecoveryAction.REPAIR_APPROVED_PLAN_CONTRACT),
    )
    assert not blocked.eligible and blocked.reason_code == "run_controlled"
    with pytest.raises(RecoveryConflict):
        await service.apply(
            run_id=case["run_id"], task_id=case["task_id"], attempt_id=case["attempt_id"],
            actor=actor, idempotency_key="stop-fenced", request=RecoveryApplyRequest(
                action=initial.action, preview_token=initial.preview_token,
                reason="Must not outrun an accepted stop",
            ),
        )
    async with case["factory"]() as work:
        assert await work.session.get(SubscriptionRepairDebit, case["attempt_id"]) is None
        assert await work.session.scalar(select(SubscriptionRecoveryReceipt.id)) is None


@pytest.mark.integration
async def test_repair_binds_prepared_plan_amid_competing_gate_rows(session_factory, tmp_path):
    from forge.persistence.models.subscription_plan_gate import SubscriptionPlanGate

    case = await recovery_case(session_factory, tmp_path)
    async with case["factory"]() as work:
        original = await work.session.scalar(select(SubscriptionPlanGate).where(
            SubscriptionPlanGate.task_id == case["task_id"]
        ))
        assert original is not None and original.attempt_id != case["attempt_id"]
        work.session.add(SubscriptionPlanGate(
            attempt_id=case["attempt_id"], run_id=case["run_id"], task_id=case["task_id"],
            plan_digest="b" * 64, evidence_digest="c" * 64,
            result_digest=original.result_digest,
            envelope_digest=original.envelope_digest, budget_digest=original.budget_digest,
            route_digest=original.route_digest, snapshot=original.snapshot,
        ))
        await work.commit()
    actor = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())
    service = SubscriptionRecoveryService(case["factory"])
    preview = await service.preview(
        run_id=case["run_id"], task_id=case["task_id"], attempt_id=case["attempt_id"],
        actor=actor, request=RecoveryPreviewRequest(action=RecoveryAction.REPAIR_APPROVED_PLAN_CONTRACT),
    )
    assert preview.eligible, preview.reason_code
    await service.apply(
        run_id=case["run_id"], task_id=case["task_id"], attempt_id=case["attempt_id"],
        actor=actor, idempotency_key="exact-plan", request=RecoveryApplyRequest(
            action=preview.action, preview_token=preview.preview_token,
            reason="Use the plan actually prepared",
        ),
    )
    async with case["factory"]() as work:
        revision = await work.session.scalar(select(SubscriptionContractRevision).where(
            SubscriptionContractRevision.task_id == case["task_id"]
        ))
        assert revision.source_attempt_id == original.attempt_id
        assert revision.plan_digest == original.plan_digest
