"""Invalid plans repair only after proving their stopped, current producer."""

import asyncio
import hashlib
import json
from dataclasses import replace
from uuid import uuid4

import pytest
from forge.application.ports.subscription_gateway import SubscriptionInvocationResult
from forge.application.ports.subscription_plan_gate import SubscriptionPlanGateError
from forge.application.services.subscription_decision_recovery import SubscriptionDecisionRecovery
from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
from forge.application.services.subscription_plan_gate import SubscriptionPlanGateService
from forge.domain.subscription import TaskBudget
from forge.persistence.models.project import Project, ProjectPolicyVersion
from forge.persistence.models.scheduling import (
    SubscriptionScheduledEffect,
    SubscriptionScheduledTask,
)
from forge.persistence.models.subscription import (
    SubscriptionAttempt,
    SubscriptionClientLaunch,
    SubscriptionTask,
)
from forge.persistence.models.subscription_results import (
    SubscriptionAttemptResult,
    SubscriptionRepairDebit,
)
from forge.persistence.models.subscription_usage import SubscriptionAttemptConsumption
from sqlalchemy import select
from subscription_launch_fixture import record_stopped_launch
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_plan_gate import proposal_case
from test_subscription_usage import _known, _reservation


@pytest.mark.integration
async def test_invalid_plan_requeues_once_without_changing_provider_result(
    session_factory, tmp_path
):
    factory, store, evidence, _, _ = await proposal_case(
        session_factory, tmp_path, plan_scope=("src",), plan_checks=("unregistered",)
    )
    identity = evidence.producer.attempt_id
    async with factory() as work:
        row = await work.session.get(SubscriptionAttemptResult, identity)
        original = (row.result_digest, row.result_payload)
    service = SubscriptionPlanGateService(store, factory)
    outcome = await service.request_settled(identity)
    assert not outcome.accepted and outcome.disposition == "plan_repair_queued"
    assert not outcome.replayed
    assert (await service.request_settled(identity)).replayed
    async with factory() as work:
        row = await work.session.get(SubscriptionAttemptResult, identity)
        assert (row.result_digest, row.result_payload) == original
        task = await work.session.get(SubscriptionTask, evidence.producer.task_id)
        scheduled = await work.session.get(SubscriptionScheduledTask, task.id)
        assert task.state == scheduled.state == "queued" and scheduled.repairs == 1
        assert await work.session.get(SubscriptionRepairDebit, identity) is not None
        assert await work.subscription_plan_gate.get(identity) is None


@pytest.mark.integration
@pytest.mark.parametrize("limit", ["repairs", "attempts"])
async def test_invalid_plan_exhaustion_is_terminal_without_debit(session_factory, tmp_path, limit):
    budget = replace(
        TaskBudget(), **({"max_repairs": 0} if limit == "repairs" else {"max_provider_attempts": 1})
    )
    factory, store, evidence, _, _ = await proposal_case(
        session_factory,
        tmp_path,
        primary_budget=budget,
        plan_scope=("src",),
        plan_checks=("missing",),
    )
    service = SubscriptionPlanGateService(store, factory)
    outcome = await service.request_settled(evidence.producer.attempt_id)
    assert outcome.disposition == "plan_rejected" and not outcome.accepted
    assert (await service.request_settled(evidence.producer.attempt_id)).replayed
    async with factory() as work:
        assert (
            await work.session.get(SubscriptionTask, evidence.producer.task_id)
        ).state == "terminal"
        assert await work.session.get(SubscriptionRepairDebit, evidence.producer.attempt_id) is None


@pytest.mark.integration
@pytest.mark.parametrize("change", ["cancel", "source", "policy", "launch", "effect", "usage"])
async def test_invalid_plan_with_unproven_source_defers_without_debit(
    session_factory, tmp_path, change
):
    factory, store, evidence, _, _ = await proposal_case(
        session_factory, tmp_path, plan_scope=("src",), plan_checks=("missing",)
    )
    identity = evidence.producer.attempt_id
    async with factory() as work:
        if change == "cancel":
            (
                await work.session.get(SubscriptionTask, evidence.producer.task_id)
            ).cancel_requested = True
        elif change == "source":
            (await work.session.get(SubscriptionAttemptResult, identity)).result_digest = "f" * 64
        elif change == "policy":
            record = await work.session.scalar(select(ProjectPolicyVersion))
            document = dict(record.document, version=2)
            work.session.add(
                ProjectPolicyVersion(
                    project_id=record.project_id,
                    version=2,
                    document=document,
                    document_schema_version=1,
                    policy_digest=hashlib.sha256(
                        json.dumps(
                            document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                        ).encode()
                    ).hexdigest(),
                )
            )
            await work.session.flush()
            (await work.session.get(Project, record.project_id)).current_policy_version = 2
        elif change == "launch":
            launch = await work.session.scalar(
                select(SubscriptionClientLaunch).where(
                    SubscriptionClientLaunch.attempt_id == identity
                )
            )
            await work.session.delete(launch)
        elif change == "usage":
            consumption = await work.session.get(SubscriptionAttemptConsumption, identity)
            consumption.telemetry_payload = None
        else:
            attempt = await work.session.get(SubscriptionAttempt, identity)
            work.session.add(
                SubscriptionScheduledEffect(
                    id=uuid4(),
                    run_id=attempt.run_id,
                    task_id=attempt.task_row_id,
                    lease_owner=attempt.lease_owner,
                    lease_generation=attempt.lease_generation,
                    candidate_epoch=attempt.candidate_epoch,
                )
            )
        await work.commit()
    with pytest.raises(SubscriptionPlanGateError):
        await SubscriptionPlanGateService(store, factory).request_settled(identity)
    async with factory() as work:
        assert await work.session.get(SubscriptionRepairDebit, identity) is None
        result = await work.session.get(SubscriptionAttemptResult, identity)
        assert result.disposition == "decision_pending" and result.application_payload is None


@pytest.mark.integration
async def test_plan_rejection_rollback(session_factory, tmp_path):
    factory, _, evidence, _, _ = await proposal_case(
        session_factory, tmp_path, plan_scope=("src",), plan_checks=("missing",)
    )
    async with factory() as work:
        assert (
            await work.subscription_plan_gate.reject(evidence)
        ).disposition == "plan_repair_queued"
        await work.rollback()
    async with factory() as work:
        assert await work.session.get(SubscriptionRepairDebit, evidence.producer.attempt_id) is None
        assert (
            await work.session.get(SubscriptionTask, evidence.producer.task_id)
        ).state == "reconciling"


@pytest.mark.integration
async def test_plan_rejection_concurrent_recovery_and_replay_after_new_attempt(
    session_factory, tmp_path
):
    factory, store, evidence, _, _ = await proposal_case(
        session_factory, tmp_path, plan_scope=("src",), plan_checks=("missing",)
    )
    service = SubscriptionPlanGateService(store, factory)
    outcomes = await asyncio.wait_for(
        asyncio.gather(
            service.request_settled(evidence.producer.attempt_id),
            service.request_settled(evidence.producer.attempt_id),
        ),
        10,
    )
    assert sorted(result.replayed for result in outcomes) == [False, True]
    admission = await SubscriptionDecisionExecutor(factory).admit_next("replanning", _reservation())
    assert admission is not None and admission.task.task_id == evidence.producer.task_id
    async with factory() as work:
        before = (await work.session.get(SubscriptionTask, admission.task.task_id)).version
    assert (await service.request_settled(evidence.producer.attempt_id)).replayed
    async with factory() as work:
        task = await work.session.get(SubscriptionTask, admission.task.task_id)
        assert task.version == before and task.state == "running"


@pytest.mark.integration
async def test_decision_recovery_applies_invalid_plan_once(session_factory, tmp_path):
    factory, store, _, _, _ = await proposal_case(
        session_factory, tmp_path, plan_scope=("src",), plan_checks=("missing",)
    )
    recovery = SubscriptionDecisionRecovery(factory, store)
    report = await recovery.reconcile_all()
    assert report.applied == 1 and report.deferred == 0
    assert (await recovery.reconcile_all()).applied == 0


@pytest.mark.integration
async def test_valid_plan_cannot_be_rejected_by_caller(session_factory, tmp_path):
    factory, _, evidence, _, _ = await proposal_case(session_factory, tmp_path, plan_scope=("src",))
    async with factory() as work:
        with pytest.raises(SubscriptionPlanGateError, match="no current semantic proof"):
            await work.subscription_plan_gate.reject(evidence)
    async with factory() as work:
        assert await work.session.get(SubscriptionRepairDebit, evidence.producer.attempt_id) is None


@pytest.mark.integration
async def test_repaired_plan_reaches_existing_human_gate(session_factory, tmp_path):
    factory, store, evidence, plan, _ = await proposal_case(
        session_factory, tmp_path, plan_scope=("src",), plan_checks=("missing",)
    )
    service = SubscriptionPlanGateService(store, factory)
    await service.request_settled(evidence.producer.attempt_id)
    executor = SubscriptionDecisionExecutor(factory)
    admission = await executor.admit_next("replanner", _reservation())
    assert admission is not None
    proof = await record_stopped_launch(session_factory, admission)
    result = SubscriptionInvocationResult(
        attempt=admission.attempt,
        decision=plan.model_copy(update={"required_checks": ("unit",)}),
        telemetry=_known(),
        launch_proof=proof,
    )
    assert (await executor.settle(admission, result)).disposition == "decision_pending"
    outcome = await service.request_settled(admission.attempt.attempt_id)
    assert outcome.evidence_digest and not outcome.replayed
    assert (await service.request_settled(evidence.producer.attempt_id)).replayed
    async with factory() as work:
        assert await work.subscription_plan_gate.get(admission.attempt.attempt_id) is not None
        assert await work.subscription_plan_gate.get(evidence.producer.attempt_id) is None
        assert (await work.session.get(SubscriptionTask, admission.task.task_id)).state == "blocked"


@pytest.mark.integration
@pytest.mark.parametrize("change", ["receipt", "decision", "debit", "plan_binding"])
async def test_plan_rejection_replay_rejects_changed_receipt(session_factory, tmp_path, change):
    from forge.persistence.models.subscription import SubscriptionDecisionRecord

    factory, store, evidence, _, _ = await proposal_case(
        session_factory, tmp_path, plan_scope=("src",), plan_checks=("missing",)
    )
    service = SubscriptionPlanGateService(store, factory)
    identity = evidence.producer.attempt_id
    await service.request_settled(identity)
    async with factory() as work:
        if change == "receipt":
            (await work.session.get(SubscriptionAttemptResult, identity)).application_digest = (
                "f" * 64
            )
        elif change == "plan_binding":
            from forge.domain.operation import canonical_digest

            result = await work.session.get(SubscriptionAttemptResult, identity)
            receipt = dict(result.application_payload)
            receipt["evidence"] = dict(receipt["evidence"], plan_digest="f" * 64)
            result.application_payload = receipt
            result.application_digest = canonical_digest(receipt)
        elif change == "debit":
            await work.session.delete(await work.session.get(SubscriptionRepairDebit, identity))
        else:
            row = await work.session.scalar(
                select(SubscriptionDecisionRecord).where(
                    SubscriptionDecisionRecord.attempt_id == identity
                )
            )
            row.payload = {"invalid": True}
        await work.commit()
    with pytest.raises(SubscriptionPlanGateError):
        await service.request_settled(identity)
