"""Bound settled task history without accepting or replaying its provider results."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forge.domain.operation import canonical_digest
from forge.domain.subscription_launch import SubscriptionLaunchTerminalProof
from forge.domain.subscription_task_controls import IdleTaskControlProof, TaskControlConflict
from forge.persistence.models.api import OperatorAuditEvent
from forge.persistence.models.subscription import (
    SubscriptionAttempt,
    SubscriptionClientLaunch,
    SubscriptionOperationBinding,
    SubscriptionTask,
)
from forge.persistence.models.subscription_results import SubscriptionAttemptResult
from forge.persistence.repositories.subscription_launch import launches_confirmed
from forge.persistence.repositories.subscription_resumption import _Source
from forge.persistence.repositories.subscription_task_stop_receipts import load_control


async def idle_history_digest(session: AsyncSession, source: _Source) -> str:
    task, scheduled = source.task, source.scheduled
    if (
        task.cancel_requested
        or scheduled.cancel_requested
        or task.state not in ("queued", "blocked")
        or scheduled.state != task.state
        or scheduled.lease_owner is not None
        or scheduled.lease_expires_at is not None
        or source.attempt.status != "terminal"
        or source.attempt.lease_generation != scheduled.lease_generation
    ):
        raise TaskControlConflict("task is not settled and idle")
    latest = await session.scalar(
        select(SubscriptionAttempt.id)
        .where(SubscriptionAttempt.task_row_id == task.id)
        .order_by(SubscriptionAttempt.attempt_number.desc())
        .limit(1)
    )
    if latest != source.attempt.id:
        raise TaskControlConflict("latest task attempt differs")
    return await settled_history_digest(session, source.attempt)


async def settled_history_digest(session: AsyncSession, last: SubscriptionAttempt) -> str:
    # A replay binds only history preceding its receipt, including after later attempts.
    rows = (
        await session.execute(
            select(SubscriptionAttempt, SubscriptionAttemptResult)
            .outerjoin(
                SubscriptionAttemptResult,
                SubscriptionAttemptResult.attempt_id == SubscriptionAttempt.id,
            )
            .where(
                SubscriptionAttempt.task_row_id == last.task_row_id,
                SubscriptionAttempt.attempt_number <= last.attempt_number,
            )
            .order_by(SubscriptionAttempt.attempt_number, SubscriptionAttempt.id)
        )
    ).all()
    history = []
    for attempt, result in rows:
        if result is None or attempt.status != "terminal" or attempt.run_id != last.run_id:
            raise TaskControlConflict("task attempt history requires reconciliation")
        if (
            canonical_digest(result.result_payload) != result.result_digest
            or ((result.application_payload is None) != (result.application_digest is None))
            or (
                result.application_payload is not None
                and canonical_digest(result.application_payload) != result.application_digest
            )
        ):
            raise TaskControlConflict("task result history differs")
        try:
            terminal = SubscriptionLaunchTerminalProof.model_validate(
                result.result_payload.get("launch_proof")
            )
        except TypeError, ValueError:
            raise TaskControlConflict("task launch history differs") from None
        launches = (
            await session.scalars(
                select(SubscriptionClientLaunch)
                .where(SubscriptionClientLaunch.attempt_id == attempt.id)
                .order_by(SubscriptionClientLaunch.launch_id)
            )
        ).all()
        if not launches_confirmed(
            launches, terminal, require_decision=False, worker_identity=attempt.lease_owner
        ):
            raise TaskControlConflict("task client history requires reconciliation")
        if (
            await session.scalar(
                select(SubscriptionOperationBinding.id)
                .where(
                    SubscriptionOperationBinding.attempt_id == attempt.id,
                    SubscriptionOperationBinding.receipt_payload.is_(None),
                )
                .limit(1)
            )
            is not None
        ):
            raise TaskControlConflict("task operation history requires reconciliation")
        history.append(
            {
                "attempt_id": str(attempt.id),
                "attempt_number": attempt.attempt_number,
                "task_version": attempt.task_version,
                "task_digest": attempt.task_digest,
                "envelope_digest": attempt.envelope_digest,
                "route": attempt.route_payload,
                "candidate_epoch": attempt.candidate_epoch,
                "lease_owner": attempt.lease_owner,
                "lease_generation": attempt.lease_generation,
                "telemetry": attempt.telemetry_payload,
                "result_digest": result.result_digest,
                "disposition": result.disposition,
                "accepted": result.accepted,
                "application_digest": result.application_digest,
                "launches": [row.terminal_payload for row in launches],
            }
        )
    if not rows or rows[-1][0].id != last.id:
        raise TaskControlConflict("latest task attempt differs")
    return canonical_digest({"schema_version": 1, "attempts": history})


async def scope_request_control_version(
    session: AsyncSession,
    attempt: SubscriptionAttempt,
    base_version: int,
    *,
    replay: bool = False,
    receipt_id: UUID | None = None,
) -> tuple[int, UUID | None]:
    """Account only for a contiguous, audited series of idle pause/resume pairs.

    Persist the final receipt in the response so future controls cannot rewrite
    its causal version. Old responses without this reference keep their proof.
    """
    if replay and receipt_id is None:
        return base_version, None
    if not replay:
        task = await session.get(SubscriptionTask, attempt.task_row_id)
        if task is None:
            raise TaskControlConflict("scope task is absent")
        if task.version == base_version:
            return base_version, None
        receipt_id = await _resume_at_version(session, attempt.task_row_id, task.version)
    assert receipt_id is not None
    final_id = receipt_id
    history = await settled_history_digest(session, attempt)
    expected_version: int | None = None
    final_version: int | None = None
    while True:
        resume = await load_control(session, receipt_id, attempt.run_id, attempt.task_row_id)
        proof, receipt = resume.proof, resume.receipt
        if (
            not isinstance(proof, IdleTaskControlProof)
            or proof.source_attempt_id != attempt.id
            or proof.task_digest != attempt.task_digest
            or proof.envelope_digest != attempt.envelope_digest
            or proof.candidate_epoch != attempt.candidate_epoch
            or proof.history_digest != history
            or proof.idle_state != "blocked"
            or receipt.action != "resume"
            or receipt.status != "blocked"
            or receipt.pause_receipt_id is None
            or (expected_version is not None and receipt.task_version != expected_version)
        ):
            raise TaskControlConflict("scope task resume evidence differs")
        pause = await load_control(
            session, receipt.pause_receipt_id, attempt.run_id, attempt.task_row_id
        )
        if (
            pause.receipt.action != "pause"
            or pause.receipt.status != "paused"
            or pause.proof != proof
            or pause.receipt.task_version + 1 != receipt.task_version
        ):
            raise TaskControlConflict("scope task pause evidence differs")
        if final_version is None:
            final_version = receipt.task_version
        previous = pause.receipt.task_version - 1
        if previous == base_version:
            return final_version, final_id
        if previous < base_version:
            raise TaskControlConflict("scope task control versions differ")
        expected_version = previous
        receipt_id = await _resume_at_version(session, attempt.task_row_id, previous)


async def _resume_at_version(session: AsyncSession, task_id: UUID, version: int) -> UUID:
    ids = list(
        await session.scalars(
            select(OperatorAuditEvent.correlation_id)
            .where(
                OperatorAuditEvent.subject_type == "subscription_task",
                OperatorAuditEvent.subject_id == task_id,
                OperatorAuditEvent.event_type == "subscription.task_resume",
                OperatorAuditEvent.payload["task_version"].as_integer() == version,
            )
            .limit(2)
        )
    )
    if len(ids) != 1:
        raise TaskControlConflict("scope task version has no unique audited resume")
    return ids[0]
