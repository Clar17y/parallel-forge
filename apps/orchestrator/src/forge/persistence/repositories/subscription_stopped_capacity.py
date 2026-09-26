"""Read-only physical stop proof for capacity counts across independently locked runs."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forge.domain.subscription_launch import SubscriptionLaunchTerminalProof
from forge.domain.subscription_task_controls import AttemptTaskControlProof, TaskControlConflict
from forge.persistence.models.execution import ToolCall
from forge.persistence.models.scheduling import (
    SubscriptionScheduledEffect,
    SubscriptionScheduledTask,
)
from forge.persistence.models.subscription import (
    SubscriptionAttempt,
    SubscriptionClientLaunch,
    SubscriptionOperationBinding,
    SubscriptionTask,
)
from forge.persistence.models.subscription_results import SubscriptionAttemptResult
from forge.persistence.models.subscription_task_stops import SubscriptionTaskStop
from forge.persistence.repositories.mutations import MutationRepositoryError
from forge.persistence.repositories.subscription_launch import launches_confirmed
from forge.persistence.repositories.subscription_task_stop_receipts import (
    load_control,
    settlement_matches,
    stop_matches_receipt,
)


async def paused_task_is_stopped(
    session: AsyncSession, scheduled: SubscriptionScheduledTask
) -> bool:
    """Free only a proved stopped client's slot; never acquire another run's locks.

    This proves physical cessation. Run quiescence and task resume separately
    validate the current contract, usage and candidate authority before acting.
    """
    if (
        scheduled.state != "reconciling"
        or not scheduled.pause_requested
        or scheduled.cancel_requested
    ):
        return False
    stop = await session.scalar(
        select(SubscriptionTaskStop)
        .where(
            SubscriptionTaskStop.run_id == scheduled.run_id,
            SubscriptionTaskStop.task_id == scheduled.task_id,
            SubscriptionTaskStop.state == "paused",
        )
        .order_by(SubscriptionTaskStop.stop_task_version.desc())
        .limit(1)
        .execution_options(populate_existing=True)
    )
    if stop is None:
        return False
    try:
        pause = await load_control(session, stop.id, stop.run_id, stop.task_id)
        proof = pause.proof
        task = await session.get(SubscriptionTask, stop.task_id, populate_existing=True)
        attempt = await session.get(SubscriptionAttempt, stop.attempt_id, populate_existing=True)
        result = await session.get(
            SubscriptionAttemptResult, stop.attempt_id, populate_existing=True
        )
        if (
            not isinstance(proof, AttemptTaskControlProof)
            or pause.receipt.action != "pause"
            or not stop_matches_receipt(stop, pause)
            or task is None
            or attempt is None
            or result is None
            or task.run_id != scheduled.run_id
            or task.state != "reconciling"
            or not task.pause_requested
            or task.cancel_requested
            or stop.settled_task_version != task.version
            or (attempt.run_id, attempt.task_row_id) != (stop.run_id, stop.task_id)
            or attempt.status != "reconciling"
            or attempt.lease_owner != scheduled.lease_owner
            or attempt.lease_owner != proof.source.lease_owner
            or attempt.lease_generation != scheduled.lease_generation
            or attempt.lease_generation != proof.source.lease_generation
            or stop.lease_generation != scheduled.lease_generation
            or attempt.task_version != proof.source.admission_version
            or result.accepted
            or result.application_payload is not None
            or result.application_digest is not None
            or result.disposition
            not in (
                ("decision_pending",) if proof.source.kind == "pending" else ("stale", "fenced")
            )
            or not settlement_matches(stop, proof, result)
        ):
            return False
        terminal = SubscriptionLaunchTerminalProof.model_validate(
            result.result_payload.get("launch_proof")
        )
        launches = list(
            await session.scalars(
                select(SubscriptionClientLaunch)
                .where(
                    SubscriptionClientLaunch.attempt_id == attempt.id,
                )
                .execution_options(populate_existing=True)
            )
        )
        if not launches_confirmed(
            launches,
            terminal,
            require_decision=proof.source.kind == "pending",
            worker_identity=attempt.lease_owner,
        ):
            return False
        # None of these reads locks another run or turns unknown effects into completion.
        unresolved = (
            select(SubscriptionScheduledEffect.id).where(
                SubscriptionScheduledEffect.task_id == task.id,
                SubscriptionScheduledEffect.state.in_(("admitted", "reconciling")),
            ),
            select(ToolCall.id).where(
                ToolCall.subscription_task_id == task.id,
                ToolCall.status.in_(("PENDING", "RUNNING")),
            ),
            select(SubscriptionOperationBinding.id).where(
                SubscriptionOperationBinding.attempt_id == attempt.id,
                SubscriptionOperationBinding.receipt_payload.is_(None),
            ),
            select(SubscriptionAttempt.id).where(
                SubscriptionAttempt.task_row_id == task.id,
                SubscriptionAttempt.id != attempt.id,
                SubscriptionAttempt.status != "terminal",
            ),
        )
        for statement in unresolved:
            if await session.scalar(statement.limit(1)) is not None:
                return False
        return True
    except TaskControlConflict, MutationRepositoryError, TypeError, ValueError:
        return False
