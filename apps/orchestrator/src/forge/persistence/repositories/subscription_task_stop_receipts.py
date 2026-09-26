"""Validate dedicated task-stop authority without changing original attempt identity."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forge.application.services.subscription_task_controls import _stored
from forge.domain.operation import canonical_digest
from forge.domain.subscription_task_controls import (
    AttemptTaskControlProof,
    StoredTaskControl,
    TaskControlConflict,
)
from forge.persistence.models.api import OperatorAuditEvent
from forge.persistence.models.subscription import SubscriptionAttempt
from forge.persistence.models.subscription_results import SubscriptionAttemptResult
from forge.persistence.models.subscription_task_stops import SubscriptionTaskStop
from forge.persistence.repositories.mutations import (
    MutationRepositoryError,
    PostgresMutationRepository,
)


async def verify_control_receipt(
    session: AsyncSession, stored: StoredTaskControl, *, actor_id: UUID, request_digest: str
) -> None:
    receipt = stored.receipt
    events = list(
        await session.scalars(
            select(OperatorAuditEvent)
            .where(
                OperatorAuditEvent.correlation_id == receipt.receipt_id,
                OperatorAuditEvent.subject_type == "subscription_task",
                OperatorAuditEvent.subject_id == receipt.task_id,
            )
            .limit(2)
        )
    )
    expected = {
        **receipt.model_dump(mode="json"),
        "request_digest": request_digest,
        "response_digest": canonical_digest(stored.model_dump(mode="json")),
    }
    if len(events) != 1 or (
        events[0].actor_id != actor_id
        or events[0].event_type != f"subscription.task_{receipt.action}"
        or events[0].schema_version != 1
        or events[0].payload != expected
    ):
        raise TaskControlConflict("task control receipt does not match its audit evidence")


async def load_control(
    session: AsyncSession, receipt_id: UUID, run_id: UUID, task_id: UUID
) -> StoredTaskControl:
    mutation = await PostgresMutationRepository(session).get(receipt_id)
    stored = _stored(mutation, run_id, task_id)
    await verify_control_receipt(
        session, stored, actor_id=mutation.actor_id, request_digest=mutation.request_digest
    )
    return stored


def stop_matches_receipt(stop: SubscriptionTaskStop, stored: StoredTaskControl) -> bool:
    proof, receipt = stored.proof, stored.receipt
    return isinstance(proof, AttemptTaskControlProof) and (
        (
            stop.id,
            stop.run_id,
            stop.task_id,
            stop.attempt_id,
            stop.stop_task_version,
            stop.lease_generation,
        )
        == (
            receipt.receipt_id,
            receipt.run_id,
            receipt.task_id,
            proof.source.attempt_id,
            receipt.task_version,
            proof.source.lease_generation,
        )
        and receipt.action in ("pause", "cancel")
        and receipt.task_version == proof.source.task_version + 1
    )


def settlement_payload(
    stop: SubscriptionTaskStop, proof: AttemptTaskControlProof, result: SubscriptionAttemptResult
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "operator_task_stopped",
        "stop_receipt_id": str(stop.id),
        "run_id": str(stop.run_id),
        "task_id": str(stop.task_id),
        "attempt_id": str(stop.attempt_id),
        "proof_digest": canonical_digest(proof.model_dump(mode="json")),
        "result_digest": result.result_digest,
        "result_payload_digest": canonical_digest(result.result_payload),
        "stop_task_version": stop.stop_task_version,
        "settled_task_version": stop.settled_task_version,
        "lease_generation": stop.lease_generation,
    }


def settlement_matches(
    stop: SubscriptionTaskStop, proof: AttemptTaskControlProof, result: SubscriptionAttemptResult
) -> bool:
    return (
        stop.settled_task_version is not None
        and stop.settlement_payload == settlement_payload(stop, proof, result)
        and canonical_digest(stop.settlement_payload) == stop.settlement_digest
        and result.result_digest == canonical_digest(result.result_payload)
    )


async def pending_decision_task_version(
    session: AsyncSession, attempt: SubscriptionAttempt, result: SubscriptionAttemptResult
) -> int | None:
    """A pending decision gains a new version only through its exact audited resume."""
    if attempt.task_version is None:
        return None
    stop = await session.scalar(
        select(SubscriptionTaskStop)
        .where(SubscriptionTaskStop.attempt_id == attempt.id)
        .order_by(SubscriptionTaskStop.stop_task_version.desc())
        .limit(1)
    )
    if stop is None:
        return attempt.task_version + 1
    if stop.state != "resumed" or stop.resume_receipt_id is None:
        return None
    try:
        pause = await load_control(session, stop.id, attempt.run_id, attempt.task_row_id)
        resume = await load_control(
            session, stop.resume_receipt_id, attempt.run_id, attempt.task_row_id
        )
        proof = pause.proof
        if (
            not isinstance(proof, AttemptTaskControlProof)
            or not stop_matches_receipt(stop, pause)
            or pause.receipt.action != "pause"
            or proof.source.kind != "pending"
            or proof.source.result_digest != result.result_digest
            or proof.source.admission_version != attempt.task_version
            or (proof.source.lease_owner, proof.source.lease_generation)
            != (attempt.lease_owner, attempt.lease_generation)
            or not settlement_matches(stop, proof, result)
            or stop.settled_task_version != stop.stop_task_version
            or resume.receipt.action != "resume"
            or resume.receipt.status != "decision_pending"
            or resume.receipt.pause_receipt_id != stop.id
            or resume.proof != proof
            or resume.receipt.task_version != stop.resumed_task_version
            or stop.resumed_task_version != stop.stop_task_version + 1
        ):
            return None
        return stop.resumed_task_version
    except TaskControlConflict, MutationRepositoryError, TypeError, ValueError:
        return None
