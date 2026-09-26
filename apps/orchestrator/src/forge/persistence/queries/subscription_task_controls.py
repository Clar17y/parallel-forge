"""Read audited control history and current stop settlement without granting authority."""

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forge.domain.operation import canonical_digest
from forge.domain.subscription_task_controls import AttemptTaskControlProof, TaskControlConflict
from forge.observability.redaction import redact_value
from forge.persistence.models.api import OperatorAuditEvent
from forge.persistence.models.scheduling import SubscriptionScheduledTask
from forge.persistence.models.subscription import SubscriptionTask
from forge.persistence.models.subscription_results import SubscriptionAttemptResult
from forge.persistence.models.subscription_task_stops import SubscriptionTaskStop
from forge.persistence.repositories.mutations import MutationRepositoryError
from forge.persistence.repositories.subscription_task_stop_receipts import (
    load_control,
    settlement_matches,
    stop_matches_receipt,
)


async def latest_control_receipts(
    session: AsyncSession, task_ids: Sequence[UUID]
) -> dict[UUID, UUID]:
    if not task_ids:
        return {}
    # Task versions serialize controls even when clocks are equal or move backwards.
    return dict(
        (
            await session.execute(
                select(OperatorAuditEvent.subject_id, OperatorAuditEvent.correlation_id)
                .where(
                    OperatorAuditEvent.subject_type == "subscription_task",
                    OperatorAuditEvent.subject_id.in_(task_ids),
                    OperatorAuditEvent.event_type.in_(
                        (
                            "subscription.task_pause",
                            "subscription.task_cancel",
                            "subscription.task_resume",
                        )
                    ),
                )
                .distinct(OperatorAuditEvent.subject_id)
                .order_by(
                    OperatorAuditEvent.subject_id,
                    OperatorAuditEvent.payload["task_version"].as_integer().desc(),
                    OperatorAuditEvent.id,
                )
            )
        )
        .tuples()
        .all()
    )


async def control_view(
    session: AsyncSession,
    task: SubscriptionTask,
    scheduled: SubscriptionScheduledTask | None,
    receipt_id: UUID | None,
) -> dict[str, object] | None:
    if receipt_id is None:
        return None
    try:
        stored = await load_control(session, receipt_id, task.run_id, task.id)
        receipt, proof = stored.receipt, stored.proof
        status = receipt.status
        if receipt.task_version > task.version:
            return None
        if receipt.action != "resume":
            if scheduled is None or proof.task_digest != canonical_digest(task.payload):
                return None
            if isinstance(proof, AttemptTaskControlProof):
                stop = await session.get(SubscriptionTaskStop, receipt_id)
                if stop is None or not stop_matches_receipt(stop, stored):
                    return None
                if stop.state == "requested":
                    if task.version not in (stop.stop_task_version, stop.stop_task_version + 1):
                        return None
                elif stop.state in ("paused", "cancelled"):
                    result = await session.get(SubscriptionAttemptResult, stop.attempt_id)
                    if (
                        result is None
                        or not settlement_matches(stop, proof, result)
                        or task.version != stop.settled_task_version
                    ):
                        return None
                    status = "paused" if stop.state == "paused" else "cancelled"
                else:
                    # A later audited resume/cancellation must be the current receipt.
                    return None
            elif receipt.task_version != task.version:
                return None
            if receipt.action == "pause":
                if (
                    not task.pause_requested
                    or not scheduled.pause_requested
                    or task.cancel_requested
                    or scheduled.cancel_requested
                ):
                    return None
                if status == "paused" and (
                    task.state != scheduled.state or task.state not in ("blocked", "reconciling")
                ):
                    return None
            elif not task.cancel_requested or not scheduled.cancel_requested:
                return None
        return {
            "receipt_id": receipt.receipt_id,
            "action": receipt.action,
            "status": status,
            "reason": str(redact_value(receipt.reason)),
            "observed_at": receipt.observed_at,
            "pause_receipt_id": receipt.receipt_id if status == "paused" else None,
        }
    except TaskControlConflict, MutationRepositoryError, TypeError, ValueError:
        # A malformed receipt never becomes a resume affordance or exposes its payload.
        return None
