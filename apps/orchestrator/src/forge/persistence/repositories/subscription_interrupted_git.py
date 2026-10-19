"""Explicit owner settlement of one unpublished Git preparation with a saved selection."""

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import JSON, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from forge.application.adapters.git_commit import _prepare_request, _primary_paths
from forge.application.ports.worktrees import ManagedWorktree
from forge.application.services.tool_recovery import _recovery_context
from forge.application.services.tools import _git_prepare_payload
from forge.domain.event import RunEvent
from forge.domain.operation import canonical_digest
from forge.domain.resource import WorktreeIdentity
from forge.domain.subscription import (
    CANDIDATE_READ_TOOLS,
    ReviewSelection,
    SpecialistPurpose,
    ToolCallBinding,
    decode_subscription_record,
)
from forge.domain.subscription_recovery import (
    RECOVERY_WORKER_FRESHNESS_SECONDS,
    RecoveryAction,
    RecoveryReceipt,
    RecoverySnapshot,
)
from forge.domain.tool import ToolName
from forge.observability.redaction import redact_value
from forge.persistence.models import Run
from forge.persistence.models.execution import (
    AgentExecution,
    ArtifactLineage,
    OperationIntent,
    Step,
    ToolCall,
)
from forge.persistence.models.execution import RunEvent as StoredRunEvent
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
    SubscriptionOperationBinding,
    SubscriptionTask,
)
from forge.persistence.models.subscription_recovery import (
    SubscriptionApplicationDiagnostic,
    SubscriptionRecoveryReceipt,
    SubscriptionRecoveryWorker,
)
from forge.persistence.models.subscription_results import SubscriptionAttemptResult
from forge.persistence.models.subscription_usage import SubscriptionAttemptConsumption
from forge.persistence.models.task_usage import TaskUsageCheckpoint
from forge.persistence.queries.subscription_usage_proofs import historical_source
from forge.persistence.repositories.commands import PostgresCommandRepository
from forge.persistence.repositories.events import PostgresEventRepository
from forge.persistence.repositories.operations import PostgresOperationRepository
from forge.persistence.repositories.runs import PostgresRunRepository
from forge.persistence.repositories.subscription_interrupted_check import _values
from forge.persistence.repositories.tool_calls import PostgresToolCallRepository

ACTION = RecoveryAction.ACKNOWLEDGE_INTERRUPTED_GIT_PREPARATION
WARNING = (
    "The owner acknowledges that the prior worker and Git preparation processes have stopped. "
    "Forge cannot prove the preparation outcome; it remains unknown. No publication was admitted. "
    "Existing work, the saved review selection and recorded usage are retained. "
    "Forge will verify the current candidate before applying that saved selection."
)


@dataclass
class _Source:
    run: Run
    task: SubscriptionTask
    attempt: SubscriptionAttempt
    result: SubscriptionAttemptResult
    scheduled: SubscriptionScheduledTask
    operation: OperationIntent
    call: ToolCall
    effect: SubscriptionScheduledEffect
    callback: SubscriptionOperationBinding
    binding: str


def _refused(code: str, message: str) -> RecoverySnapshot:
    return RecoverySnapshot("", False, code, message, (), (), 0, 0)


class InterruptedGitPreparationRecovery:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def preview(
        self,
        run_id: UUID,
        task_id: UUID,
        attempt_id: UUID,
        *,
        locked: bool = False,
        lock_run: bool = True,
    ) -> RecoverySnapshot:
        source = await self._source(run_id, task_id, attempt_id, locked=locked, lock_run=lock_run)
        if isinstance(source, RecoverySnapshot):
            return source
        return RecoverySnapshot(
            source.binding,
            True,
            "owner_acknowledgment_required",
            WARNING,
            (
                "Release this unpublished preparation as owner-acknowledged unknown; never replay it.",
                "Retry the exact saved review selection through normal candidate verification.",
            ),
            (
                "Original provider result, known and unknown usage, worktree and prior receipts remain retained.",
            ),
            0,
            0,
        )

    async def _source(
        self, run_id: UUID, task_id: UUID, attempt_id: UUID, *, locked: bool, lock_run: bool
    ) -> _Source | RecoverySnapshot:
        run = await self.session.get(Run, run_id, with_for_update=lock_run)
        task = await self.session.get(SubscriptionTask, task_id, with_for_update=locked)
        attempt = await self.session.get(SubscriptionAttempt, attempt_id, with_for_update=locked)
        result = await self.session.get(
            SubscriptionAttemptResult, attempt_id, with_for_update=locked
        )
        scheduled = await self.session.get(
            SubscriptionScheduledTask, task_id, with_for_update=locked
        )
        scheduler = await self.session.get(SubscriptionSchedulerRun, run_id, with_for_update=locked)
        if (
            run is None
            or task is None
            or attempt is None
            or result is None
            or scheduled is None
            or scheduler is None
            or (
                task.run_id != run_id
                or attempt.run_id != run_id
                or attempt.task_row_id != task_id
                or scheduled.run_id != run_id
            )
        ):
            return _refused("source_missing", "The saved attempt is unavailable.")
        latest = await self.session.scalar(
            select(SubscriptionAttempt.id)
            .where(SubscriptionAttempt.task_row_id == task_id)
            .order_by(SubscriptionAttempt.attempt_number.desc())
            .limit(1)
        )
        if (
            latest != attempt_id
            or result.disposition != "fenced"
            or result.accepted
            or result.application_payload is not None
            or result.application_digest is not None
        ):
            return _refused(
                "source_changed",
                "The latest saved result is no longer an unapplied fenced selection.",
            )
        intervention = (
            run.state == "AWAITING_HUMAN_INTERVENTION"
            and run.suspension_kind == "INTERVENTION"
            and run.suspended_state in {"IMPLEMENTING", "REMEDIATING"}
        )
        if (
            (run.state not in {"IMPLEMENTING", "REMEDIATING"} and not intervention)
            or run.pending_gate is not None
            or any(
                (
                    task.pause_requested,
                    task.cancel_requested,
                    scheduled.pause_requested,
                    scheduled.cancel_requested,
                )
            )
            or await PostgresCommandRepository(
                session=self.session
            ).has_pending_current_control_stop(run_id=run_id, expected_run_version=run.version)
            or await self.session.scalar(
                select(TaskUsageCheckpoint.id)
                .where(
                    TaskUsageCheckpoint.subject_kind == "run",
                    TaskUsageCheckpoint.subject_id == run_id,
                    TaskUsageCheckpoint.state.in_(("checkpoint_requested", "pausing", "paused")),
                )
                .limit(1)
            )
            is not None
        ):
            return _refused(
                "run_controlled",
                "A pause, cancellation, checkpoint or other run control takes precedence.",
            )
        if (
            intervention
            and await self.session.scalar(
                select(StoredRunEvent.id)
                .where(
                    StoredRunEvent.run_id == run_id,
                    StoredRunEvent.run_version == run.version,
                    StoredRunEvent.event_type == "run.recovery_intervention",
                )
                .limit(1)
            )
            is None
        ):
            return _refused("run_controlled", "This is not the recorded startup interruption.")
        if (
            attempt.status != "reconciling"
            or task.state != "reconciling"
            or scheduled.state != "reconciling"
            or task.parent_task_id is not None
            or (
                scheduled.lease_owner != attempt.lease_owner
                or scheduled.lease_generation != attempt.lease_generation
                or attempt.task_version is None
                or task.version != attempt.task_version + 1
                or canonical_digest(task.payload) != attempt.task_digest
                or not scheduler.admitted
                or scheduler.candidate_state != "open"
                or scheduler.candidate_epoch != attempt.candidate_epoch
            )
        ):
            return _refused("source_changed", "Task ownership or candidate changed.")
        now = datetime.now(UTC)
        worker = await self.session.get(SubscriptionRecoveryWorker, attempt.lease_owner)
        if (
            scheduled.lease_expires_at is None
            or scheduled.lease_expires_at > now
            or (
                worker is not None
                and worker.observed_at >= now - timedelta(seconds=RECOVERY_WORKER_FRESHNESS_SECONDS)
            )
        ):
            return _refused(
                "ownership_active", "The previous worker is active or its ownership is uncertain."
            )
        launches = list(
            (
                await self.session.scalars(
                    select(SubscriptionClientLaunch).where(
                        SubscriptionClientLaunch.attempt_id == attempt_id
                    )
                )
            ).all()
        )
        usage = await self.session.get(SubscriptionAttemptConsumption, attempt_id)
        envelope = await self.session.get(SubscriptionEnvelope, run_id)
        source = historical_source(
            attempt,
            task,
            result,
            usage,
            envelope,
            launches[0] if len(launches) == 1 else None,
            len(launches),
        )
        if (
            source is None
            or source.failed
            or not isinstance(source.decision, ReviewSelection)
            or source.contract.purpose is not SpecialistPurpose.PRIMARY
            or source.envelope.safety_policy_version != run.policy_version
        ):
            return _refused(
                "source_unproved",
                "The saved selection, usage and stopped provider proof must match.",
            )
        effects = list(
            (
                await self.session.scalars(
                    select(SubscriptionScheduledEffect)
                    .where(
                        SubscriptionScheduledEffect.run_id == run_id,
                        SubscriptionScheduledEffect.state.in_(("admitted", "reconciling")),
                    )
                    .with_for_update()
                    if locked
                    else select(SubscriptionScheduledEffect).where(
                        SubscriptionScheduledEffect.run_id == run_id,
                        SubscriptionScheduledEffect.state.in_(("admitted", "reconciling")),
                    )
                )
            ).all()
        )
        if len(effects) != 1:
            return _refused(
                "effect_uncertain", "Recovery requires exactly one unpublished Git preparation."
            )
        effect = effects[0]
        operation = await self.session.get(OperationIntent, effect.id, with_for_update=locked)
        call = await self.session.get(ToolCall, effect.id, with_for_update=locked)
        callbacks = list(
            (
                await self.session.scalars(
                    select(SubscriptionOperationBinding).where(
                        SubscriptionOperationBinding.durable_operation_id == effect.id
                    )
                )
            ).all()
        )
        if operation is None or call is None or len(callbacks) != 1:
            return _refused("source_missing", "Git preparation authority is incomplete.")
        callback = callbacks[0]
        if operation.execution_owner is not None and (
            operation.execution_lease_expires_at is None
            or operation.execution_lease_expires_at > now
        ):
            return _refused(
                "ownership_active", "The Git preparation still has active or uncertain ownership."
            )
        try:
            record = await PostgresToolCallRepository(self.session).get(call.id)
            intent = await PostgresOperationRepository(session=self.session).get(operation.id)
            context = _recovery_context(record)
            worktree = ManagedWorktree(
                identity=WorktreeIdentity.for_run(
                    run.project_id, run_id, run.branch_name or "", run.database_name is not None
                ),
                path=Path(run.worktree_path or ""),
                base_sha=run.base_sha or "",
            )
            request = _prepare_request(intent, worktree)
            message = request["message"]
            binding = decode_subscription_record(callback.payload)
            if (
                not isinstance(message, str)
                or not isinstance(binding, ToolCallBinding)
                or (
                    call.run_id != run_id
                    or call.subscription_task_id != task_id
                    or call.subscription_attempt_id != attempt_id
                    or call.subscription_purpose != "primary"
                    or not call.authorized
                    or call.status != "RUNNING"
                    or call.completed_at is not None
                    or call.tool_name != ToolName.GIT_COMMIT.value
                    or operation.run_id != run_id
                    or operation.status not in {"PENDING", "NEEDS_RECONCILIATION"}
                    or operation.outcome_payload is not None
                    or operation.completed_at is not None
                    or operation.idempotency_key != f"git.commit:{call.id}:prepare"
                    or record.operation_intent_id != call.id
                    or record.correlation_id != call.id
                    or record.resource_id != scheduled.worktree_id
                    or record.policy_version != run.policy_version
                    or record.invocation_schema_version != 1
                    or record.arguments_schema_version != 1
                    or _primary_paths(request) != source.contract.owned_paths
                    or request
                    != _git_prepare_payload(
                        context,
                        worktree,
                        message,
                        canonical_digest({"message": message}),
                        owned_paths=source.contract.owned_paths,
                    )
                    or record.normalized_arguments
                    != {"message_digest": hashlib.sha256(message.encode()).hexdigest()}
                    or record.request_digest != request["request_digest"]
                    or record.artifact_digests
                    or effect.task_id != task_id
                    or effect.lease_owner != attempt.lease_owner
                    or effect.lease_generation != attempt.lease_generation
                    or effect.candidate_epoch != attempt.candidate_epoch
                    or not effect.whole_worktree_exclusive
                    or callback.attempt_id != attempt_id
                    or callback.receipt_payload is not None
                    or binding.attempt_id != attempt_id
                    or binding.durable_operation_id != call.id
                    or binding.provider_call_key != callback.provider_call_key
                    or binding.tool_name is not ToolName.GIT_COMMIT
                    or binding.arguments_digest != record.request_digest
                )
            ):
                raise ValueError
        except RuntimeError, TypeError, ValueError:
            return _refused("source_changed", "Git preparation request or lineage differs.")
        pending_reads = list(
            (
                await self.session.scalars(
                    select(SubscriptionOperationBinding)
                    .where(
                        SubscriptionOperationBinding.attempt_id == attempt_id,
                        SubscriptionOperationBinding.durable_operation_id != effect.id,
                        or_(
                            SubscriptionOperationBinding.receipt_payload.is_(None),
                            SubscriptionOperationBinding.receipt_payload == JSON.NULL,
                        ),
                    )
                    .order_by(SubscriptionOperationBinding.id)
                )
            ).all()
        )
        for pending in pending_reads:
            try:
                read = decode_subscription_record(pending.payload)
            except TypeError, ValueError:
                read = None
            if (
                not isinstance(read, ToolCallBinding)
                or read.tool_name not in CANDIDATE_READ_TOOLS
                or read.attempt_id != attempt_id
                or read.provider_call_key != pending.provider_call_key
                or read.durable_operation_id != pending.durable_operation_id
            ):
                return _refused(
                    "other_work_unresolved", "Another callback must be reconciled first."
                )
            for model in (ToolCall, OperationIntent, SubscriptionScheduledEffect):
                if await self.session.get(model, pending.durable_operation_id) is not None:
                    return _refused(
                        "other_work_unresolved", "Another callback must be reconciled first."
                    )
        # A stopped provider can leave a read reservation before tool admission.
        # Preserve its missing result; it grants no writer and is never replayed.
        other = (
            select(OperationIntent.id).where(
                OperationIntent.run_id == run_id,
                OperationIntent.id != effect.id,
                or_(
                    OperationIntent.status.in_(("PENDING", "NEEDS_RECONCILIATION")),
                    OperationIntent.idempotency_key == f"git.commit:{effect.id}:publish",
                    OperationIntent.request_payload["preparation_intent_id"].astext
                    == str(effect.id),
                ),
            ),
            select(ToolCall.id).where(
                ToolCall.run_id == run_id,
                ToolCall.id != effect.id,
                ToolCall.status.in_(("PENDING", "RUNNING")),
            ),
            select(ArtifactLineage.id).where(
                ArtifactLineage.run_id == run_id, ArtifactLineage.producer_id == effect.id
            ),
            select(SubscriptionAttempt.id).where(
                SubscriptionAttempt.run_id == run_id,
                SubscriptionAttempt.id != attempt_id,
                SubscriptionAttempt.status.in_(("running", "reconciling")),
            ),
            select(SubscriptionDecisionRecord.id).where(
                SubscriptionDecisionRecord.attempt_id == attempt_id
            ),
            select(AgentExecution.id).where(
                AgentExecution.run_id == run_id, AgentExecution.status == "RUNNING"
            ),
            select(Step.id).where(Step.run_id == run_id, Step.status == "RUNNING"),
        )
        for query in other:
            if await self.session.scalar(query.limit(1)) is not None:
                return _refused(
                    "other_work_unresolved",
                    "Another effect, publication or result must be reconciled first.",
                )
        digest = canonical_digest(
            {
                "action": ACTION.value,
                "run": _values(
                    run,
                    "id",
                    "version",
                    "state",
                    "suspended_state",
                    "policy_version",
                    "worktree_path",
                    "branch_name",
                    "base_sha",
                ),
                "task": _values(task, "id", "version", "payload", "state"),
                "attempt": _values(
                    attempt,
                    "id",
                    "task_version",
                    "status",
                    "lease_owner",
                    "lease_generation",
                    "telemetry_payload",
                ),
                "result": _values(result, "result_digest", "disposition", "application_digest"),
                "scheduled": _values(
                    scheduled,
                    "state",
                    "lease_owner",
                    "lease_generation",
                    "lease_expires_at",
                    "worktree_id",
                ),
                "operation": _values(
                    operation,
                    "id",
                    "request_digest",
                    "status",
                    "last_error",
                    "execution_owner",
                    "execution_lease_expires_at",
                ),
                "tool": _values(call, "id", "status", "normalized_arguments", "result_metadata"),
                "effect": _values(
                    effect, "id", "state", "lease_owner", "lease_generation", "candidate_epoch"
                ),
                "callback": _values(callback, "id", "payload", "receipt_payload"),
                "unstarted_reads": [
                    _values(row, "id", "payload", "receipt_payload") for row in pending_reads
                ],
                "launches": [
                    {
                        **_values(row, "id", "state", "pid", "terminal_payload"),
                        "process_identity": row.process_start_token,
                    }
                    for row in launches
                ],
            }
        )
        return _Source(
            run, task, attempt, result, scheduled, operation, call, effect, callback, digest
        )

    async def apply(
        self,
        run_id: UUID,
        task_id: UUID,
        attempt_id: UUID,
        *,
        actor_id: UUID,
        idempotency_key: str,
        request_digest: str,
        binding: str,
        reason: str,
    ) -> RecoveryReceipt:
        from forge.persistence.repositories.subscription_recovery import RecoveryConflict

        source = await self._source(run_id, task_id, attempt_id, locked=True, lock_run=True)
        if isinstance(source, RecoverySnapshot) or source.binding != binding:
            raise RecoveryConflict("recovery source differs from preview")
        now, receipt_id = datetime.now(UTC), uuid4()
        ack = {
            "schema_version": 1,
            "kind": "owner_acknowledged_git_preparation",
            "recovery_receipt_id": str(receipt_id),
            "actor_id": str(actor_id),
            "snapshot_digest": binding,
            "operation_id": str(source.operation.id),
            "owner_acknowledged_stopped": True,
            "stop_confirmed": False,
            "operation_outcome": "unknown",
            "warning": WARNING,
            "source_result_digest": source.result.result_digest,
        }
        source.operation.status = "FAILED"
        source.operation.last_error = WARNING
        source.operation.completed_at = now
        source.operation.execution_owner = None
        source.operation.execution_lease_expires_at = None
        source.call.status = "CANCELLED"
        source.call.completed_at = now
        source.call.result_metadata = {
            **(source.call.result_metadata or {}),
            "result_status": "cancelled",
            "recovery": ack,
            "error": {
                "code": "cancelled",
                "message": "Owner acknowledged Git preparation cessation; outcome unknown.",
            },
        }
        source.callback.receipt_payload = {
            "operation_id": str(source.operation.id),
            "accepted": False,
            "result": {"status": "cancelled", "recovery": ack},
        }
        source.effect.state = "rejected"
        source.result.disposition = "decision_pending"
        diagnostic = await self.session.get(SubscriptionApplicationDiagnostic, attempt_id)
        if diagnostic is not None:
            diagnostic.resolution = "scheduled"
            diagnostic.next_retry_at = now
        row = SubscriptionRecoveryReceipt(
            id=receipt_id,
            run_id=run_id,
            task_id=task_id,
            attempt_id=attempt_id,
            actor_id=str(actor_id),
            idempotency_key=idempotency_key,
            request_digest=request_digest,
            action=ACTION.value,
            reason_code="git_preparation_acknowledged",
            status="applied",
            reason=str(redact_value(reason))[:1000],
        )
        self.session.add(row)
        await self.session.flush()
        events = PostgresEventRepository(self.session)
        payload = {
            **ack,
            "task_id": str(task_id),
            "attempt_id": str(attempt_id),
            "saved_decision_pending": True,
        }
        if source.run.state == "AWAITING_HUMAN_INTERVENTION":
            await PostgresRunRepository(self.session, events=events).resume_interrupted_work(
                run_id,
                source.run.version,
                actor_id=actor_id,
                event_payload=payload,
                event_type="run.interrupted_git_preparation_recovered",
            )
        else:
            await events.append(
                RunEvent(
                    run_id=run_id,
                    run_version=source.run.version,
                    event_type="run.interrupted_git_preparation_recovered",
                    actor_class="operator",
                    actor_id=actor_id,
                    payload=payload,
                )
            )
        return RecoveryReceipt(
            receipt_id=receipt_id,
            run_id=run_id,
            task_id=task_id,
            attempt_id=attempt_id,
            action=ACTION,
            status="applied",
            observed_at=row.created_at,
            reason_code=row.reason_code,
        )
