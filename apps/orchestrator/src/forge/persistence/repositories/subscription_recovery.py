"""Locked, causally bound operator recovery over settled subscription evidence."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from forge.application.ports.subscription_gateway import RoleDecisionRejection
from forge.domain.event import RunEvent as DomainRunEvent
from forge.domain.operation import canonical_digest
from forge.domain.paths import policy_path_key
from forge.domain.plan import decode_plan_output
from forge.domain.run import RunState
from forge.domain.subscription import (
    BoundReassignDecision,
    BoundScopeResponseDecision,
    DelegateDecision,
    ForwardFeedbackDecision,
    HandoffStatus,
    LogicalTaskContract,
    TaskHandoff,
    WaitDecision,
    decode_subscription_record,
    encode_subscription_record,
)
from forge.domain.subscription_decision_policy import (
    decision_allowed,
    decision_kind,
    is_approved_plan_primary_contract,
)
from forge.domain.subscription_launch import SubscriptionLaunchTerminalProof
from forge.domain.subscription_plan_contract import approved_implementation_contract
from forge.domain.subscription_recovery import (
    RECOVERY_WORKER_FRESHNESS_SECONDS,
    RecoveryAction,
    RecoveryReceipt,
    RecoveryReceiptRecord,
    RecoverySnapshot,
)
from forge.observability.redaction import redact_value
from forge.persistence.models.execution import Approval, RunEvent
from forge.persistence.models.project import Project
from forge.persistence.models.run import Run
from forge.persistence.models.scheduling import (
    SubscriptionScheduledEffect,
    SubscriptionScheduledTask,
    SubscriptionSchedulerRun,
)
from forge.persistence.models.subscription import (
    SubscriptionAttempt,
    SubscriptionClientLaunch,
    SubscriptionDecisionRecord,
    SubscriptionEnvelope,
    SubscriptionTask,
)
from forge.persistence.models.subscription_plan_gate import SubscriptionPlanGate
from forge.persistence.models.subscription_recovery import (
    SubscriptionApplicationDiagnostic,
    SubscriptionContractRevision,
    SubscriptionRecoveryReceipt,
    SubscriptionRecoverySigningKey,
    SubscriptionRecoveryWorker,
)
from forge.persistence.models.subscription_results import SubscriptionAttemptResult
from forge.persistence.models.subscription_usage import SubscriptionAttemptConsumption
from forge.persistence.queries.subscription_usage_proofs import applied_decision, historical_source
from forge.persistence.repositories.commands import PostgresCommandRepository
from forge.persistence.repositories.events import PostgresEventRepository
from forge.persistence.repositories.scheduling import PostgresSchedulingRepository
from forge.persistence.repositories.subscription_budget import PostgresSubscriptionBudgetRepository
from forge.persistence.repositories.subscription_launch import launches_confirmed
from forge.persistence.repositories.subscription_resumption import verify_paused_attempts


class RecoveryConflict(RuntimeError):
    """The preview, idempotency key, or current authority no longer matches."""


def _stored_decision(payload: object) -> object | None:
    if not isinstance(payload, dict):
        return None
    if payload.get("type") == "PlanOutput":
        return decode_plan_output(payload["value"])
    if "record" in payload:
        return decode_subscription_record(payload)
    return None


def _stored_role_rejection(payload: object) -> RoleDecisionRejection | None:
    if not isinstance(payload, dict) or set(payload) != {"kind", "reason_code"}:
        return None
    try:
        return RoleDecisionRejection(**payload)
    except (TypeError, ValueError):
        return None


def _forbidden_role_rejection(
    result: SubscriptionAttemptResult, contract: LogicalTaskContract
) -> bool:
    payload = result.result_payload
    if (
        result.disposition != "role_rejected"
        or payload.get("decision") is not None
        or payload.get("failure") != "protocol"
        or payload.get("effective_failure") != "protocol"
    ):
        return False
    rejection = _stored_role_rejection(payload.get("role_rejection"))
    return rejection is not None and not decision_allowed(
        rejection.kind, contract.purpose, RunState.IMPLEMENTING
    )


_EXPLANATIONS = {
    "eligible": "The selected recovery can be applied to the current stopped result.",
    "source_missing": "The selected task or result is unavailable.",
    "source_changed": "The saved result or task contract changed; inspect it again.",
    "run_controlled": "The run is paused, cancelled, gated, or no longer implementing.",
    "task_controlled": "The task has a pending pause or cancellation.",
    "candidate_changed": "The candidate changed or is no longer open.",
    "policy_changed": "The project policy differs from the run's approved policy.",
    "worker_unavailable": "A compatible worker has not reported recently.",
    "effect_uncertain": "A provider or controlled effect still needs stopped evidence.",
    "budget_exhausted": "The approved attempt or repair budget is exhausted.",
    "approval_missing": "The approved plan evidence is unavailable or invalidated.",
    "not_stale_plan": "This result does not match the approved planning-contract defect.",
    "invalid_result": "This saved decision is invalid for its role or phase.",
    "application_not_retryable": "The saved application has no changed prerequisite to retry.",
    "unsupported_source": "This result state cannot be recovered with the selected action.",
}


def _is_prior_role_rejection(result: SubscriptionAttemptResult) -> bool:
    return (
        result.disposition == "role_rejected"
        and isinstance(result.application_payload, dict)
        and result.application_payload.get("kind") == "role_rejection"
        and canonical_digest(result.application_payload) == result.application_digest
    )


class PostgresSubscriptionRecoveryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _verified_decision(
        self, task: SubscriptionTask, attempt: SubscriptionAttempt,
        result: SubscriptionAttemptResult,
    ) -> object | None:
        envelope = await self._session.get(SubscriptionEnvelope, attempt.run_id)
        consumption = await self._session.get(SubscriptionAttemptConsumption, attempt.id)
        launches = (
            await self._session.scalars(
                select(SubscriptionClientLaunch).where(SubscriptionClientLaunch.attempt_id == attempt.id)
            )
        ).all()
        record = await self._session.scalar(
            select(SubscriptionDecisionRecord).where(
                SubscriptionDecisionRecord.attempt_id == attempt.id,
            )
        )
        source = historical_source(
            attempt, task, result, consumption, envelope,
            launches[0] if len(launches) == 1 else None, len(launches),
        )
        return source.decision if source is not None and applied_decision(source, attempt, result, record) else None

    async def _queued_continuation_proven(
        self, run_id: UUID, task: SubscriptionTask,
        attempt: SubscriptionAttempt, result: SubscriptionAttemptResult,
    ) -> bool:
        if result.disposition == "stale" and not result.accepted:
            receipt = result.application_payload
            if not isinstance(receipt, dict) or receipt.get("kind") != "paused_subscription_attempt":
                return False
            try:
                raw_resume_id = receipt["resume_command_id"]
                if not isinstance(raw_resume_id, str):
                    return False
                resume_id = UUID(raw_resume_id)
                resume = await PostgresCommandRepository(session=self._session).get(resume_id)
                verified = await verify_paused_attempts(self._session, resume)
            except (KeyError, TypeError, ValueError, RuntimeError):
                return False
            return resume.run_id == run_id and any(
                item.attempt_id == attempt.id and item.application_digest == result.application_digest
                for item in verified
            )
        decision = await self._verified_decision(task, attempt, result)
        # Preflight only blocks a queued task. The immutable applied-decision
        # proof above supplies authority; wake causes also include scope and
        # pending-feedback events before any child becomes terminal.
        return isinstance(
            decision,
            (
                BoundScopeResponseDecision,
                DelegateDecision,
                WaitDecision,
                BoundReassignDecision,
                ForwardFeedbackDecision,
            ),
        )

    async def _wait_snapshot_allows(
        self, reference: object, attempt: SubscriptionAttempt, *, matching_current: bool
    ) -> bool:
        if reference is None:
            return True
        if not isinstance(reference, str):
            return False
        try:
            reference_id = UUID(reference)
        except ValueError:
            return False
        recorded = await self._session.get(SubscriptionAttempt, reference_id)
        if (
            recorded is None
            or recorded.run_id != attempt.run_id
            or recorded.task_row_id != attempt.task_row_id
        ):
            return False
        if recorded.attempt_number < attempt.attempt_number:
            return True
        return matching_current and recorded.id == attempt.id

    async def _attention_changed(
        self, run_id: UUID, task_id: UUID, attempt_id: UUID, *,
        was_attention: bool, is_attention: bool, reason_code: str,
        previous_reason: str | None = None,
    ) -> None:
        if was_attention == is_attention and (
            not is_attention or previous_reason == reason_code
        ):
            return
        run = await self._session.get(Run, run_id)
        if run is None:
            raise RecoveryConflict("recovery attention run is missing")
        await PostgresEventRepository(self._session).append(DomainRunEvent(
            run_id=run_id, run_version=run.version,
            event_type="run.subscription_recovery_attention_changed",
            payload={
                "task_id": str(task_id), "attempt_id": str(attempt_id),
                "attention": is_attention, "reason_code": reason_code,
            },
        ))

    async def _latest_attempt_id(self, task_id: UUID) -> UUID | None:
        attempt_id: UUID | None = await self._session.scalar(
            select(SubscriptionAttempt.id)
            .where(SubscriptionAttempt.task_row_id == task_id)
            .order_by(SubscriptionAttempt.attempt_number.desc())
            .limit(1)
        )
        return attempt_id

    async def _resolve_diagnostic(
        self,
        diagnostic: SubscriptionApplicationDiagnostic,
        *,
        resolution: str,
        next_retry_at: datetime | None,
    ) -> None:
        was_attention = diagnostic.resolution == "attention"
        diagnostic.resolution = resolution
        diagnostic.next_retry_at = next_retry_at
        await self._attention_changed(
            diagnostic.run_id,
            diagnostic.task_id,
            diagnostic.attempt_id,
            was_attention=was_attention,
            is_attention=False,
            reason_code=diagnostic.reason_code,
        )

    async def correction_feedback(self, task_id: UUID, contract_digest: str) -> dict[str, str] | None:
        row = await self._session.scalar(
            select(SubscriptionAttemptResult)
            .join(SubscriptionAttempt)
            .where(
                SubscriptionAttempt.task_row_id == task_id,
                SubscriptionAttempt.task_digest == contract_digest,
                SubscriptionAttemptResult.disposition == "role_correction_queued",
            )
            .order_by(SubscriptionAttempt.attempt_number.desc())
            .limit(1)
        )
        if row is None:
            return None
        rejection = row.result_payload.get("role_rejection")
        if not isinstance(rejection, dict):
            return None
        kind, reason = rejection.get("kind"), rejection.get("reason_code")
        if not isinstance(kind, str) or not isinstance(reason, str):
            return None
        return {"kind": kind, "reason_code": reason, "source_attempt_id": str(row.attempt_id)}

    async def block_stale_queued_contract(
        self, run_id: UUID, task: SubscriptionTask, scheduled: SubscriptionScheduledTask
    ) -> None:
        source_id = await self._latest_attempt_id(task.id)
        if source_id is None:
            raise RecoveryConflict("approved planning source is unavailable")
        now = datetime.now(UTC)
        row = await self._session.get(SubscriptionApplicationDiagnostic, source_id)
        was_attention = row is not None and row.resolution == "attention"
        previous_reason = row.reason_code if row is not None else None
        if row is None:
            self._session.add(
                SubscriptionApplicationDiagnostic(
                    attempt_id=source_id,
                    run_id=run_id,
                    task_id=task.id,
                    classification="invalid",
                    reason_code="approved_plan_contract_stale",
                    resolution="attention",
                    failed_applications=0,
                    first_failure_at=now,
                    last_failure_at=now,
                )
            )
        else:
            row.classification = "invalid"
            row.reason_code = "approved_plan_contract_stale"
            row.resolution = "attention"
            row.next_retry_at = None
            row.last_failure_at = now
        if task.state != "blocked" or scheduled.state != "blocked":
            task.state = "blocked"
            task.version += 1
            scheduled.state = "blocked"
        await self._attention_changed(
            run_id, task.id, source_id, was_attention=was_attention,
            is_attention=True, reason_code="approved_plan_contract_stale",
            previous_reason=previous_reason,
        )
        await self._session.flush()

    async def _approved_source(
        self, run_id: UUID, task_id: UUID
    ) -> tuple[SubscriptionPlanGate, Approval, RunEvent] | None:
        prepared = await self._session.scalar(
            select(RunEvent)
            .where(
                RunEvent.run_id == run_id,
                RunEvent.event_type == "run.worktree_prepared",
                RunEvent.payload["primary_task_id"].astext == str(task_id),
            )
            .order_by(RunEvent.run_version.desc())
            .limit(1)
        )
        if prepared is None:
            return None
        try:
            gate_id = UUID(str(prepared.payload["plan_attempt_id"]))
            approval_id = UUID(str(prepared.payload["approval_id"]))
        except KeyError, TypeError, ValueError:
            return None
        gate = await self._session.get(SubscriptionPlanGate, gate_id)
        approval = await self._session.get(Approval, approval_id)
        planning_result = await self._session.get(SubscriptionAttemptResult, gate_id)
        if (
            gate is None
            or approval is None
            or planning_result is None
            or gate.run_id != run_id
            or gate.task_id != task_id
            or approval.run_id != run_id
            or approval.gate != "plan"
            or approval.evidence_digest != gate.evidence_digest
            or approval.invalidated_at is not None
            or planning_result.result_digest != gate.result_digest
            or planning_result.disposition != "plan_approval"
            or not planning_result.accepted
        ):
            return None
        return gate, approval, prepared

    async def attempt_run_id(self, attempt_id: UUID) -> UUID:
        run_id = await self._session.scalar(
            select(SubscriptionAttempt.run_id).where(SubscriptionAttempt.id == attempt_id)
        )
        if run_id is None:
            raise RecoveryConflict("application source is missing")
        return run_id

    async def due(self, attempt_id: UUID) -> bool:
        return await self.due_version(attempt_id) is not None

    async def due_version(self, attempt_id: UUID) -> int | None:
        if not await self._application_pending(attempt_id):
            return None
        attempt = await self._session.get(SubscriptionAttempt, attempt_id)
        assert attempt is not None
        task = await self._session.get(SubscriptionTask, attempt.task_row_id)
        scheduled = await self._session.get(SubscriptionScheduledTask, attempt.task_row_id)
        if (
            task is None
            or scheduled is None
            or any(
                (
                    task.pause_requested,
                    task.cancel_requested,
                    scheduled.pause_requested,
                    scheduled.cancel_requested,
                )
            )
        ):
            return None
        row = await self._session.get(SubscriptionApplicationDiagnostic, attempt_id)
        if row is None:
            return task.version
        if row.resolution == "waiting" and row.reason_code == "run_controlled":
            run = await self._session.get(Run, row.run_id)
            return (
                task.version
                if run is not None
                and run.state
                not in {
                    RunState.PAUSED.value,
                    RunState.CANCELLED.value,
                }
                else None
            )
        if row.resolution == "waiting":
            # Older persisted prerequisite waits had no due time. Re-enter the
            # bounded retry schedule instead of leaving them invisible forever.
            return task.version
        return (
            task.version
            if row.resolution == "scheduled"
            and (row.next_retry_at is None or row.next_retry_at <= datetime.now(UTC))
            else None
        )

    async def record_failure(
        self,
        attempt_id: UUID,
        *,
        classification: str,
        reason_code: str,
        observed_task_version: int | None = None,
    ) -> None:
        attempt = await self._session.get(SubscriptionAttempt, attempt_id)
        if attempt is None:
            raise RecoveryConflict("application diagnostic source is missing")
        await self._session.get(Run, attempt.run_id, with_for_update=True)
        task = await self._session.get(
            SubscriptionTask, attempt.task_row_id, with_for_update=True, populate_existing=True
        )
        scheduled = await self._session.get(
            SubscriptionScheduledTask,
            attempt.task_row_id,
            with_for_update=True,
            populate_existing=True,
        )
        if (
            task is None
            or scheduled is None
            or any(
                (
                    task.pause_requested,
                    task.cancel_requested,
                    scheduled.pause_requested,
                    scheduled.cancel_requested,
                )
            )
            or (observed_task_version is not None and task.version != observed_task_version)
        ):
            return
        now = datetime.now(UTC)
        row = await self._session.get(
            SubscriptionApplicationDiagnostic, attempt_id, with_for_update=True
        )
        if not await self._application_pending(attempt_id, locked=True):
            if row is not None:
                await self._resolve_diagnostic(row, resolution="superseded", next_retry_at=None)
                await self._session.flush()
            return
        was_attention = row is not None and row.resolution == "attention"
        previous_reason = row.reason_code if row is not None else None
        if row is None:
            row = SubscriptionApplicationDiagnostic(
                attempt_id=attempt_id,
                run_id=attempt.run_id,
                task_id=attempt.task_row_id,
                classification=classification,
                reason_code=reason_code,
                resolution="scheduled",
                failed_applications=0,
                first_failure_at=now,
                last_failure_at=now,
            )
            self._session.add(row)
        row.classification = classification
        row.reason_code = reason_code
        row.failed_applications += 1
        row.last_failure_at = now
        delays = (5, 15, 60)
        if (
            classification == "temporary"
            or (classification == "prerequisite" and reason_code != "run_controlled")
        ) and row.failed_applications <= len(delays):
            row.resolution = "scheduled"
            row.next_retry_at = now + timedelta(seconds=delays[row.failed_applications - 1])
        elif classification == "prerequisite" and reason_code == "run_controlled":
            row.resolution = "waiting"
            row.next_retry_at = None
        else:
            row.resolution = "attention"
            row.next_retry_at = None
        await self._attention_changed(
            attempt.run_id,
            attempt.task_row_id,
            attempt_id,
            was_attention=was_attention,
            is_attention=row.resolution == "attention",
            reason_code=reason_code,
            previous_reason=previous_reason,
        )
        await self._session.flush()

    async def record_success(self, attempt_id: UUID) -> None:
        attempt = await self._session.get(SubscriptionAttempt, attempt_id)
        if attempt is None:
            raise RecoveryConflict("application diagnostic source is missing")
        await self._session.get(Run, attempt.run_id, with_for_update=True)
        row = await self._session.get(
            SubscriptionApplicationDiagnostic, attempt_id, with_for_update=True
        )
        if row is not None:
            if await self._application_pending(attempt_id, locked=True):
                await self._resolve_diagnostic(
                    row, resolution="scheduled", next_retry_at=datetime.now(UTC)
                )
            else:
                await self._resolve_diagnostic(row, resolution="applied", next_retry_at=None)
            await self._session.flush()

    async def schedule_pending_resume(self, attempt_id: UUID) -> None:
        """Retry a verified resumed source now while keeping diagnostic history."""
        row = await self._session.get(
            SubscriptionApplicationDiagnostic, attempt_id, with_for_update=True
        )
        if row is not None and await self._application_pending(attempt_id, locked=True):
            await self._resolve_diagnostic(
                row, resolution="scheduled", next_retry_at=datetime.now(UTC)
            )
            await self._session.flush()

    async def _application_pending(self, attempt_id: UUID, *, locked: bool = False) -> bool:
        result = await self._session.get(
            SubscriptionAttemptResult, attempt_id, with_for_update=locked
        )
        attempt = await self._session.get(SubscriptionAttempt, attempt_id)
        if result is None or attempt is None:
            return False
        run = await self._session.get(Run, attempt.run_id)
        task = await self._session.get(SubscriptionTask, attempt.task_row_id)
        latest = await self._latest_attempt_id(attempt.task_row_id)
        if (
            run is None
            or task is None
            or run.state in {RunState.CANCELLED.value, RunState.FAILED.value, RunState.COMPLETED.value}
            or task.cancel_requested
            or latest != attempt_id
        ):
            return False
        if result.disposition == "decision_pending" and not result.accepted:
            return True
        if result.disposition == "candidate_prepared" and result.accepted:
            return True
        if result.disposition == "acceptance_prepared" and result.accepted:
            requested = await self._session.scalar(
                select(RunEvent.id).where(
                    RunEvent.run_id == attempt.run_id,
                    RunEvent.event_type == "run.subscription_validation_requested",
                    RunEvent.payload["binding"]["source_attempt_id"].astext == str(attempt_id),
                ).limit(1)
            )
            return requested is None
        return False

    async def record_settlement_role_violation(
        self, attempt_id: UUID, reason_code: str, *, automatic: bool
    ) -> None:
        attempt = await self._session.get(SubscriptionAttempt, attempt_id)
        if attempt is None:
            raise RecoveryConflict("role rejection source is missing")
        now = datetime.now(UTC)
        self._session.add(
            SubscriptionApplicationDiagnostic(
                attempt_id=attempt_id,
                run_id=attempt.run_id,
                task_id=attempt.task_row_id,
                classification="invalid",
                reason_code=reason_code,
                resolution="rejected" if automatic else "attention",
                failed_applications=1,
                first_failure_at=now,
                last_failure_at=now,
            )
        )
        if not automatic:
            await self._attention_changed(
                attempt.run_id, attempt.task_row_id, attempt_id,
                was_attention=False, is_attention=True, reason_code=reason_code,
            )
        await self._session.flush()

    async def role_violation(self, attempt_id: UUID) -> str | None:
        result = await self._session.get(SubscriptionAttemptResult, attempt_id)
        attempt = await self._session.get(SubscriptionAttempt, attempt_id)
        if result is None or attempt is None or result.disposition != "decision_pending":
            return None
        task = await self._session.get(SubscriptionTask, attempt.task_row_id)
        run = await self._session.get(Run, attempt.run_id)
        if task is None or run is None:
            return None
        try:
            context = result.result_payload["proposal_context"]
            if not isinstance(context, dict):
                raise TypeError
            contract_payload = context["task"]
            if not isinstance(contract_payload, dict):
                raise TypeError
            contract = decode_subscription_record(contract_payload)
            decision_payload = result.result_payload["decision"]
            decision = _stored_decision(decision_payload)
            phase = RunState(
                run.suspended_state
                if run.state == RunState.PAUSED.value and run.suspended_state is not None
                else run.state
            )
            if not isinstance(contract, LogicalTaskContract):
                return "decision_source_invalid"
            if decision_allowed(decision_kind(decision) or "unknown", contract.purpose, phase):
                return None
            return f"{contract.purpose.value}_{decision_kind(decision) or 'unknown'}_forbidden"
        except KeyError, TypeError, ValueError:
            return "decision_source_invalid"

    async def reject_role_violation(
        self, attempt_id: UUID, reason_code: str, *, observed_task_version: int | None = None
    ) -> None:
        attempt = await self._session.get(SubscriptionAttempt, attempt_id)
        if attempt is None:
            raise RecoveryConflict("invalid decision source is missing")
        run = await self._session.get(Run, attempt.run_id, with_for_update=True)
        result = await self._session.get(
            SubscriptionAttemptResult, attempt_id, with_for_update=True
        )
        task = await self._session.get(SubscriptionTask, attempt.task_row_id, with_for_update=True)
        scheduled = await self._session.get(
            SubscriptionScheduledTask, attempt.task_row_id, with_for_update=True
        )
        if run is None or result is None or task is None or scheduled is None:
            raise RecoveryConflict("invalid decision source differs")
        if (
            task.pause_requested
            or task.cancel_requested
            or scheduled.pause_requested
            or scheduled.cancel_requested
            or (observed_task_version is not None and task.version != observed_task_version)
        ):
            return
        if result.disposition == "role_rejected":
            return
        if result.disposition != "decision_pending" or result.application_payload is not None:
            raise RecoveryConflict("invalid decision already applied")
        launches = (
            await self._session.scalars(
                select(SubscriptionClientLaunch).where(
                    SubscriptionClientLaunch.attempt_id == attempt_id
                )
            )
        ).all()
        try:
            proof = SubscriptionLaunchTerminalProof.model_validate(
                result.result_payload.get("launch_proof")
            )
            stopped = launches_confirmed(
                launches,
                proof,
                require_decision=True,
                worker_identity=attempt.lease_owner,
            )
        except TypeError, ValueError:
            stopped = False
        effects = await self._session.scalar(
            select(SubscriptionScheduledEffect.id)
            .where(
                SubscriptionScheduledEffect.run_id == attempt.run_id,
                SubscriptionScheduledEffect.task_id == attempt.task_row_id,
                SubscriptionScheduledEffect.state.in_(("admitted", "reconciling")),
            )
            .limit(1)
        )
        if not stopped or effects is not None:
            await self.record_failure(
                attempt_id, classification="unknown_effect", reason_code="effect_uncertain"
            )
            return
        receipt = {
            "schema_version": 1,
            "kind": "role_rejection",
            "reason_code": reason_code,
            "source_result_digest": result.result_digest,
        }
        await self.record_failure(attempt_id, classification="invalid", reason_code=reason_code)
        result.application_payload = receipt
        result.application_digest = canonical_digest(receipt)
        result.disposition = "role_rejected"
        result.accepted = False
        attempt.status = "terminal"
        task.state = "blocked"
        task.version += 1
        await self._session.flush()

    async def signing_key(self) -> bytes:
        row = await self._session.get(SubscriptionRecoverySigningKey, 1)
        if row is None:
            raise RecoveryConflict("recovery signing key unavailable")
        return bytes.fromhex(row.key_hex)

    async def receipt_for(
        self, run_id: UUID, actor_id: UUID, idempotency_key: str
    ) -> RecoveryReceiptRecord | None:
        row = await self._session.scalar(
            select(SubscriptionRecoveryReceipt).where(
                SubscriptionRecoveryReceipt.run_id == run_id,
                SubscriptionRecoveryReceipt.actor_id == str(actor_id),
                SubscriptionRecoveryReceipt.idempotency_key == idempotency_key,
            )
        )
        return None if row is None else RecoveryReceiptRecord(_receipt(row), row.request_digest)

    async def receipt_history(
        self, run_id: UUID, task_id: UUID, attempt_id: UUID, *, offset: int, limit: int
    ) -> tuple[list[RecoveryReceipt], bool]:
        rows = list((await self._session.scalars(
            select(SubscriptionRecoveryReceipt)
            .where(
                SubscriptionRecoveryReceipt.run_id == run_id,
                SubscriptionRecoveryReceipt.task_id == task_id,
                SubscriptionRecoveryReceipt.attempt_id == attempt_id,
            )
            .order_by(
                SubscriptionRecoveryReceipt.created_at.desc(),
                SubscriptionRecoveryReceipt.id.desc(),
            )
            .offset(offset)
            .limit(limit + 1)
        )).all())
        return [_receipt(row) for row in rows[:limit]], len(rows) > limit

    async def preview(
        self,
        run_id: UUID,
        task_id: UUID,
        attempt_id: UUID,
        action: RecoveryAction,
        *,
        locked: bool = False,
        lock_run: bool = True,
    ) -> RecoverySnapshot:
        if locked and not lock_run:
            raise ValueError("locked preview requires run write lock")

        def refused(reason: str, binding: str = "") -> RecoverySnapshot:
            return RecoverySnapshot(
                binding,
                False,
                reason,
                _EXPLANATIONS[reason],
                (),
                ("Original provider result and usage remain retained.",),
                0,
                0,
            )

        # The run lock also serializes admission of an accepted pause/cancel
        # command with this preview and the locked apply revalidation.
        run = await self._session.get(Run, run_id, with_for_update=lock_run)
        task = await self._session.get(SubscriptionTask, task_id, with_for_update=locked)
        attempt = await self._session.get(SubscriptionAttempt, attempt_id, with_for_update=locked)
        result = await self._session.get(
            SubscriptionAttemptResult, attempt_id, with_for_update=locked
        )
        scheduled = await self._session.get(
            SubscriptionScheduledTask, task_id, with_for_update=locked
        )
        scheduler_run = await self._session.get(
            SubscriptionSchedulerRun, run_id, with_for_update=locked
        )
        if (
            run is None
            or task is None
            or attempt is None
            or result is None
            or scheduled is None
            or scheduler_run is None
            or task.run_id != run_id
            or attempt.run_id != run_id
            or attempt.task_row_id != task_id
            or scheduled.run_id != run_id
        ):
            return refused("source_missing")
        latest_attempt_id = await self._latest_attempt_id(task_id)
        if (
            latest_attempt_id != attempt_id
            or scheduled.lease_generation != attempt.lease_generation
        ):
            return refused("source_changed")
        try:
            contract = decode_subscription_record(task.payload)
            context = result.result_payload["proposal_context"]
            if not isinstance(context, dict) or not isinstance(context.get("task"), dict):
                raise TypeError
            original = decode_subscription_record(context["task"])
            decision_payload = result.result_payload["decision"]
            decision = _stored_decision(decision_payload)
            if not isinstance(contract, LogicalTaskContract) or not isinstance(
                original, LogicalTaskContract
            ):
                raise TypeError
            if canonical_digest(result.result_payload) != result.result_digest:
                raise ValueError
        except KeyError, TypeError, ValueError:
            return refused("source_changed")
        approved_source = await self._approved_source(run_id, task_id)
        gate, approval, prepared_event = approved_source if approved_source is not None else (None, None, None)
        project = await self._session.get(Project, run.project_id)
        diagnostic = await self._session.get(SubscriptionApplicationDiagnostic, attempt_id)
        binding = canonical_digest(
            {
                "run_id": str(run_id),
                "run_version": run.version,
                "task_id": str(task_id),
                "task_version": task.version,
                "attempt_id": str(attempt_id),
                "attempt_status": attempt.status,
                "result_digest": result.result_digest,
                "disposition": result.disposition,
                "contract_digest": canonical_digest(task.payload),
                "candidate_epoch": scheduler_run.candidate_epoch,
                "candidate_state": scheduler_run.candidate_state,
                "worktree_id": scheduled.worktree_id,
                "scheduled_state": scheduled.state,
                "scheduled_generation": scheduled.lease_generation,
                "policy_version": run.policy_version,
                "approval_id": str(approval.id) if approval else None,
                "plan_attempt_id": str(gate.attempt_id) if gate else None,
                "plan_digest": gate.plan_digest if gate else None,
                "prepared_run_version": prepared_event.run_version if prepared_event else None,
                "approval_invalidated": approval.invalidated_at.isoformat()
                if approval and approval.invalidated_at
                else None,
                "diagnostic_revision": diagnostic.updated_at.isoformat() if diagnostic else None,
                "action": action.value,
            }
        )
        if run.state not in {
            RunState.PLANNING.value,
            RunState.IMPLEMENTING.value,
            RunState.REMEDIATING.value,
        } or run.pending_gate is not None:
            return refused("run_controlled", binding)
        if await PostgresCommandRepository(session=self._session).has_pending_current_control_stop(
            run_id=run_id, expected_run_version=run.version
        ):
            return refused("run_controlled", binding)
        if (
            task.pause_requested
            or task.cancel_requested
            or scheduled.pause_requested
            or scheduled.cancel_requested
        ):
            return refused("task_controlled", binding)
        staged_epoch = (
            result.application_payload.get("candidate_epoch")
            if isinstance(result.application_payload, dict)
            and canonical_digest(result.application_payload) == result.application_digest
            else None
        )
        prepared_candidate_current = (
            action is RecoveryAction.RETRY_APPLICATION
            and result.disposition == "candidate_prepared"
            and scheduler_run.candidate_state == "closed"
            and attempt.candidate_epoch is not None
            and staged_epoch == scheduler_run.candidate_epoch == attempt.candidate_epoch + 1
        )
        prepared_acceptance_current = (
            action is RecoveryAction.RETRY_APPLICATION
            and result.disposition == "acceptance_prepared"
            and scheduler_run.candidate_state == "closed"
            and staged_epoch == scheduler_run.candidate_epoch == attempt.candidate_epoch
        )
        if not (
            prepared_candidate_current
            or prepared_acceptance_current
            or (
                scheduler_run.candidate_state == "open"
                and scheduler_run.candidate_epoch == attempt.candidate_epoch
            )
        ):
            return refused("candidate_changed", binding)
        if project is None or project.current_policy_version != run.policy_version:
            return refused("policy_changed", binding)
        worker = await self._session.scalar(
            select(SubscriptionRecoveryWorker.worker_id)
            .where(
                SubscriptionRecoveryWorker.contract_version >= 1,
                SubscriptionRecoveryWorker.observed_at
                >= datetime.now(UTC) - timedelta(seconds=RECOVERY_WORKER_FRESHNESS_SECONDS),
            )
            .limit(1)
        )
        if worker is None:
            return refused("worker_unavailable", binding)
        effects = await self._session.scalar(
            select(SubscriptionScheduledEffect.id)
            .where(
                SubscriptionScheduledEffect.run_id == run_id,
                SubscriptionScheduledEffect.task_id == task_id,
                SubscriptionScheduledEffect.state.in_(("admitted", "reconciling")),
            )
            .limit(1)
        )
        launches = (
            await self._session.scalars(
                select(SubscriptionClientLaunch).where(
                    SubscriptionClientLaunch.attempt_id == attempt_id
                )
            )
        ).all()
        try:
            proof = SubscriptionLaunchTerminalProof.model_validate(
                result.result_payload.get("launch_proof")
            )
            stopped = launches_confirmed(
                launches,
                proof,
                require_decision=decision is not None,
                worker_identity=attempt.lease_owner,
            )
        except TypeError, ValueError:
            stopped = False
        if effects is not None or not stopped or scheduled.state == "leased":
            return refused("effect_uncertain", binding)
        stale_contract = (
            is_approved_plan_primary_contract(contract)
            and gate is not None
            and approval is not None
        )
        contract_only = (
            stale_contract
            and gate is not None
            and (gate.attempt_id == attempt_id or result.disposition != "plan_approval")
            and task.state == "blocked"
            and scheduled.state == "blocked"
            and diagnostic is not None
            and diagnostic.reason_code == "approved_plan_contract_stale"
            and diagnostic.resolution == "attention"
            and (
                (
                    result.disposition in {"plan_approval", "repair_queued", "quota_deferred"}
                    and result.accepted
                )
                or await self._queued_continuation_proven(run_id, task, attempt, result)
            )
        )
        stale_plan = (
            stale_contract
            and result.disposition in {"decision_pending", "role_rejected"}
            and (
                (
                    decision is not None
                    and not decision_allowed(
                        decision_kind(decision) or "unknown", contract.purpose,
                        RunState.IMPLEMENTING,
                    )
                )
                or (decision is None and _forbidden_role_rejection(result, contract))
            )
        )
        if action is RecoveryAction.RETRY_APPLICATION:
            if (
                result.disposition not in {"decision_pending", "candidate_prepared", "acceptance_prepared"}
                or diagnostic is None
                or diagnostic.classification not in {"temporary", "prerequisite", "unsupported"}
                or diagnostic.resolution not in {"scheduled", "waiting", "attention"}
                or not await self._application_pending(attempt_id)
                or not decision_allowed(
                    decision_kind(decision) or "unknown", contract.purpose, RunState(run.state)
                )
            ):
                return refused("application_not_retryable", binding)
            return RecoverySnapshot(
                binding,
                True,
                "eligible",
                _EXPLANATIONS["eligible"],
                ("Retry the exact saved decision application without a provider call.",),
                ("Original provider result and settled usage remain retained.",),
                0,
                0,
            )
        if action is RecoveryAction.REPAIR_APPROVED_PLAN_CONTRACT:
            if not (stale_plan or contract_only):
                return refused("not_stale_plan", binding)
            if (
                gate is None
                or approval is None
                or approval.invalidated_at is not None
                or approval.policy_version != run.policy_version
            ):
                return refused("approval_missing", binding)
        elif action is RecoveryAction.REJECT_AND_RETRY_STEP:
            if stale_plan:
                return refused("invalid_result", binding)
            expired_unapplied_handoff = (
                result.disposition == "decision_pending"
                and not result.accepted
                and isinstance(decision, TaskHandoff)
                and decision.status is HandoffStatus.COMPLETED
                and diagnostic is not None
                and diagnostic.classification == "prerequisite"
                and diagnostic.reason_code == "handoff_observation_changed"
                and diagnostic.resolution == "attention"
                and result.application_payload is None
                and result.application_digest is None
                and await self._session.scalar(
                    select(SubscriptionDecisionRecord.id).where(
                        SubscriptionDecisionRecord.attempt_id == attempt_id
                    ).limit(1)
                ) is None
            )
            ordinary_rejection = (
                result.disposition in {"decision_pending", "role_rejected"}
                and not result.accepted
                and diagnostic is not None
                and diagnostic.classification in {"invalid", "unsupported"}
                and diagnostic.resolution == "attention"
            )
            if not (expired_unapplied_handoff or ordinary_rejection):
                return refused("unsupported_source", binding)
            if task.parent_task_id is not None:
                parent = await self._session.get(SubscriptionTask, task.parent_task_id)
                parent_schedule = await self._session.get(
                    SubscriptionScheduledTask, task.parent_task_id
                )
                parent_latest = await self._latest_attempt_id(task.parent_task_id)
                parent_result = (
                    await self._session.get(SubscriptionAttemptResult, parent_latest)
                    if parent_latest is not None else None
                )
                parent_attempt = (
                    await self._session.get(SubscriptionAttempt, parent_latest)
                    if parent_latest is not None else None
                )
                parent_decision = (
                    await self._verified_decision(parent, parent_attempt, parent_result)
                    if parent is not None and parent_attempt is not None and parent_result is not None
                    else None
                )
                wait_receipt = parent_result.application_payload if parent_result else None
                child_attempt_ids = (
                    wait_receipt.get("child_attempt_ids")
                    if isinstance(wait_receipt, dict) else None
                )
                # A current selected child is safe only while its handoff is
                # unapplied. Earlier same-child attempts are ordered by their
                # durable numbers; terminal rejections still need an older wait.
                wait_before_source = (
                    parent_result is not None
                    and parent_result.accepted
                    and parent_result.disposition == "waiting"
                    and isinstance(parent_decision, WaitDecision)
                    and isinstance(wait_receipt, dict)
                    and canonical_digest(wait_receipt) == parent_result.application_digest
                    and wait_receipt.get("kind") == "wait_child_attempt_snapshot"
                    and wait_receipt.get("source_result_digest") == parent_result.result_digest
                    and isinstance(child_attempt_ids, dict)
                    and str(task_id) in child_attempt_ids
                    and await self._wait_snapshot_allows(
                        child_attempt_ids[str(task_id)], attempt,
                        matching_current=(
                            expired_unapplied_handoff
                            and task_id in parent_decision.waiting_on_task_ids
                        ),
                    )
                )
                delegated_pending_child = (
                    expired_unapplied_handoff
                    and parent_result is not None
                    and parent_result.disposition == "delegated"
                    and isinstance(parent_decision, DelegateDecision)
                    and any(child.task_id == task_id for child in parent_decision.child_tasks)
                )
                dependent_progress = await self._session.scalar(
                    select(SubscriptionAttempt.id)
                    .join(
                        SubscriptionScheduledTask,
                        SubscriptionScheduledTask.task_id == SubscriptionAttempt.task_row_id,
                    )
                    .where(
                        SubscriptionScheduledTask.run_id == run_id,
                        SubscriptionScheduledTask.dependency_task_ids.any(literal(task_id)),
                    )
                    .limit(1)
                )
                if (
                    parent is None
                    or parent_schedule is None
                    or parent.state != "blocked"
                    or parent_schedule.state != "blocked"
                    or not (wait_before_source or delegated_pending_child)
                    or dependent_progress is not None
                ):
                    return refused("source_changed", binding)
        else:
            return refused("unsupported_source", binding)
        if contract_only:
            return RecoverySnapshot(
                binding,
                True,
                "eligible",
                _EXPLANATIONS["eligible"],
                ("Replace the queued stale planning objective with the approved implementation contract.",),
                ("Original prepared contract, planning result, approval, and usage remain retained.",),
                0,
                0,
            )
        prior_rejection = _is_prior_role_rejection(result)
        if result.application_payload is not None and not prior_rejection:
            return refused("unsupported_source", binding)
        if scheduled.state not in {"reconciling", "terminal", "blocked"}:
            return refused("unsupported_source", binding)
        if (
            scheduled.repairs >= scheduled.max_repairs
            or not await PostgresSubscriptionBudgetRepository(self._session).can_debit_repair(
                run_id, task_id, attempt_id, locked=lock_run
            )
        ):
            return refused("budget_exhausted", binding)
        change = (
            "Replace the stale planning objective with the approved implementation contract."
            if action is RecoveryAction.REPAIR_APPROVED_PLAN_CONTRACT
            else "Reject this saved decision and queue one fresh attempt under the current contract."
        )
        return RecoverySnapshot(
            binding,
            True,
            "eligible",
            _EXPLANATIONS["eligible"],
            (change, "Record the rejected result and reserve one repair attempt."),
            ("Original plan, provider result, tool evidence, and usage remain retained.",),
            1,
            1,
        )

    async def apply(
        self,
        run_id: UUID,
        task_id: UUID,
        attempt_id: UUID,
        action: RecoveryAction,
        *,
        actor_id: UUID,
        idempotency_key: str,
        request_digest: str,
        binding: str,
        expires_at: datetime,
        reason: str,
    ) -> RecoveryReceipt:
        await self._session.get(Run, run_id, with_for_update=True)
        prior = await self.receipt_for(run_id, actor_id, idempotency_key)
        if prior is not None:
            if prior.request_digest != request_digest:
                raise RecoveryConflict("recovery idempotency key conflicts")
            return prior.receipt
        if expires_at.tzinfo is None or expires_at <= datetime.now(UTC):
            raise RecoveryConflict("recovery preview expired")
        snapshot = await self.preview(run_id, task_id, attempt_id, action, locked=True)
        if not snapshot.eligible or snapshot.binding != binding:
            raise RecoveryConflict("recovery source differs from preview")
        result = await self._session.get(
            SubscriptionAttemptResult, attempt_id, with_for_update=True
        )
        task = await self._session.get(SubscriptionTask, task_id, with_for_update=True)
        scheduled = await self._session.get(
            SubscriptionScheduledTask, task_id, with_for_update=True
        )
        attempt = await self._session.get(SubscriptionAttempt, attempt_id, with_for_update=True)
        assert (
            result is not None
            and task is not None
            and scheduled is not None
            and attempt is not None
        )
        if action is RecoveryAction.RETRY_APPLICATION:
            diagnostic = await self._session.get(
                SubscriptionApplicationDiagnostic, attempt_id, with_for_update=True
            )
            assert diagnostic is not None
            await self._resolve_diagnostic(
                diagnostic, resolution="scheduled", next_retry_at=datetime.now(UTC)
            )
        else:
            contract_only = (
                action is RecoveryAction.REPAIR_APPROVED_PLAN_CONTRACT
                and scheduled.state == "blocked"
                and snapshot.repair_units == 0
            )
            if contract_only:
                await self._repair_contract(run_id, task, result)
                if scheduled.state not in {"queued", "blocked"}:
                    raise RecoveryConflict("stale contract schedule changed")
                scheduled.state = "queued"
                task.state = "queued"
                task.version += 1
                diagnostic = await self._session.get(
                    SubscriptionApplicationDiagnostic, attempt_id, with_for_update=True
                )
                if diagnostic is not None:
                    await self._resolve_diagnostic(
                        diagnostic, resolution="rejected", next_retry_at=None
                    )
            else:
                await self._reject_and_queue(
                    run_id, task_id, attempt_id, action, actor_id, result, task, scheduled, attempt
                )
        row = SubscriptionRecoveryReceipt(
            run_id=run_id,
            task_id=task_id,
            attempt_id=attempt_id,
            actor_id=str(actor_id),
            idempotency_key=idempotency_key,
            request_digest=request_digest,
            action=action.value,
            reason_code="applied",
            status="applied",
            reason=str(redact_value(reason))[:1000],
        )
        self._session.add(row)
        await self._session.flush()
        return _receipt(row)

    async def _reject_and_queue(
        self,
        run_id: UUID,
        task_id: UUID,
        attempt_id: UUID,
        action: RecoveryAction,
        actor_id: UUID,
        result: SubscriptionAttemptResult,
        task: SubscriptionTask,
        scheduled: SubscriptionScheduledTask,
        attempt: SubscriptionAttempt,
    ) -> None:
        prior_rejection = _is_prior_role_rejection(result)
        if result.application_payload is not None and not prior_rejection:
            raise RecoveryConflict("source already has an application receipt")
        budget = PostgresSubscriptionBudgetRepository(self._session)
        if not await budget.try_debit_repair(run_id, task_id, attempt_id):
            raise RecoveryConflict("recovery budget changed")
        if action is RecoveryAction.REPAIR_APPROVED_PLAN_CONTRACT:
            await self._repair_contract(run_id, task, result)
        receipt_payload = {
            "schema_version": 1,
            "kind": "operator_rejection",
            "action": action.value,
            "actor_id": str(actor_id),
            "source_result_digest": result.result_digest,
        }
        if not prior_rejection:
            result.application_payload = receipt_payload
            result.application_digest = canonical_digest(receipt_payload)
        result.disposition = "recovery_rejected"
        result.accepted = False
        attempt.status = "terminal"
        if scheduled.state == "reconciling":
            scheduled.repairs += 1
            await PostgresSchedulingRepository(self._session).reconcile_expired(
                run_id, task_id, retry=True
            )
        elif scheduled.state in {"terminal", "blocked"}:
            scheduled.state = "queued"
            scheduled.lease_owner = None
            scheduled.lease_expires_at = None
            scheduled.repairs += 1
        else:
            raise RecoveryConflict("recovery schedule changed")
        task.state = "queued"
        task.version += 1
        diagnostic = await self._session.get(
            SubscriptionApplicationDiagnostic, attempt_id, with_for_update=True
        )
        if diagnostic is not None:
            await self._resolve_diagnostic(
                diagnostic, resolution="rejected", next_retry_at=None
            )

    async def _repair_contract(
        self, run_id: UUID, task: SubscriptionTask, result: SubscriptionAttemptResult
    ) -> None:
        approved_source = await self._approved_source(run_id, task.id)
        if approved_source is None:
            raise RecoveryConflict("approved plan is missing")
        gate, approval, _ = approved_source
        original = decode_subscription_record(task.payload)
        assert isinstance(original, LogicalTaskContract)
        decision = gate.snapshot.get("decision")
        if not isinstance(decision, dict) or decision.get("type") != "PlanOutput":
            raise RecoveryConflict("approved plan differs")
        plan = decode_plan_output(decision["value"])
        prepared = approved_implementation_contract(
            original,
            plan,
            gate.attempt_id,
            gate.plan_digest,
            approval.id,
        )
        payload = encode_subscription_record(prepared)
        previous = await self._session.scalar(
            select(func.max(SubscriptionContractRevision.revision)).where(
                SubscriptionContractRevision.task_id == task.id
            )
        )
        self._session.add(
            SubscriptionContractRevision(
                run_id=run_id,
                task_id=task.id,
                revision=(previous or 0) + 1,
                source_attempt_id=gate.attempt_id,
                approval_id=approval.id,
                plan_digest=gate.plan_digest,
                original_contract_digest=canonical_digest(task.payload),
                original_contract_payload=task.payload,
                contract_digest=canonical_digest(payload),
                contract_payload=payload,
            )
        )
        task.payload = payload
        scheduled = await self._session.get(
            SubscriptionScheduledTask, task.id, with_for_update=True
        )
        assert scheduled is not None
        scheduled.owned_paths = [policy_path_key(path) for path in prepared.owned_paths]
        await self._session.flush()


def _receipt(row: SubscriptionRecoveryReceipt) -> RecoveryReceipt:
    return RecoveryReceipt(
        receipt_id=row.id,
        run_id=row.run_id,
        task_id=row.task_id,
        attempt_id=row.attempt_id,
        action=RecoveryAction(row.action),
        status=row.status,
        observed_at=row.created_at,
        reason_code=row.reason_code,
    )
