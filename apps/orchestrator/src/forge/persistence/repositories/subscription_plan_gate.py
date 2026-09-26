"""PostgreSQL proof for an exact settled subscription PlanOutput proposal."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forge.application.ports.subscription_execution import SubscriptionSettlement
from forge.application.ports.subscription_plan_gate import (
    SettledSubscriptionPlan,
    SubscriptionPlanGateError,
    SubscriptionPlanGateRecord,
)
from forge.domain.approval import (
    ApprovalGate,
    SubscriptionPlanApprovalEvidence,
    SubscriptionPlanProducer,
)
from forge.domain.approval import canonical_digest as evidence_digest
from forge.domain.operation import canonical_digest
from forge.domain.paths import policy_path_key
from forge.domain.plan import ScopedPlanOutput, decode_plan_output
from forge.domain.policy import ProjectPolicy
from forge.domain.resource import WorktreeIdentity
from forge.domain.run import RunState
from forge.domain.subscription import (
    AttemptTelemetry,
    ExecutionEnvelope,
    HandoffStatus,
    LogicalTaskContract,
    SpecialistPurpose,
    TaskBudget,
    TaskHandoff,
    decode_subscription_record,
    encode_subscription_record,
)
from forge.domain.subscription_launch import SubscriptionLaunchTerminalProof
from forge.persistence.models.execution import Approval
from forge.persistence.models.project import Project, ProjectPolicyVersion
from forge.persistence.models.scheduling import (
    SubscriptionScheduledEffect,
    SubscriptionScheduledTask,
    SubscriptionSchedulerRun,
)
from forge.persistence.models.subscription import (
    SubscriptionAttempt,
    SubscriptionClientLaunch,
    SubscriptionDecisionRecord,
    SubscriptionTask,
)
from forge.persistence.models.subscription_plan_gate import SubscriptionPlanGate
from forge.persistence.models.subscription_results import (
    SubscriptionAttemptResult,
    SubscriptionRepairDebit,
)
from forge.persistence.models.subscription_usage import SubscriptionAttemptConsumption
from forge.persistence.repositories.runs import PostgresRunRepository
from forge.persistence.repositories.scheduling import PostgresSchedulingRepository
from forge.persistence.repositories.subscription import PostgresSubscriptionRepository
from forge.persistence.repositories.subscription_budget import PostgresSubscriptionBudgetRepository
from forge.persistence.repositories.subscription_launch import launches_confirmed


class PostgresSubscriptionPlanGateRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def rejection(self, attempt_id: UUID) -> SubscriptionSettlement | None:
        result = await self._session.get(SubscriptionAttemptResult, attempt_id)
        if result is None or result.disposition not in {"plan_repair_queued", "plan_rejected"}:
            return None
        try:
            receipt = result.application_payload
            if receipt is None or canonical_digest(receipt) != result.application_digest:
                raise ValueError
            evidence = SubscriptionPlanApprovalEvidence.model_validate(receipt["evidence"])
            if evidence.producer.attempt_id != attempt_id:
                raise ValueError
        except KeyError, TypeError, ValueError:
            raise SubscriptionPlanGateError("plan rejection receipt differs") from None
        return await self.reject(evidence)

    async def reject(self, evidence: SubscriptionPlanApprovalEvidence) -> SubscriptionSettlement:
        producer = evidence.producer
        await PostgresRunRepository(self._session).get_for_update(producer.run_id)
        result = await self._session.get(
            SubscriptionAttemptResult,
            producer.attempt_id,
            with_for_update=True,
            populate_existing=True,
        )
        replay = result is not None and result.disposition in {
            "plan_repair_queued",
            "plan_rejected",
        }
        await self._proof(
            evidence, create=False, historical=replay, proposal_only=True, rejected=replay
        )
        assert result is not None
        receipt = {
            "schema_version": 1,
            "kind": "rejected_plan",
            "reason": "unregistered_checks",
            "evidence": evidence.model_dump(mode="json"),
        }
        attempt = await self._session.get(SubscriptionAttempt, producer.attempt_id)
        task = await self._session.get(SubscriptionTask, producer.task_id)
        scheduled = await self._session.get(SubscriptionScheduledTask, producer.task_id)
        assert attempt is not None and task is not None and scheduled is not None
        consumption = await self._session.get(SubscriptionAttemptConsumption, attempt.id)
        if consumption is None or consumption.telemetry_payload != attempt.telemetry_payload:
            raise SubscriptionPlanGateError("plan rejection usage proof differs")
        if replay:
            repair = result.disposition == "plan_repair_queued"
            debit = await self._session.get(SubscriptionRepairDebit, attempt.id)
            decision = await self._session.scalar(
                select(SubscriptionDecisionRecord)
                .where(
                    SubscriptionDecisionRecord.run_id == producer.run_id,
                    SubscriptionDecisionRecord.idempotency_key == f"plan-rejection:{attempt.id}",
                )
                .with_for_update()
            )
            if (
                result.application_payload != receipt
                or result.application_digest != canonical_digest(receipt)
                or result.accepted
                or attempt.status != "terminal"
                or (debit is not None) != repair
                or decision is None
                or decision.task_row_id != task.id
                or decision.attempt_id != attempt.id
                or decision.payload != encode_subscription_record(_plan_rejection(attempt, repair))
            ):
                raise SubscriptionPlanGateError("plan rejection replay differs")
            return SubscriptionSettlement(False, result.disposition, True)
        # Recompute the semantic contradiction against the exact immutable policy.
        run = await PostgresRunRepository(self._session).get_for_update(producer.run_id)
        project = await self._session.get(Project, run.project_id, with_for_update=True)
        record = await self._session.scalar(
            select(ProjectPolicyVersion)
            .where(
                ProjectPolicyVersion.project_id == run.project_id,
                ProjectPolicyVersion.version == evidence.policy_version,
            )
            .with_for_update()
        )
        if record is None or project is None:
            raise SubscriptionPlanGateError("plan rejection policy is absent")
        policy = ProjectPolicy.model_validate(record.document)
        policy_digest = hashlib.sha256(
            json.dumps(
                record.document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        raw_decision = result.result_payload["decision"]
        assert isinstance(raw_decision, dict)
        plan = decode_plan_output(raw_decision["value"])
        if (
            project.current_policy_version != record.version
            or policy.id != run.project_id
            or policy.version != record.version
            or record.document_schema_version != 1
            or policy_digest != record.policy_digest
            or not isinstance(plan, ScopedPlanOutput)
            or set(plan.required_checks) <= {command.name for command in policy.commands}
            or dict(evidence.required_checks) != {name: "planned" for name in plan.required_checks}
            or evidence.plan_digest != producer.plan_digest
            or attempt.status != "reconciling"
            or result.application_payload is not None
            or result.application_digest is not None
        ):
            raise SubscriptionPlanGateError("plan rejection has no current semantic proof")
        repair = (
            scheduled.repairs < scheduled.max_repairs
            and await PostgresSubscriptionBudgetRepository(self._session).try_debit_repair(
                task.run_id, task.id, attempt.id
            )
        )
        subscription = PostgresSubscriptionRepository(self._session)
        await subscription.record_decision(
            _plan_rejection(attempt, repair), idempotency_key=f"plan-rejection:{attempt.id}"
        )
        decision = await self._session.scalar(
            select(SubscriptionDecisionRecord).where(
                SubscriptionDecisionRecord.run_id == task.run_id,
                SubscriptionDecisionRecord.idempotency_key == f"plan-rejection:{attempt.id}",
            )
        )
        assert decision is not None
        decision.task_row_id, decision.attempt_id = task.id, attempt.id
        scheduled.repairs += int(repair)
        task.state = "queued" if repair else "terminal"
        task.version += 1
        attempt.status = "terminal"
        result.accepted = False
        result.disposition = "plan_repair_queued" if repair else "plan_rejected"
        result.application_payload, result.application_digest = receipt, canonical_digest(receipt)
        await PostgresSchedulingRepository(self._session).reconcile_expired(
            task.run_id, task.id, retry=repair
        )
        await self._session.flush()
        return SubscriptionSettlement(False, result.disposition)

    async def proposal(self, attempt_id: UUID) -> SettledSubscriptionPlan:
        """Read exact retained inputs; publication revalidates them under its locks."""
        attempt = await self._session.get(SubscriptionAttempt, attempt_id)
        result = await self._session.get(SubscriptionAttemptResult, attempt_id)
        if (
            attempt is None
            or result is None
            or result.disposition not in {"decision_pending", "plan_approval"}
        ):
            raise SubscriptionPlanGateError("settled subscription plan is absent")
        try:
            payload = result.result_payload
            if type(payload.get("schema_version")) is not int or payload["schema_version"] != 4:
                raise ValueError
            if canonical_digest(payload) != result.result_digest:
                raise ValueError
            context, decision, raw_telemetry = (
                payload["proposal_context"],
                payload["decision"],
                payload["telemetry"],
            )
            if (
                not isinstance(context, dict)
                or not isinstance(decision, dict)
                or not isinstance(raw_telemetry, dict)
            ):
                raise TypeError
            if decision.get("type") != "PlanOutput" or payload.get("effective_failure") is not None:
                raise ValueError
            plan = decode_plan_output(decision["value"])
            telemetry = decode_subscription_record(raw_telemetry)
            if not isinstance(telemetry, AttemptTelemetry):
                raise TypeError
            producer = SubscriptionPlanProducer(
                attempt_id=attempt.id,
                run_id=attempt.run_id,
                task_id=attempt.task_row_id,
                plan_attempt=attempt.attempt_number,
                plan_digest=hashlib.sha256(
                    plan.model_dump_json(by_alias=False).encode()
                ).hexdigest(),
                task_digest=canonical_digest(context["task"]),
                envelope_digest=canonical_digest(context["envelope"]),
                budget_digest=canonical_digest(context["budget"]),
                route_digest=canonical_digest(context["route"]),
                telemetry={
                    "input_tokens": telemetry.input_tokens,
                    "output_tokens": telemetry.output_tokens,
                    "duration_ms": telemetry.duration_ms,
                },
            )
        except KeyError, TypeError, ValueError:
            raise SubscriptionPlanGateError("settled subscription plan is invalid") from None
        return SettledSubscriptionPlan(plan, producer, result.result_digest)

    async def mark_applied(self, evidence: SubscriptionPlanApprovalEvidence) -> None:
        """Complete the same transaction that publishes the human plan gate."""
        await self.verify(evidence)
        producer = evidence.producer
        result = await self._session.get(SubscriptionAttemptResult, producer.attempt_id)
        attempt = await self._session.get(SubscriptionAttempt, producer.attempt_id)
        task = await self._session.get(SubscriptionTask, producer.task_id)
        scheduled = await self._session.scalar(
            select(SubscriptionScheduledTask)
            .where(
                SubscriptionScheduledTask.run_id == producer.run_id,
                SubscriptionScheduledTask.task_id == producer.task_id,
            )
            .with_for_update()
        )
        if result is None or attempt is None or task is None or scheduled is None:
            raise SubscriptionPlanGateError("subscription plan application source is absent")
        if result.disposition == "plan_approval":
            return
        result.disposition, result.accepted = "plan_approval", True
        attempt.status = "terminal"
        task.state = scheduled.state = "blocked"
        task.version += 1
        scheduled.lease_owner = scheduled.lease_expires_at = None
        await self._session.flush()

    async def resume_prepared(
        self, evidence: SubscriptionPlanApprovalEvidence, *, worktree_id: str, approval_id: UUID
    ) -> UUID:
        """Queue the approved primary in the preparation transition transaction."""
        producer = evidence.producer
        run = await PostgresRunRepository(self._session).get_for_update(producer.run_id)
        await self.verify(evidence, historical=True)
        approval = await self._session.get(Approval, approval_id, with_for_update=True)
        try:
            # Database provisioning does not change the worktree resource name.
            expected_resource = WorktreeIdentity.for_run(
                run.project_id, run.id, run.branch_name or "", False
            ).worktree_name
        except TypeError, ValueError:
            raise SubscriptionPlanGateError("prepared worktree identity is invalid") from None
        if (
            run.state not in {RunState.PREPARING_WORKTREE, RunState.IMPLEMENTING}
            or run.pending_gate is not None
            or not worktree_id
            or not run.worktree_path
            or worktree_id != expected_resource
            or approval is None
            or approval.run_id != run.id
            or approval.gate != "plan"
            or approval.evidence_digest != evidence_digest(evidence)
            or approval.policy_version != run.policy_version
            or approval.invalidated_at is not None
        ):
            raise SubscriptionPlanGateError("prepared primary lacks current approval authority")
        attempt = await self._session.get(
            SubscriptionAttempt, producer.attempt_id, with_for_update=True
        )
        task = await self._session.get(SubscriptionTask, producer.task_id, with_for_update=True)
        scheduled = await self._session.scalar(
            select(SubscriptionScheduledTask)
            .where(
                SubscriptionScheduledTask.run_id == run.id,
                SubscriptionScheduledTask.task_id == producer.task_id,
            )
            .with_for_update()
        )
        result = await self._session.get(SubscriptionAttemptResult, producer.attempt_id)
        if (
            attempt is None
            or task is None
            or scheduled is None
            or result is None
            or result.disposition != "plan_approval"
            or not result.accepted
            or attempt.task_version is None
        ):
            raise SubscriptionPlanGateError("prepared primary source is absent")
        decision_source = result.result_payload["decision"]
        context_source = result.result_payload["proposal_context"]
        if not isinstance(decision_source, dict) or not isinstance(context_source, dict):
            raise SubscriptionPlanGateError("prepared primary source differs")
        plan = decode_plan_output(decision_source["value"])
        original = decode_subscription_record(context_source["task"])
        assert isinstance(original, LogicalTaskContract)
        prepared = (
            replace(original, owned_paths=plan.owned_paths, named_checks=plan.required_checks)
            if isinstance(plan, ScopedPlanOutput)
            else original
        )
        prepared_payload = encode_subscription_record(prepared)
        prepared_paths = tuple(policy_path_key(path) for path in prepared.owned_paths)
        if run.state is RunState.IMPLEMENTING:
            if (
                scheduled.worktree_id != worktree_id
                or task.version < attempt.task_version + 3
                or (
                    task.version == attempt.task_version + 3
                    and (
                        task.payload != prepared_payload
                        or tuple(scheduled.owned_paths) != prepared_paths
                    )
                )
            ):
                raise SubscriptionPlanGateError("prepared primary replay differs")
            return task.id
        await self._current(attempt, task, run.id, task.id, applied=True)
        scheduled.worktree_id = worktree_id
        task.payload = prepared_payload
        scheduled.owned_paths = list(prepared_paths)
        task.state = scheduled.state = "queued"
        task.version += 1
        await self._session.flush()
        return task.id

    async def get(self, attempt_id: UUID) -> SubscriptionPlanGateRecord | None:
        row = await self._session.get(SubscriptionPlanGate, attempt_id)
        return None if row is None else _record(row)

    async def record(
        self, evidence: SubscriptionPlanApprovalEvidence
    ) -> SubscriptionPlanGateRecord:
        return await self._proof(evidence, create=True, historical=False)

    async def verify(
        self, evidence: SubscriptionPlanApprovalEvidence, *, historical: bool = False
    ) -> SubscriptionPlanGateRecord:
        """Verify an existing gate without creating missing approval authority."""
        return await self._proof(evidence, create=False, historical=historical)

    async def _proof(
        self,
        evidence: SubscriptionPlanApprovalEvidence,
        *,
        create: bool,
        historical: bool,
        proposal_only: bool = False,
        rejected: bool = False,
    ) -> SubscriptionPlanGateRecord:
        producer = evidence.producer
        run = await PostgresRunRepository(self._session).get_for_update(producer.run_id)
        attempt = await self._session.get(
            SubscriptionAttempt, producer.attempt_id, with_for_update=True
        )
        result = await self._session.get(
            SubscriptionAttemptResult, producer.attempt_id, with_for_update=True
        )
        task = await self._session.get(SubscriptionTask, producer.task_id, with_for_update=True)
        row = await self._session.get(
            SubscriptionPlanGate, producer.attempt_id, with_for_update=True
        )
        if (run.policy_version, run.base_ref, run.base_sha) != (
            evidence.policy_version,
            evidence.base_ref,
            evidence.base_sha,
        ):
            raise SubscriptionPlanGateError("subscription plan run binding differs")
        if not historical:
            if row is None:
                if run.state is not RunState.PLANNING or run.pending_gate is not None:
                    raise SubscriptionPlanGateError("subscription plan run is not planning")
            elif (
                run.state is not RunState.AWAITING_PLAN_APPROVAL
                or run.pending_gate is not ApprovalGate.PLAN
                or run.pending_evidence_digest != evidence_digest(evidence)
            ):
                raise SubscriptionPlanGateError("subscription plan gate is no longer pending")
        if (
            attempt is None
            or result is None
            or task is None
            or (not create and row is None and not proposal_only)
        ):
            raise SubscriptionPlanGateError("subscription plan source is absent")
        if (
            attempt.run_id != producer.run_id
            or attempt.task_row_id != producer.task_id
            or attempt.attempt_number != producer.plan_attempt
            or result.result_digest != evidence.result_digest
            or task.run_id != producer.run_id
            or task.parent_task_id is not None
            or attempt.status not in {"reconciling", "terminal"}
        ):
            raise SubscriptionPlanGateError("subscription plan source differs")
        try:
            payload = result.result_payload
            launch_proof = SubscriptionLaunchTerminalProof.model_validate(
                payload.get("launch_proof")
            )
            context, decision = payload["proposal_context"], payload["decision"]
            if not isinstance(context, dict) or not isinstance(decision, dict):
                raise TypeError
            frozen_task = decode_subscription_record(context["task"])
            envelope = decode_subscription_record(context["envelope"])
            route = decode_subscription_record(context["route"])
            budget = decode_subscription_record(context["budget"])
            telemetry_payload = payload["telemetry"]
            if not isinstance(telemetry_payload, dict):
                raise TypeError
            telemetry = decode_subscription_record(telemetry_payload)
            plan = decode_plan_output(decision["value"])
            current_envelope = await PostgresSubscriptionRepository(self._session).envelope_for_run(
                producer.run_id
            )
            if (
                type(payload.get("schema_version")) is not int
                or payload["schema_version"] != 4
                or canonical_digest(payload) != result.result_digest
                or result.disposition
                not in (
                    {"plan_repair_queued", "plan_rejected"}
                    if rejected
                    else {"decision_pending", "plan_approval"}
                )
                or result.accepted != (result.disposition == "plan_approval")
                or (result.disposition == "plan_approval" and row is None)
                or payload.get("effective_failure") is not None
                or decision.get("type") != "PlanOutput"
                or not isinstance(frozen_task, LogicalTaskContract)
                or frozen_task.parent_task_id is not None
                or frozen_task.purpose is not SpecialistPurpose.PRIMARY
                or not isinstance(envelope, ExecutionEnvelope)
                or not isinstance(budget, TaskBudget)
                or not isinstance(telemetry, AttemptTelemetry)
                or frozen_task.route != route
                or frozen_task.budget != budget
                or frozen_task.run_id != producer.run_id
                or frozen_task.task_id != producer.task_id
                or envelope.run_id != producer.run_id
                or current_envelope != envelope
                or envelope.safety_policy_version != evidence.policy_version
                or envelope.route_for(SpecialistPurpose.PRIMARY) != route
                or canonical_digest(context["task"]) != producer.task_digest
                or producer.task_digest != attempt.task_digest
                or canonical_digest(context["envelope"]) != producer.envelope_digest
                or producer.envelope_digest != attempt.envelope_digest
                or canonical_digest(context["budget"]) != producer.budget_digest
                or canonical_digest(context["route"]) != producer.route_digest
                or attempt.route_payload != context["route"]
                or context["task_version"] != attempt.task_version
                or context["candidate_epoch"] != attempt.candidate_epoch
                or attempt.telemetry_payload != encode_subscription_record(telemetry)
                or dict(producer.telemetry)
                != {
                    "input_tokens": telemetry.input_tokens,
                    "output_tokens": telemetry.output_tokens,
                    "duration_ms": telemetry.duration_ms,
                }
                or hashlib.sha256(plan.model_dump_json(by_alias=False).encode()).hexdigest()
                != producer.plan_digest
            ):
                raise ValueError
        except KeyError, TypeError, ValueError:
            raise SubscriptionPlanGateError("subscription plan evidence is invalid") from None
        launches = (
            await self._session.scalars(
                select(SubscriptionClientLaunch)
                .where(SubscriptionClientLaunch.attempt_id == attempt.id)
                .with_for_update()
            )
        ).all()
        if not launches_confirmed(
            launches, launch_proof, require_decision=True, worker_identity=attempt.lease_owner
        ):
            raise SubscriptionPlanGateError("subscription plan launch evidence differs")
        if not historical:
            await self._current(
                attempt,
                task,
                producer.run_id,
                producer.task_id,
                applied=result.disposition == "plan_approval",
            )
        expected = SubscriptionPlanGateRecord(
            producer.attempt_id,
            producer.run_id,
            producer.task_id,
            evidence.plan_digest,
            evidence_digest(evidence),
            evidence.result_digest,
            producer.envelope_digest,
            producer.budget_digest,
            producer.route_digest,
        )
        if proposal_only:
            if row is not None:
                raise SubscriptionPlanGateError("approved plan cannot be rejected")
            return expected
        snapshot = {
            "proposal_context": context,
            "decision": decision,
            "launch_proof": launch_proof.model_dump(mode="json"),
        }
        if row is not None:
            if _record(row) != expected or row.snapshot != snapshot:
                raise SubscriptionPlanGateError("subscription plan replay conflicts")
            return expected
        if not create:
            raise SubscriptionPlanGateError("subscription plan gate is absent")
        self._session.add(
            SubscriptionPlanGate(
                attempt_id=producer.attempt_id,
                run_id=producer.run_id,
                task_id=producer.task_id,
                plan_digest=evidence.plan_digest,
                evidence_digest=evidence_digest(evidence),
                result_digest=evidence.result_digest,
                envelope_digest=producer.envelope_digest,
                budget_digest=producer.budget_digest,
                route_digest=producer.route_digest,
                snapshot=snapshot,
            )
        )
        await self._session.flush()
        return expected

    async def _current(
        self,
        attempt: SubscriptionAttempt,
        task: SubscriptionTask,
        run_id: UUID,
        task_id: UUID,
        *,
        applied: bool,
    ) -> None:
        scheduled = await self._session.scalar(
            select(SubscriptionScheduledTask)
            .where(
                SubscriptionScheduledTask.run_id == run_id,
                SubscriptionScheduledTask.task_id == task_id,
            )
            .with_for_update()
        )
        scheduler = await self._session.get(SubscriptionSchedulerRun, run_id, with_for_update=True)
        effects = await self._session.scalar(
            select(SubscriptionScheduledEffect.id)
            .where(
                SubscriptionScheduledEffect.run_id == run_id,
                SubscriptionScheduledEffect.task_id == task_id,
                SubscriptionScheduledEffect.state.in_(("admitted", "reconciling")),
            )
            .limit(1)
        )
        if (
            scheduled is None
            or scheduler is None
            or effects is not None
            or task.pause_requested
            or task.cancel_requested
            or scheduled.pause_requested
            or scheduled.cancel_requested
            or task.state != ("blocked" if applied else "reconciling")
            or scheduled.state != ("blocked" if applied else "reconciling")
            or attempt.task_version is None
            or task.version != attempt.task_version + (2 if applied else 1)
            or canonical_digest(task.payload) != attempt.task_digest
            or attempt.candidate_epoch != scheduler.candidate_epoch
            or scheduler.candidate_state != "open"
            or scheduled.lease_owner != (None if applied else attempt.lease_owner)
            or (applied and scheduled.lease_expires_at is not None)
            or (applied and attempt.status != "terminal")
            or scheduled.lease_generation != attempt.lease_generation
        ):
            raise SubscriptionPlanGateError("subscription plan source is no longer current")


def _plan_rejection(attempt: SubscriptionAttempt, repair: bool) -> TaskHandoff:
    return TaskHandoff(
        run_id=attempt.run_id,
        task_id=attempt.task_row_id,
        attempt_id=attempt.id,
        status=HandoffStatus.FAILED if repair else HandoffStatus.REPAIRS_EXHAUSTED,
        summary="Plan rejected: required checks must name registered policy commands",
    )


def _record(row: SubscriptionPlanGate) -> SubscriptionPlanGateRecord:
    return SubscriptionPlanGateRecord(
        row.attempt_id,
        row.run_id,
        row.task_id,
        row.plan_digest,
        row.evidence_digest,
        row.result_digest,
        row.envelope_digest,
        row.budget_digest,
        row.route_digest,
    )
