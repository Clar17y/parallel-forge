"""Owner-attested interruption settlement; never a command or process result.

Legacy named checks have no durable runner identity. This explicit owner action
is the authority to relinquish their hold, not an inference from lease expiry.
It deliberately cannot produce a successful check or supervisor stop proof.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forge.domain.event import RunEvent as Event
from forge.domain.operation import canonical_digest
from forge.domain.subscription import AttemptTelemetry, decode_subscription_record
from forge.domain.subscription_recovery import RecoveryAction, RecoveryReceipt, RecoverySnapshot
from forge.domain.tool import ToolName
from forge.observability.redaction import redact_value
from forge.persistence.models import Run
from forge.persistence.models.execution import (
    AgentExecution,
    ArtifactLineage,
    OperationIntent,
    RunEvent,
    Step,
    ToolCall,
)
from forge.persistence.models.scheduling import (
    SubscriptionScheduledEffect,
    SubscriptionScheduledTask,
    SubscriptionSchedulerRun,
)
from forge.persistence.models.subscription import (
    SubscriptionAttempt,
    SubscriptionClientLaunch,
    SubscriptionOperationBinding,
    SubscriptionTask,
)
from forge.persistence.models.subscription_recovery import SubscriptionRecoveryReceipt
from forge.persistence.models.subscription_results import SubscriptionAttemptResult
from forge.persistence.models.subscription_usage import SubscriptionAttemptConsumption
from forge.persistence.repositories.commands import PostgresCommandRepository
from forge.persistence.repositories.events import PostgresEventRepository
from forge.persistence.repositories.runs import PostgresRunRepository
from forge.persistence.repositories.subscription_budget import PostgresSubscriptionBudgetRepository
from forge.persistence.repositories.subscription_launch import launches_confirmed
from forge.persistence.repositories.subscription_quota import PostgresSubscriptionQuotaRepository

ACTION = RecoveryAction.ACKNOWLEDGE_INTERRUPTED_CHECK
WARNING = (
    "The owner acknowledges that the prior worker, provider and check processes have stopped. "
    "Forge cannot prove their cessation or recover the check outcome; both the outcome "
    "and missing usage remain unknown. This action retains existing work and never marks the check passed."
)


def _values(row: object, *fields: str) -> dict[str, Any]:
    return {
        field: value.isoformat()
        if isinstance(value, datetime)
        else str(value)
        if isinstance(value, UUID)
        else value
        for field in fields
        for value in (getattr(row, field),)
    }


def _attempt_telemetry(attempt: SubscriptionAttempt) -> AttemptTelemetry | None:
    if attempt.telemetry_payload is None:
        return None
    telemetry = decode_subscription_record(attempt.telemetry_payload)
    if not isinstance(telemetry, AttemptTelemetry):
        raise TypeError("interrupted attempt has invalid telemetry")
    return telemetry


async def owner_interruption_history_digest(
    session: AsyncSession,
    attempt: SubscriptionAttempt,
) -> str | None:
    """Bind explicit owner authority for history; never provide supervisor proof."""
    if attempt.status != "terminal":
        return None
    calls = list(
        (
            await session.scalars(
                select(ToolCall).where(
                    ToolCall.subscription_attempt_id == attempt.id,
                    ToolCall.status == "CANCELLED",
                    ToolCall.tool_name == ToolName.BUILD_RUN_NAMED_CHECK.value,
                )
            )
        ).all()
    )
    calls = [
        call for call in calls if isinstance((call.result_metadata or {}).get("recovery"), dict)
    ]
    if len(calls) != 1:
        return None
    call = calls[0]
    ack = (call.result_metadata or {})["recovery"]
    try:
        receipt_id = UUID(ack["recovery_receipt_id"])
    except KeyError, TypeError, ValueError:
        return None
    receipt = await session.get(SubscriptionRecoveryReceipt, receipt_id)
    operation = await session.get(OperationIntent, call.id)
    effect = await session.get(SubscriptionScheduledEffect, call.id)
    usage = await session.get(SubscriptionAttemptConsumption, attempt.id)
    if (
        receipt is None
        or operation is None
        or effect is None
        or usage is None
        or receipt.action != ACTION.value
        or receipt.status != "applied"
        or receipt.run_id != attempt.run_id
        or receipt.task_id != attempt.task_row_id
        or receipt.attempt_id != attempt.id
        or receipt.actor_id != ack.get("actor_id")
        or call.run_id != attempt.run_id
        or call.subscription_task_id != attempt.task_row_id
        or not call.authorized
        or operation.run_id != attempt.run_id
        or operation.status != "FAILED"
        or operation.outcome_payload is not None
        or operation.completed_at is None
        or effect.state != "rejected"
        or effect.task_id != attempt.task_row_id
        or effect.run_id != attempt.run_id
        or ack.get("kind") != "owner_acknowledged_interruption"
        or ack.get("operation_id") != str(call.id)
        or ack.get("owner_acknowledged_stopped") is not True
        or ack.get("stop_confirmed") is not False
        or ack.get("check_outcome") != "unknown"
    ):
        return None
    bindings = list(
        (
            await session.scalars(
                select(SubscriptionOperationBinding)
                .where(
                    SubscriptionOperationBinding.attempt_id == attempt.id,
                )
                .order_by(SubscriptionOperationBinding.id)
            )
        ).all()
    )
    callbacks = [row for row in bindings if row.durable_operation_id == call.id]
    if len(callbacks) != 1 or any(row.receipt_payload is None for row in bindings):
        return None
    if callbacks[0].receipt_payload != {
        "operation_id": str(call.id),
        "accepted": False,
        "result": {"status": "cancelled", "recovery": ack},
    }:
        return None
    launches = list(
        (
            await session.scalars(
                select(SubscriptionClientLaunch)
                .where(
                    SubscriptionClientLaunch.attempt_id == attempt.id,
                )
                .order_by(SubscriptionClientLaunch.launch_id)
            )
        ).all()
    )
    if not launches:
        return None
    for launch in launches:
        if launch.state != "terminal" or launch.worker_identity != attempt.lease_owner:
            return None
        if launch.terminal_payload != ack and not launches_confirmed(
            [launch],
            None,
            require_decision=False,
            worker_identity=attempt.lease_owner,
        ):
            return None
    return canonical_digest(
        {
            "schema_version": 1,
            "authority": "owner_acknowledged_interruption",
            "attempt": _values(
                attempt,
                "id",
                "attempt_number",
                "task_version",
                "task_digest",
                "envelope_digest",
                "route_payload",
                "candidate_epoch",
                "lease_owner",
                "lease_generation",
                "telemetry_payload",
            ),
            "receipt_id": str(receipt.id),
            "acknowledgment": ack,
            "operation": _values(operation, "request_digest", "last_error"),
            "tool": _values(call, "normalized_arguments", "result_metadata"),
            "bindings": [_values(row, "id", "payload", "receipt_payload") for row in bindings],
            "launches": [
                {
                    **_values(row, "launch_id", "pid", "terminal_payload"),
                    "process_identity": row.process_start_token,
                }
                for row in launches
            ],
            "usage": _values(usage, "observed", "charged", "unknown_fields", "uncertain"),
        }
    )


class InterruptedCheckRecovery:
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
        def refused(code: str, message: str) -> RecoverySnapshot:
            return RecoverySnapshot("", False, code, message, (), (), 0, 0)

        run = await self.session.get(Run, run_id, with_for_update=lock_run)
        task = await self.session.get(SubscriptionTask, task_id, with_for_update=locked)
        attempt = await self.session.get(SubscriptionAttempt, attempt_id, with_for_update=locked)
        scheduled = await self.session.get(
            SubscriptionScheduledTask, task_id, with_for_update=locked
        )
        scheduler_run = await self.session.get(
            SubscriptionSchedulerRun, run_id, with_for_update=locked
        )
        if (
            run is None
            or task is None
            or attempt is None
            or scheduled is None
            or scheduler_run is None
            or task.run_id != run_id
            or attempt.run_id != run_id
            or attempt.task_row_id != task_id
            or scheduled.run_id != run_id
        ):
            return refused("source_missing", "The interrupted attempt is unavailable.")
        latest = await self.session.scalar(
            select(SubscriptionAttempt.id)
            .where(SubscriptionAttempt.task_row_id == task_id)
            .order_by(SubscriptionAttempt.attempt_number.desc())
            .limit(1)
        )
        if latest != attempt_id:
            return refused("source_changed", "A newer attempt supersedes this interruption.")
        if await self.session.get(SubscriptionAttemptResult, attempt_id) is not None:
            return refused(
                "result_available", "Recover the saved result without discarding its evidence."
            )
        try:
            _attempt_telemetry(attempt)
        except TypeError, ValueError:
            return refused(
                "telemetry_unresolved", "Saved usage is invalid and requires reconciliation."
            )
        if (
            run.state != "AWAITING_HUMAN_INTERVENTION"
            or run.suspension_kind != "INTERVENTION"
            or run.suspended_state not in {"PLANNING", "IMPLEMENTING", "REMEDIATING"}
            or run.pending_gate is not None
            or task.pause_requested
            or task.cancel_requested
            or scheduled.pause_requested
            or scheduled.cancel_requested
            or await PostgresCommandRepository(
                session=self.session
            ).has_pending_current_control_stop(run_id=run_id, expected_run_version=run.version)
        ):
            return refused(
                "run_controlled", "A pause, cancellation, or another run decision takes precedence."
            )
        if (
            attempt.status not in {"running", "reconciling"}
            or scheduled.state not in {"leased", "reconciling"}
            or scheduled.lease_generation != attempt.lease_generation
            or scheduled.lease_owner != attempt.lease_owner
            or scheduler_run.candidate_epoch != attempt.candidate_epoch
            or scheduler_run.candidate_state != "open"
            or attempt.task_digest != canonical_digest(task.payload)
        ):
            return refused(
                "source_changed", "Attempt ownership or candidate changed; refresh the source."
            )
        now = datetime.now(UTC)
        if scheduled.lease_expires_at is None or scheduled.lease_expires_at > now:
            return refused(
                "ownership_active", "The previous worker still owns an active or uncertain lease."
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
            return refused(
                "effect_uncertain", "Recovery requires exactly one unresolved named check."
            )
        effect = effects[0]
        other_work = (
            select(OperationIntent.id).where(
                OperationIntent.run_id == run_id,
                OperationIntent.id != effect.id,
                OperationIntent.status.in_(("PENDING", "NEEDS_RECONCILIATION")),
            ),
            select(ToolCall.id).where(
                ToolCall.run_id == run_id,
                ToolCall.id != effect.id,
                ToolCall.status.in_(("PENDING", "RUNNING")),
            ),
            select(SubscriptionOperationBinding.id).where(
                SubscriptionOperationBinding.attempt_id == attempt_id,
                SubscriptionOperationBinding.durable_operation_id != effect.id,
                SubscriptionOperationBinding.receipt_payload.is_(None),
            ),
            select(SubscriptionAttempt.id).where(
                SubscriptionAttempt.run_id == run_id,
                SubscriptionAttempt.id != attempt_id,
                SubscriptionAttempt.status.in_(("running", "reconciling")),
            ),
            select(AgentExecution.id).where(
                AgentExecution.run_id == run_id, AgentExecution.status == "RUNNING"
            ),
            select(Step.id).where(Step.run_id == run_id, Step.status == "RUNNING"),
        )
        for query in other_work:
            if await self.session.scalar(query.limit(1)) is not None:
                return refused(
                    "other_work_unresolved", "Other interrupted work must be recovered first."
                )
        operation = await self.session.get(OperationIntent, effect.id, with_for_update=locked)
        call = await self.session.get(ToolCall, effect.id, with_for_update=locked)
        bindings = list(
            (
                await self.session.scalars(
                    select(SubscriptionOperationBinding).where(
                        SubscriptionOperationBinding.durable_operation_id == effect.id,
                    )
                )
            ).all()
        )
        if operation is None or call is None or len(bindings) != 1:
            return refused("source_missing", "The check's durable identity is incomplete.")
        callback = bindings[0]
        request = operation.request_payload
        metadata = call.result_metadata or {}
        if (
            effect.task_id != task_id
            or effect.lease_owner != attempt.lease_owner
            or effect.lease_generation != attempt.lease_generation
            or effect.candidate_epoch != attempt.candidate_epoch
            or not effect.whole_worktree_exclusive
            or operation.run_id != run_id
            or operation.operation_kind != "named_check"
            or operation.status not in {"PENDING", "NEEDS_RECONCILIATION"}
            or operation.outcome_payload is not None
            or operation.completed_at is not None
            or canonical_digest(request) != operation.request_digest
            or any(
                request.get(key) != expected
                for key, expected in {
                    "authority_schema_version": 2,
                    "protocol_version": 1,
                    "run_id": str(run_id),
                    "project_id": str(run.project_id),
                    "subscription_task_id": str(task_id),
                    "subscription_attempt_id": str(attempt_id),
                    "tool_call_id": str(effect.id),
                    "worktree_id": scheduled.worktree_id,
                    "policy_version": run.policy_version,
                    "subscription_purpose": call.subscription_purpose,
                    "command_name": call.normalized_arguments.get("command_name"),
                }.items()
            )
            or call.run_id != run_id
            or call.subscription_task_id != task_id
            or call.subscription_attempt_id != attempt_id
            or not call.authorized
            or call.status != "RUNNING"
            or call.completed_at is not None
            or call.tool_name != ToolName.BUILD_RUN_NAMED_CHECK.value
            or any(
                metadata.get(key) != expected
                for key, expected in {
                    "operation_intent_id": str(effect.id),
                    "correlation_id": str(effect.id),
                    "resource_id": scheduled.worktree_id,
                    "policy_version": run.policy_version,
                    "request_digest": canonical_digest(call.normalized_arguments),
                    "invocation_schema_version": 1,
                }.items()
            )
            or callback.attempt_id != attempt_id
            or callback.receipt_payload is not None
        ):
            return refused(
                "source_changed", "Check authority, request, or result no longer matches."
            )
        if operation.execution_owner is not None and (
            operation.execution_lease_expires_at is None
            or operation.execution_lease_expires_at > now
        ):
            return refused(
                "ownership_active", "The check still has an active or uncertain execution owner."
            )
        if (
            await self.session.scalar(
                select(ArtifactLineage.id)
                .where(
                    ArtifactLineage.run_id == run_id,
                    ArtifactLineage.producer_id == effect.id,
                )
                .limit(1)
            )
            is not None
        ):
            return refused(
                "result_available",
                "A check artifact exists; recover its result before owner settlement.",
            )
        launches = list(
            (
                await self.session.scalars(
                    select(SubscriptionClientLaunch)
                    .where(
                        SubscriptionClientLaunch.attempt_id == attempt_id,
                    )
                    .order_by(SubscriptionClientLaunch.id)
                )
            ).all()
        )
        if not launches or any(row.worker_identity != attempt.lease_owner for row in launches):
            return refused("source_changed", "Provider launch ownership is missing or mismatched.")
        startup = await self.session.scalar(
            select(RunEvent.id)
            .where(
                RunEvent.run_id == run_id,
                RunEvent.event_type == "run.recovery_intervention",
                RunEvent.run_version == run.version,
            )
            .limit(1)
        )
        if startup is None:
            return refused("run_controlled", "This is not the recorded startup interruption.")
        binding = canonical_digest(
            {
                "action": ACTION.value,
                "run": _values(run, "id", "version", "state", "suspended_state", "policy_version"),
                "task": _values(task, "id", "version", "state", "payload"),
                "attempt": _values(
                    attempt, "id", "status", "lease_owner", "lease_generation", "telemetry_payload"
                ),
                "scheduled": _values(
                    scheduled,
                    "state",
                    "lease_owner",
                    "lease_generation",
                    "lease_expires_at",
                    "worktree_id",
                    "repairs",
                    "max_repairs",
                ),
                "epoch": scheduler_run.candidate_epoch,
                "operation": _values(
                    operation,
                    "id",
                    "request_digest",
                    "status",
                    "execution_owner",
                    "execution_lease_expires_at",
                    "remote_resource_id",
                ),
                "tool": _values(call, "id", "normalized_arguments", "result_metadata", "status"),
                "effect": _values(effect, "id", "state", "lease_owner", "lease_generation"),
                "callback": _values(callback, "id", "payload", "receipt_payload"),
                "launches": [
                    {
                        **_values(row, "id", "state", "pid", "terminal_payload"),
                        "process_identity": row.process_start_token,
                    }
                    for row in launches
                ],
            }
        )
        return RecoverySnapshot(
            binding,
            True,
            "owner_acknowledgment_required",
            WARNING,
            (
                "Settle this interrupted check as unknown; do not replay its operation.",
                "Charge unknown usage conservatively, then queue a fresh attempt only if existing budgets allow.",
                "Budget impact is an upper bound; settlement can leave the run blocked without a retry.",
            ),
            ("Worktree, original requests, process identities, artifacts and usage are retained.",),
            1,
            1,
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
        # The caller holds the run and source-row locks and checked the signed snapshot.
        run = await self.session.get(Run, run_id)
        task = await self.session.get(SubscriptionTask, task_id)
        attempt = await self.session.get(SubscriptionAttempt, attempt_id)
        scheduled = await self.session.get(SubscriptionScheduledTask, task_id)
        assert (
            run is not None and task is not None and attempt is not None and scheduled is not None
        )
        effect = await self.session.scalar(
            select(SubscriptionScheduledEffect).where(
                SubscriptionScheduledEffect.run_id == run_id,
                SubscriptionScheduledEffect.state.in_(("admitted", "reconciling")),
            )
        )
        assert effect is not None
        operation = await self.session.get(OperationIntent, effect.id)
        call = await self.session.get(ToolCall, effect.id)
        callback = await self.session.scalar(
            select(SubscriptionOperationBinding).where(
                SubscriptionOperationBinding.durable_operation_id == effect.id,
            )
        )
        assert operation is not None and call is not None and callback is not None
        assert call.result_metadata is not None
        now, receipt_id = datetime.now(UTC), uuid4()
        acknowledgment = {
            "schema_version": 1,
            "kind": "owner_acknowledged_interruption",
            "recovery_receipt_id": str(receipt_id),
            "actor_id": str(actor_id),
            "snapshot_digest": binding,
            "operation_id": str(effect.id),
            "owner_acknowledged_stopped": True,
            "stop_confirmed": False,
            "check_outcome": "unknown",
            "usage_outcome": "unknown",
            "warning": WARNING,
        }
        operation.status = "FAILED"
        operation.outcome_schema_version = None
        operation.outcome_payload = None
        operation.last_error = WARNING
        operation.completed_at = now
        operation.execution_owner = None
        operation.execution_lease_expires_at = None
        call.status = "CANCELLED"
        call.completed_at = now
        call.result_metadata = {
            **call.result_metadata,
            "result_status": "cancelled",
            "recovery": acknowledgment,
            "error": {
                "code": "cancelled",
                "message": "Owner acknowledged interrupted check; outcome unknown.",
            },
        }
        callback.receipt_payload = {
            "operation_id": str(effect.id),
            "accepted": False,
            "result": {"status": "cancelled", "recovery": acknowledgment},
        }
        effect.state = "rejected"
        launches = (
            await self.session.scalars(
                select(SubscriptionClientLaunch).where(
                    SubscriptionClientLaunch.attempt_id == attempt_id,
                )
            )
        ).all()
        for launch in launches:
            if launch.state != "terminal":
                launch.state = "terminal"
                launch.terminal_payload = acknowledgment
        budget = PostgresSubscriptionBudgetRepository(self.session)
        telemetry = _attempt_telemetry(attempt)
        await budget.settle_attempt(run_id, task_id, attempt_id, telemetry)
        retry = scheduled.repairs < scheduled.max_repairs and await budget.try_debit_repair(
            run_id,
            task_id,
            attempt_id,
        )
        await PostgresSubscriptionQuotaRepository(self.session).settle(
            attempt_id,
            exhaustion=None,
            succeeded=False,
            stopped=True,
        )
        attempt.status = "terminal"
        scheduled.state = "queued" if retry else "blocked"
        scheduled.lease_owner = None
        scheduled.lease_expires_at = None
        scheduled.repairs += int(retry)
        task.state = scheduled.state
        task.version += 1
        reason_code = (
            "interruption_acknowledged" if retry else "interruption_acknowledged_budget_blocked"
        )
        recovery = SubscriptionRecoveryReceipt(
            id=receipt_id,
            run_id=run_id,
            task_id=task_id,
            attempt_id=attempt_id,
            actor_id=str(actor_id),
            idempotency_key=idempotency_key,
            request_digest=request_digest,
            action=ACTION.value,
            reason_code=reason_code,
            status="applied",
            reason=str(redact_value(reason))[:1000],
        )
        self.session.add(recovery)
        await self.session.flush()
        events = PostgresEventRepository(self.session)
        event_payload = {
            **acknowledgment,
            "task_id": str(task_id),
            "attempt_id": str(attempt_id),
            "retry_queued": retry,
            "reason_code": reason_code,
        }
        if retry:
            await PostgresRunRepository(self.session, events=events).resume_interrupted_work(
                run_id,
                run.version,
                actor_id=actor_id,
                event_payload=event_payload,
            )
        else:
            await events.append(
                Event(
                    run_id=run_id,
                    run_version=run.version,
                    event_type="run.interrupted_check_recovered",
                    actor_class="operator",
                    actor_id=actor_id,
                    payload=event_payload,
                )
            )
        return RecoveryReceipt(
            receipt_id=receipt_id,
            run_id=run_id,
            task_id=task_id,
            attempt_id=attempt_id,
            action=ACTION,
            status="applied",
            observed_at=recovery.created_at,
            reason_code=reason_code,
        )
