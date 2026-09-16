"""Audited idempotent task controls and durable stopped-attempt reconciliation."""

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, Self
from uuid import UUID

from forge.application.ports.audit import AuditRepository
from forge.application.ports.mutations import ApiMutationRecord, MutationRepository
from forge.application.ports.runs import RunRepository
from forge.application.ports.subscription_task_controls import SubscriptionTaskControlRepository
from forge.application.services.auth import AuthenticatedActor
from forge.application.services.subscription_profiles import LocalOperatorProfileActor, ProfileActor
from forge.domain.operation import canonical_digest
from forge.domain.subscription_task_controls import (
    StoredTaskControl,
    SubscriptionTaskControlRequest,
    TaskControlConflict,
    TaskControlReceipt,
)
from forge.observability.redaction import redact_value

ACTION = "subscription.task.control"


class TaskControlUnitOfWork(Protocol):
    @property
    def runs(self) -> RunRepository: ...
    @property
    def task_controls(self) -> SubscriptionTaskControlRepository: ...
    @property
    def mutations(self) -> MutationRepository: ...
    @property
    def audit(self) -> AuditRepository: ...
    async def __aenter__(self) -> Self: ...
    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None: ...
    async def commit(self) -> None: ...
    async def rollback(self) -> None: ...


@dataclass(frozen=True, slots=True)
class TaskControlRecoveryReport:
    stopped: int = 0
    deferred: int = 0


def _stored(receipt: ApiMutationRecord, run_id: UUID, task_id: UUID) -> StoredTaskControl:
    try:
        if (
            receipt.action != ACTION
            or receipt.scope != f"run:{run_id}:task:{task_id}"
            or receipt.lifecycle_state != "COMPLETED"
            or receipt.resource_kind != "subscription_task"
            or receipt.resource_id != task_id
            or receipt.response_status != 200
        ):
            raise ValueError
        stored = StoredTaskControl.model_validate_json(json.dumps(receipt.response_payload))
        if (stored.receipt.receipt_id, stored.receipt.run_id, stored.receipt.task_id) != (
            receipt.id,
            run_id,
            task_id,
        ):
            raise ValueError
        return stored
    except TypeError, ValueError:
        raise TaskControlConflict("stored task control receipt differs") from None


class SubscriptionTaskControlService:
    def __init__(
        self,
        unit_of_work_factory: Callable[[], TaskControlUnitOfWork],
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._factory = unit_of_work_factory
        self._now = now or (lambda: datetime.now(UTC))

    async def reconcile_all(self) -> TaskControlRecoveryReport:
        """Finish requested stops in fresh transactions through the existing worker loop."""
        cursor = None
        stopped = deferred = 0
        while True:
            async with self._factory() as work:
                candidates = await work.task_controls.pending_stops(cursor, 100)
                await work.rollback()
            if not candidates:
                return TaskControlRecoveryReport(stopped, deferred)
            for run_id, receipt_id in candidates:
                try:
                    async with self._factory() as work:
                        changed = await work.task_controls.reconcile_stop(run_id, receipt_id)
                        await work.commit()
                    if changed is None:
                        deferred += 1
                    else:
                        stopped += int(changed)
                except Exception:  # noqa: BLE001 - one unproved stop must not starve other runs
                    deferred += 1
            cursor = candidates[-1][1]

    async def control(
        self,
        *,
        run_id: UUID,
        task_id: UUID,
        actor: ProfileActor,
        idempotency_key: str,
        request: SubscriptionTaskControlRequest,
    ) -> TaskControlReceipt:
        if not isinstance(actor, (AuthenticatedActor, LocalOperatorProfileActor)):
            raise TypeError("operator actor is required")
        if any(
            not isinstance(value, UUID) or value.int == 0
            for value in (run_id, task_id, actor.actor_id)
        ):
            raise ValueError("task control identity is invalid")
        if not isinstance(request, SubscriptionTaskControlRequest):
            raise TypeError("typed task control request is required")
        # Revalidate model_copy/model_construct inputs at this application boundary.
        body = SubscriptionTaskControlRequest.model_validate(request.model_dump())
        # Digest the original request without persisting its unredacted reason.
        digest = hashlib.sha256(
            json.dumps(
                body.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        async with self._factory() as work:
            # Run-first ordering also applies to mutation replays. Versions are
            # checked only for a new transition, so a replay returns its original receipt.
            await work.runs.get_for_update(run_id)
            mutation = await work.mutations.reserve(
                actor_id=actor.actor_id,
                action=ACTION,
                scope=f"run:{run_id}:task:{task_id}",
                idempotency_key=idempotency_key,
                request_digest=digest,
            )
            if mutation.is_replay:
                stored = _stored(mutation, run_id, task_id)
                await work.task_controls.verify_receipt(
                    stored,
                    actor_id=mutation.actor_id,
                    request_digest=mutation.request_digest,
                )
                await work.commit()
                return stored.receipt
            pause = None
            if body.pause_receipt_id is not None:
                pause_mutation = await work.mutations.get(body.pause_receipt_id)
                pause = _stored(pause_mutation, run_id, task_id)
                await work.task_controls.verify_receipt(
                    pause,
                    actor_id=pause_mutation.actor_id,
                    request_digest=pause_mutation.request_digest,
                )
                if pause.receipt.action != "pause":
                    raise TaskControlConflict("resume requires the task's pause receipt")
            observed_at = self._now()
            if (
                not isinstance(observed_at, datetime)
                or observed_at.tzinfo is None
                or observed_at.utcoffset() is None
            ):
                raise ValueError("task control clock must be timezone-aware")
            changed = await work.task_controls.apply(
                run_id, task_id, body, pause=pause, receipt_id=mutation.id
            )
            feedback = getattr(work, "subscription_feedback", None)
            if body.action == "cancel" and feedback is not None:
                await feedback.close_cancelled(run_id, task_id)
            receipt = TaskControlReceipt(
                receipt_id=mutation.id,
                run_id=run_id,
                task_id=task_id,
                action=body.action,
                status=changed.status,
                run_version=changed.run_version,
                task_version=changed.task_version,
                observed_at=observed_at.astimezone(UTC),
                reason=str(redact_value(body.reason))[:512],
                pause_receipt_id=body.pause_receipt_id,
            )
            payload = StoredTaskControl(receipt=receipt, proof=changed.proof).model_dump(
                mode="json"
            )
            await work.audit.append(
                actor_id=actor.actor_id,
                event_type=f"subscription.task_{body.action}",
                subject_type="subscription_task",
                subject_id=task_id,
                correlation_id=mutation.id,
                payload={
                    **receipt.model_dump(mode="json"),
                    "request_digest": digest,
                    "response_digest": canonical_digest(payload),
                },
            )
            await work.mutations.complete(
                mutation.id,
                response_status=200,
                response_payload=payload,
                resource_kind="subscription_task",
                resource_id=task_id,
            )
            await work.commit()
            return receipt
