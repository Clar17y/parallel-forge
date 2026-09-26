"""Authenticated, versioned and idempotent worker feedback submission."""

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol, Self
from uuid import UUID

from forge.application.ports.audit import AuditRepository
from forge.application.ports.mutations import ApiMutationRecord, MutationRepository
from forge.application.ports.runs import RunRepository
from forge.application.ports.subscription_feedback import SubscriptionFeedbackRepository
from forge.application.services.auth import AuthenticatedActor
from forge.application.services.subscription_profiles import LocalOperatorProfileActor, ProfileActor
from forge.domain.operation import canonical_digest
from forge.domain.subscription_feedback import (
    StoredTaskFeedback,
    SubscriptionTaskFeedbackRequest,
    TaskFeedbackConflict,
    TaskFeedbackReceipt,
)

ACTION = "subscription.task.feedback"


class TaskFeedbackUnitOfWork(Protocol):
    @property
    def runs(self) -> RunRepository: ...

    @property
    def subscription_feedback(self) -> SubscriptionFeedbackRepository: ...

    @property
    def mutations(self) -> MutationRepository: ...

    @property
    def audit(self) -> AuditRepository: ...

    async def __aenter__(self) -> Self: ...
    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None: ...
    async def commit(self) -> None: ...


def _stored(receipt: ApiMutationRecord, run_id: UUID, task_id: UUID) -> StoredTaskFeedback:
    try:
        if (
            receipt.action != ACTION
            or receipt.scope != f"run:{run_id}:task:{task_id}"
            or receipt.lifecycle_state != "COMPLETED"
            or receipt.resource_kind != "subscription_feedback"
            or receipt.resource_id != receipt.id
            or receipt.response_status != 200
        ):
            raise ValueError
        stored = StoredTaskFeedback.model_validate_json(json.dumps(receipt.response_payload))
        if (
            stored.receipt.receipt_id != receipt.id
            or stored.receipt.operator_id != receipt.actor_id
            or stored.receipt.run_id != run_id
            or stored.receipt.task_id != task_id
        ):
            raise ValueError
        return stored
    except TypeError, ValueError:
        raise TaskFeedbackConflict("stored task feedback receipt differs") from None


class SubscriptionTaskFeedbackService:
    def __init__(
        self,
        unit_of_work_factory: Callable[[], TaskFeedbackUnitOfWork],
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._factory = unit_of_work_factory
        self._now = now or (lambda: datetime.now(UTC))

    async def submit(
        self,
        *,
        run_id: UUID,
        task_id: UUID,
        actor: ProfileActor,
        idempotency_key: str,
        request: SubscriptionTaskFeedbackRequest,
    ) -> TaskFeedbackReceipt:
        if not isinstance(actor, (AuthenticatedActor, LocalOperatorProfileActor)):
            raise TypeError("operator actor is required")
        if any(
            not isinstance(value, UUID) or value.int == 0
            for value in (run_id, task_id, actor.actor_id)
        ):
            raise ValueError("task feedback identity is invalid")
        if not isinstance(request, SubscriptionTaskFeedbackRequest):
            raise TypeError("typed task feedback request is required")
        body = SubscriptionTaskFeedbackRequest.model_validate(request.model_dump())
        digest = hashlib.sha256(
            json.dumps(body.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        async with self._factory() as work:
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
                await work.subscription_feedback.verify_receipt(
                    stored,
                    actor_id=mutation.actor_id,
                    request_digest=mutation.request_digest,
                )
                await work.commit()
                return stored.receipt
            observed_at = self._now()
            if observed_at.tzinfo is None or observed_at.utcoffset() is None:
                raise ValueError("task feedback clock must be timezone-aware")
            changed = await work.subscription_feedback.submit(
                run_id,
                task_id,
                body,
                actor_id=actor.actor_id,
                request_digest=digest,
                receipt_id=mutation.id,
            )
            receipt = TaskFeedbackReceipt(
                receipt_id=mutation.id,
                operator_id=actor.actor_id,
                run_id=run_id,
                primary_task_id=changed.primary_task_id,
                task_id=task_id,
                status=changed.status,
                run_version=changed.run_version,
                task_version=changed.task_version,
                primary_task_version=changed.primary_task_version,
                feedback_digest=changed.feedback_digest,
                binding_digest=changed.binding_digest,
                feedback_bytes=changed.feedback_bytes,
                observed_at=observed_at.astimezone(UTC),
            )
            payload = StoredTaskFeedback(receipt=receipt).model_dump(mode="json")
            await work.audit.append(
                actor_id=actor.actor_id,
                event_type="subscription.task_feedback_submitted",
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
                resource_kind="subscription_feedback",
                resource_id=mutation.id,
            )
            await work.commit()
            return receipt


__all__ = ["SubscriptionTaskFeedbackService", "TaskFeedbackUnitOfWork"]
