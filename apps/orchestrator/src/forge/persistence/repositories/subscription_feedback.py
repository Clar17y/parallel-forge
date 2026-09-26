"""PostgreSQL lifecycle for exact operator feedback delivery."""

import hashlib
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from forge.application.ports.subscription_execution import SubscriptionAdmission
from forge.application.ports.subscription_feedback import (
    FeedbackInvocationContext,
    FeedbackTransition,
)
from forge.domain.operation import canonical_digest
from forge.domain.payload import validate_durable_payload
from forge.domain.run import RunState
from forge.domain.subscription import (
    ExecutionEnvelope,
    ForwardFeedbackDecision,
    LogicalTaskContract,
    SpecialistPurpose,
    decode_subscription_record,
    encode_subscription_record,
)
from forge.domain.subscription_feedback import (
    MAX_FEEDBACK_PER_TASK,
    StoredTaskFeedback,
    SubscriptionTaskFeedbackRequest,
    TaskFeedbackConflict,
)
from forge.persistence.models.run import Run
from forge.persistence.models.scheduling import SubscriptionScheduledTask
from forge.persistence.models.subscription import (
    SubscriptionAttempt,
    SubscriptionClientLaunch,
    SubscriptionDecisionRecord,
    SubscriptionEnvelope,
    SubscriptionTask,
)
from forge.persistence.models.subscription_feedback import SubscriptionTaskFeedback
from forge.persistence.models.subscription_results import SubscriptionAttemptResult
from forge.persistence.models.subscription_usage import (
    SubscriptionAttemptConsumption,
    SubscriptionAttemptReservation,
)
from forge.persistence.repositories.scheduling import PostgresSchedulingRepository
from forge.persistence.repositories.subscription_budget import PostgresSubscriptionBudgetRepository


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _binding_digest(row: SubscriptionTaskFeedback) -> str:
    return canonical_digest(
        {
            "schema_version": 1,
            "receipt_id": str(row.id),
            "operator_id": str(row.actor_id),
            "run_id": str(row.run_id),
            "primary_task_id": str(row.primary_task_id),
            "task_id": str(row.task_id),
            "observed_run_version": row.observed_run_version,
            "observed_task_version": row.observed_task_version,
            "observed_primary_version": row.observed_primary_version,
            "observed_task_digest": row.observed_task_digest,
            "observed_primary_digest": row.observed_primary_digest,
            "envelope_digest": row.envelope_digest,
            "request_digest": row.request_digest,
            "feedback_digest": row.feedback_digest,
            "feedback_bytes": row.feedback_bytes,
        }
    )


def _context(row: SubscriptionTaskFeedback) -> dict[str, object]:
    payload: dict[str, object] = {
        "receipt_id": str(row.id),
        "operator_id": str(row.actor_id),
        "run_id": str(row.run_id),
        "primary_task_id": str(row.primary_task_id),
        "task_id": str(row.task_id),
        "observed_run_version": row.observed_run_version,
        "observed_task_version": row.observed_task_version,
        "observed_primary_version": row.observed_primary_version,
        "feedback": row.feedback,
        "feedback_digest": row.feedback_digest,
        "binding_digest": _binding_digest(row),
        "feedback_bytes": row.feedback_bytes,
    }
    validate_durable_payload(payload)
    return payload


def _close_before_forwarding(row: SubscriptionTaskFeedback, reason: str) -> None:
    application: dict[str, object] = {
        "schema_version": 1,
        "kind": "feedback_closed_before_forwarding",
        "feedback_receipt_id": str(row.id),
        "feedback_digest": row.feedback_digest,
        "binding_digest": _binding_digest(row),
        "reason": reason,
    }
    if row.primary_attempt_id is not None:
        application["primary_attempt_id"] = str(row.primary_attempt_id)
    row.state = "closed"
    row.closed_reason = reason
    row.application_digest = canonical_digest(application)


class PostgresSubscriptionFeedbackRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _has_unsettled_reservation(self, run_id: UUID) -> bool:
        return (
            await self._session.scalar(
                select(SubscriptionAttemptReservation.attempt_id)
                .outerjoin(
                    SubscriptionAttemptConsumption,
                    SubscriptionAttemptConsumption.attempt_id
                    == SubscriptionAttemptReservation.attempt_id,
                )
                .where(
                    SubscriptionAttemptReservation.run_id == run_id,
                    SubscriptionAttemptConsumption.attempt_id.is_(None),
                )
                .limit(1)
            )
            is not None
        )

    async def verify_receipt(
        self,
        stored: StoredTaskFeedback,
        *,
        actor_id: UUID,
        request_digest: str,
    ) -> None:
        receipt = stored.receipt
        row = await self._session.get(
            SubscriptionTaskFeedback, receipt.receipt_id, populate_existing=True
        )
        if (
            row is None
            or row.actor_id != actor_id
            or receipt.operator_id != actor_id
            or row.request_digest != request_digest
            or row.run_id != receipt.run_id
            or row.primary_task_id != receipt.primary_task_id
            or row.task_id != receipt.task_id
            or row.observed_run_version != receipt.run_version
            or row.observed_task_version != receipt.task_version
            or row.observed_primary_version != receipt.primary_task_version
            or receipt.status != "pending_primary"
            or row.feedback_digest != receipt.feedback_digest
            or _binding_digest(row) != receipt.binding_digest
            or row.feedback_bytes != receipt.feedback_bytes
            or _digest(row.feedback) != row.feedback_digest
        ):
            raise TaskFeedbackConflict("stored task feedback receipt differs")

    async def submit(
        self,
        run_id: UUID,
        task_id: UUID,
        request: SubscriptionTaskFeedbackRequest,
        *,
        actor_id: UUID,
        request_digest: str,
        receipt_id: UUID,
    ) -> FeedbackTransition:
        run = await self._session.get(Run, run_id, populate_existing=True)
        task = await self._session.get(
            SubscriptionTask, task_id, with_for_update=True, populate_existing=True
        )
        scheduled = await self._session.get(
            SubscriptionScheduledTask, task_id, with_for_update=True, populate_existing=True
        )
        if run is None or task is None or scheduled is None or task.run_id != run_id:
            raise TaskFeedbackConflict("feedback target is absent or foreign")
        if (
            run.version != request.expected_run_version
            or task.version != request.expected_task_version
        ):
            raise TaskFeedbackConflict("feedback observation is stale")
        if run.state in {
            RunState.CANCELLED.value,
            RunState.COMPLETED.value,
            RunState.FAILED.value,
        }:
            raise TaskFeedbackConflict("feedback run is settled")
        try:
            target = decode_subscription_record(task.payload)
        except TypeError, ValueError:
            raise TaskFeedbackConflict("feedback target contract differs") from None
        if (
            not isinstance(target, LogicalTaskContract)
            or target.run_id != run_id
            or target.task_id != task_id
            or target.parent_task_id is None
            or target.purpose is SpecialistPurpose.PRIMARY
            or scheduled.run_id != run_id
            or scheduled.parent_task_id != target.parent_task_id
            or task.parent_task_id != target.parent_task_id
            or task.cancel_requested
            or scheduled.cancel_requested
        ):
            raise TaskFeedbackConflict("feedback target is not an eligible worker")
        primary = await self._session.get(
            SubscriptionTask,
            target.parent_task_id,
            with_for_update=True,
            populate_existing=True,
        )
        primary_schedule = await self._session.get(
            SubscriptionScheduledTask,
            target.parent_task_id,
            with_for_update=True,
            populate_existing=True,
        )
        try:
            primary_contract = (
                None if primary is None else decode_subscription_record(primary.payload)
            )
        except TypeError, ValueError:
            primary_contract = None
        if (
            primary is None
            or primary_schedule is None
            or not isinstance(primary_contract, LogicalTaskContract)
            or primary.run_id != run_id
            or primary_schedule.run_id != run_id
            or primary_contract.run_id != run_id
            or primary_contract.task_id != primary.id
            or primary_contract.parent_task_id is not None
            or primary_contract.purpose is not SpecialistPurpose.PRIMARY
            or primary.cancel_requested
            or primary_schedule.cancel_requested
            or (
                primary.state == "terminal"
                and not (primary.pause_requested or primary_schedule.pause_requested)
            )
        ):
            raise TaskFeedbackConflict("feedback primary authority is unavailable")
        if await self._target_is_accepted(run_id, task_id):
            raise TaskFeedbackConflict("feedback target was already accepted")
        pending = await self._session.scalar(
            select(SubscriptionTaskFeedback.id)
            .where(
                SubscriptionTaskFeedback.run_id == run_id,
                SubscriptionTaskFeedback.primary_task_id == primary.id,
                SubscriptionTaskFeedback.state == "pending_primary",
            )
            .limit(1)
        )
        if pending is not None:
            raise TaskFeedbackConflict("primary already has pending feedback")
        count = await self._session.scalar(
            select(func.count())
            .select_from(SubscriptionTaskFeedback)
            .where(
                SubscriptionTaskFeedback.run_id == run_id,
                SubscriptionTaskFeedback.task_id == task_id,
            )
        )
        if int(count or 0) >= MAX_FEEDBACK_PER_TASK:
            raise TaskFeedbackConflict("feedback history reached its bound")
        envelope = await self._session.get(SubscriptionEnvelope, run_id)
        try:
            frozen_envelope = (
                None if envelope is None else decode_subscription_record(envelope.payload)
            )
        except TypeError, ValueError:
            frozen_envelope = None
        if (
            envelope is None
            or not isinstance(frozen_envelope, ExecutionEnvelope)
            or frozen_envelope.run_id != run_id
            or not frozen_envelope.permits_route(target.purpose, target.route)
            or not frozen_envelope.permits_route(primary_contract.purpose, primary_contract.route)
        ):
            raise TaskFeedbackConflict("feedback execution envelope is absent")
        primary_budget = await PostgresSubscriptionBudgetRepository(self._session).fit_reservation(
            run_id,
            primary.id,
            replace(primary_contract.budget, max_provider_attempts=1, max_repairs=0),
        )
        if primary_budget is None and not await self._has_unsettled_reservation(run_id):
            raise TaskFeedbackConflict("primary cumulative budget is exhausted")
        feedback_digest = _digest(request.feedback)
        feedback_bytes = len(request.feedback.encode("utf-8"))
        row = SubscriptionTaskFeedback(
            id=receipt_id,
            actor_id=actor_id,
            run_id=run_id,
            primary_task_id=primary.id,
            task_id=task_id,
            observed_run_version=run.version,
            observed_task_version=task.version,
            observed_primary_version=primary.version,
            observed_task_digest=canonical_digest(task.payload),
            observed_primary_digest=canonical_digest(primary.payload),
            envelope_digest=canonical_digest(envelope.payload),
            request_digest=request_digest,
            feedback=request.feedback,
            feedback_digest=feedback_digest,
            feedback_bytes=feedback_bytes,
        )
        self._session.add(row)
        if (
            primary.state == "blocked"
            and primary_schedule.state == "blocked"
            and not primary.pause_requested
            and not primary_schedule.pause_requested
        ):
            primary.state = primary_schedule.state = "queued"
            primary.version += 1
        await self._session.flush()
        return FeedbackTransition(
            primary_task_id=primary.id,
            status="pending_primary",
            run_version=run.version,
            task_version=task.version,
            primary_task_version=row.observed_primary_version,
            feedback_digest=feedback_digest,
            binding_digest=_binding_digest(row),
            feedback_bytes=feedback_bytes,
        )

    async def invocation_context(
        self, admission: SubscriptionAdmission
    ) -> FeedbackInvocationContext:
        if admission.task.purpose is SpecialistPurpose.PRIMARY:
            row = await self._session.scalar(
                select(SubscriptionTaskFeedback)
                .where(
                    SubscriptionTaskFeedback.run_id == admission.task.run_id,
                    SubscriptionTaskFeedback.primary_task_id == admission.task.task_id,
                    SubscriptionTaskFeedback.state == "pending_primary",
                )
                .order_by(SubscriptionTaskFeedback.created_at, SubscriptionTaskFeedback.id)
                .limit(1)
                .with_for_update()
            )
            if row is None:
                return FeedbackInvocationContext()
            await self._bind_primary(row, admission)
            return FeedbackInvocationContext(pending_primary=_context(row))

        rows = list(
            await self._session.scalars(
                select(SubscriptionTaskFeedback)
                .where(
                    SubscriptionTaskFeedback.run_id == admission.task.run_id,
                    SubscriptionTaskFeedback.task_id == admission.task.task_id,
                    SubscriptionTaskFeedback.state.in_(("forwarded", "delivered")),
                )
                .order_by(SubscriptionTaskFeedback.created_at, SubscriptionTaskFeedback.id)
                .limit(MAX_FEEDBACK_PER_TASK + 1)
                .with_for_update()
            )
        )
        if len(rows) > MAX_FEEDBACK_PER_TASK:
            raise TaskFeedbackConflict("feedback invocation context exceeds its bound")
        for row in rows:
            if row.state == "forwarded":
                await self._bind_worker(row, admission)
        return FeedbackInvocationContext(worker_feedback=tuple(_context(row) for row in rows))

    async def _bind_primary(
        self, row: SubscriptionTaskFeedback, admission: SubscriptionAdmission
    ) -> None:
        if row.primary_attempt_id == admission.attempt.attempt_id:
            return
        if row.primary_attempt_id is not None:
            previous = await self._session.get(SubscriptionAttempt, row.primary_attempt_id)
            if previous is None or previous.status != "terminal":
                raise TaskFeedbackConflict("feedback primary attempt is still active")
        row.primary_attempt_id = admission.attempt.attempt_id
        await self._session.flush()

    async def _bind_worker(
        self, row: SubscriptionTaskFeedback, admission: SubscriptionAdmission
    ) -> None:
        if row.delivery_attempt_id == admission.attempt.attempt_id:
            return
        if row.delivery_attempt_id is not None:
            previous = await self._session.get(SubscriptionAttempt, row.delivery_attempt_id)
            if previous is None or previous.status != "terminal":
                raise TaskFeedbackConflict("feedback delivery attempt is still active")
        row.delivery_attempt_id = admission.attempt.attempt_id
        await self._session.flush()

    async def apply_forward(
        self,
        *,
        attempt_id: UUID,
        primary: LogicalTaskContract,
        decision: ForwardFeedbackDecision,
        result_digest: str,
    ) -> tuple[dict[str, object], str]:
        row = await self._session.get(
            SubscriptionTaskFeedback,
            decision.feedback_receipt_id,
            with_for_update=True,
            populate_existing=True,
        )
        envelope = await self._session.get(SubscriptionEnvelope, primary.run_id)
        try:
            frozen_envelope = (
                None if envelope is None else decode_subscription_record(envelope.payload)
            )
        except TypeError, ValueError:
            frozen_envelope = None
        if (
            row is None
            or row.state != "pending_primary"
            or row.run_id != primary.run_id
            or row.primary_task_id != primary.task_id
            or row.task_id != decision.task_id
            or row.primary_attempt_id != attempt_id
            or row.feedback_digest != decision.feedback_digest
            or _digest(row.feedback) != row.feedback_digest
            or row.observed_primary_digest != canonical_digest(encode_subscription_record(primary))
            or envelope is None
            or not isinstance(frozen_envelope, ExecutionEnvelope)
            or frozen_envelope.run_id != primary.run_id
            or not frozen_envelope.permits_route(primary.purpose, primary.route)
            or row.envelope_digest != canonical_digest(envelope.payload)
        ):
            raise TaskFeedbackConflict("feedback forwarding source differs")
        target_row = await self._session.get(
            SubscriptionTask, row.task_id, with_for_update=True, populate_existing=True
        )
        scheduled = await self._session.get(
            SubscriptionScheduledTask,
            row.task_id,
            with_for_update=True,
            populate_existing=True,
        )
        try:
            target = None if target_row is None else decode_subscription_record(target_row.payload)
        except TypeError, ValueError:
            target = None
        if (
            target_row is None
            or scheduled is None
            or not isinstance(target, LogicalTaskContract)
            or target.run_id != primary.run_id
            or target.task_id != row.task_id
            or target.parent_task_id != primary.task_id
            or target_row.run_id != primary.run_id
            or target_row.parent_task_id != primary.task_id
            or scheduled.run_id != primary.run_id
            or scheduled.parent_task_id != primary.task_id
            or not frozen_envelope.permits_route(target.purpose, target.route)
        ):
            raise TaskFeedbackConflict("feedback target authority differs")

        delivery = "retained"
        accepted = await self._target_is_accepted(primary.run_id, target.task_id)
        cancelled = target_row.cancel_requested or scheduled.cancel_requested
        settled = (
            target_row.state == "terminal"
            and scheduled.state == "terminal"
            and not target_row.pause_requested
            and not scheduled.pause_requested
        )
        active = target_row.state == "running" or scheduled.state in {
            "leased",
            "reconciling",
        }
        remaining_budget = None
        if not (accepted or cancelled or active):
            remaining_budget = await PostgresSubscriptionBudgetRepository(
                self._session
            ).fit_reservation(
                primary.run_id,
                target.task_id,
                replace(target.budget, max_provider_attempts=1, max_repairs=0),
            )
        capacity_pending = (
            not (accepted or cancelled or active)
            and remaining_budget is None
            and await self._has_unsettled_reservation(primary.run_id)
        )
        closed_reason = None
        if accepted:
            closed_reason = "accepted"
        elif cancelled:
            closed_reason = "cancelled"
        elif not active and remaining_budget is None and not capacity_pending:
            closed_reason = "budget_exhausted"

        primary_budget = None
        if closed_reason is not None:
            primary_budget = await PostgresSubscriptionBudgetRepository(
                self._session
            ).fit_reservation(
                primary.run_id,
                primary.task_id,
                replace(primary.budget, max_provider_attempts=1, max_repairs=0),
            )
            row.state = "closed"
            row.closed_reason = closed_reason
            delivery = closed_reason
        else:
            row.state = "forwarded"
            if settled:
                target_row.state = scheduled.state = "queued"
                target_row.version += 1
                delivery = "queued"
            elif target_row.pause_requested or scheduled.pause_requested:
                delivery = "paused"
            elif target_row.state == "running" or scheduled.state in {"leased", "reconciling"}:
                delivery = "after_current_attempt"
        receipt: dict[str, object] = {
            "schema_version": 1,
            "kind": "feedback_forwarded",
            "result_digest": result_digest,
            "feedback_receipt_id": str(row.id),
            "feedback_digest": row.feedback_digest,
            "binding_digest": _binding_digest(row),
            "target_task_id": str(row.task_id),
            "observed_run_version": row.observed_run_version,
            "observed_task_version": row.observed_task_version,
            "delivery": delivery,
        }
        row.application_digest = canonical_digest(receipt)
        scope_pending = await PostgresSchedulingRepository(self._session).has_scope_request(
            primary.run_id,
            tuple(
                await self._session.scalars(
                    select(SubscriptionTask.id).where(
                        SubscriptionTask.run_id == primary.run_id,
                        SubscriptionTask.parent_task_id == primary.task_id,
                    )
                )
            ),
        )
        await self._session.flush()
        return receipt, (
            "queued"
            if (row.state == "closed" and primary_budget is not None) or scope_pending
            else "blocked"
        )

    async def verify_forward_replay(
        self,
        *,
        attempt_id: UUID,
        decision: ForwardFeedbackDecision,
        application: dict[str, object] | None,
    ) -> None:
        row = await self._session.get(
            SubscriptionTaskFeedback, decision.feedback_receipt_id, populate_existing=True
        )
        if (
            row is None
            or row.state not in {"forwarded", "delivered", "closed"}
            or row.primary_attempt_id != attempt_id
            or row.run_id != decision.run_id
            or row.task_id != decision.task_id
            or row.feedback_digest != decision.feedback_digest
            or application is None
            or row.application_digest != canonical_digest(application)
            or application.get("feedback_receipt_id") != str(row.id)
            or application.get("feedback_digest") != row.feedback_digest
            or application.get("binding_digest") != _binding_digest(row)
            or application.get("target_task_id") != str(row.task_id)
        ):
            raise TaskFeedbackConflict("feedback forwarding replay differs")

    async def settle_delivery(self, attempt_id: UUID) -> None:
        rows = list(
            await self._session.scalars(
                select(SubscriptionTaskFeedback)
                .where(
                    SubscriptionTaskFeedback.delivery_attempt_id == attempt_id,
                    SubscriptionTaskFeedback.state == "forwarded",
                )
                .with_for_update()
            )
        )
        now = datetime.now(UTC)
        for row in rows:
            row.state = "delivered"
            row.delivered_at = now
        if rows:
            await self._session.flush()

    async def close_cancelled(self, run_id: UUID, task_id: UUID) -> int:
        task = await self._session.get(
            SubscriptionTask, task_id, with_for_update=True, populate_existing=True
        )
        scheduled = await self._session.get(
            SubscriptionScheduledTask, task_id, with_for_update=True, populate_existing=True
        )
        if (
            task is None
            or scheduled is None
            or task.run_id != run_id
            or scheduled.run_id != run_id
            or not task.cancel_requested
            or not scheduled.cancel_requested
        ):
            raise TaskFeedbackConflict("feedback cancellation authority differs")
        rows = list(
            await self._session.scalars(
                select(SubscriptionTaskFeedback)
                .where(
                    SubscriptionTaskFeedback.run_id == run_id,
                    SubscriptionTaskFeedback.task_id == task_id,
                    SubscriptionTaskFeedback.state == "forwarded",
                )
                .order_by(SubscriptionTaskFeedback.created_at, SubscriptionTaskFeedback.id)
                .limit(MAX_FEEDBACK_PER_TASK + 1)
                .with_for_update()
            )
        )
        if len(rows) > MAX_FEEDBACK_PER_TASK:
            raise TaskFeedbackConflict("feedback cancellation history exceeds its bound")
        closed = 0
        for row in rows:
            if row.delivery_attempt_id is not None:
                delivery = await self._session.get(SubscriptionAttempt, row.delivery_attempt_id)
                if delivery is None or delivery.status != "terminal":
                    continue
            row.delivery_attempt_id = None
            row.state = "closed"
            row.closed_reason = "cancelled"
            closed += 1
        if closed:
            await self._session.flush()
        return closed

    async def close_run_cancelled(self, run_id: UUID) -> int:
        run = await self._session.get(Run, run_id, with_for_update=True, populate_existing=True)
        if run is None or run.state != RunState.CANCELLED.value:
            return 0
        rows = list(
            await self._session.scalars(
                select(SubscriptionTaskFeedback)
                .where(
                    SubscriptionTaskFeedback.run_id == run_id,
                    SubscriptionTaskFeedback.state.in_(("pending_primary", "forwarded")),
                )
                .order_by(SubscriptionTaskFeedback.created_at, SubscriptionTaskFeedback.id)
                .with_for_update()
            )
        )
        closed = 0
        for row in rows:
            if row.state == "forwarded" and row.delivery_attempt_id is not None:
                delivery = await self._session.get(
                    SubscriptionAttempt,
                    row.delivery_attempt_id,
                    with_for_update=True,
                    populate_existing=True,
                )
                if delivery is None:
                    raise TaskFeedbackConflict("feedback delivery attempt is absent")
                if delivery.status != "terminal":
                    result = await self._session.get(
                        SubscriptionAttemptResult,
                        delivery.id,
                        with_for_update=True,
                        populate_existing=True,
                    )
                    launched = await self._session.scalar(
                        select(SubscriptionClientLaunch.id)
                        .where(SubscriptionClientLaunch.attempt_id == delivery.id)
                        .limit(1)
                    )
                    if result is None or result.disposition != "stale" or launched is not None:
                        continue
                row.delivery_attempt_id = None
            if row.state == "pending_primary":
                _close_before_forwarding(row, "cancelled")
            else:
                row.state = "closed"
                row.closed_reason = "cancelled"
            closed += 1
        if closed:
            await self._session.flush()
        return closed

    async def close_exhausted(self, run_id: UUID) -> int:
        run = await self._session.get(Run, run_id, with_for_update=True, populate_existing=True)
        if run is None or run.state == RunState.CANCELLED.value:
            return 0
        if await self._has_unsettled_reservation(run_id):
            return 0
        rows = list(
            await self._session.scalars(
                select(SubscriptionTaskFeedback)
                .where(
                    SubscriptionTaskFeedback.run_id == run_id,
                    SubscriptionTaskFeedback.state.in_(("pending_primary", "forwarded")),
                )
                .order_by(SubscriptionTaskFeedback.created_at, SubscriptionTaskFeedback.id)
                .with_for_update()
            )
        )
        grouped: dict[tuple[bool, UUID], list[SubscriptionTaskFeedback]] = {}
        for row in rows:
            primary = row.state == "pending_primary"
            if primary and row.primary_attempt_id is not None:
                prior = await self._session.get(
                    SubscriptionAttempt,
                    row.primary_attempt_id,
                    with_for_update=True,
                    populate_existing=True,
                )
                if (
                    prior is None
                    or prior.run_id != run_id
                    or prior.task_row_id != row.primary_task_id
                ):
                    raise TaskFeedbackConflict("feedback primary attempt is absent")
                if prior.status != "terminal":
                    # A live primary attempt still owns forwarding or failure recovery.
                    continue
            if not primary and row.delivery_attempt_id is not None:
                delivery = await self._session.get(
                    SubscriptionAttempt,
                    row.delivery_attempt_id,
                    with_for_update=True,
                    populate_existing=True,
                )
                if delivery is None:
                    raise TaskFeedbackConflict("feedback delivery attempt is absent")
                if delivery.status != "terminal":
                    continue
            task_id = row.primary_task_id if primary else row.task_id
            grouped.setdefault((primary, task_id), []).append(row)

        closed = 0
        budget = PostgresSubscriptionBudgetRepository(self._session)
        for (primary, task_id), candidates in grouped.items():
            task = await self._session.get(
                SubscriptionTask, task_id, with_for_update=True, populate_existing=True
            )
            scheduled = await self._session.get(
                SubscriptionScheduledTask,
                task_id,
                with_for_update=True,
                populate_existing=True,
            )
            try:
                contract = None if task is None else decode_subscription_record(task.payload)
            except TypeError, ValueError:
                contract = None
            if (
                task is None
                or scheduled is None
                or not isinstance(contract, LogicalTaskContract)
                or task.run_id != run_id
                or scheduled.run_id != run_id
                or contract.run_id != run_id
                or contract.task_id != task_id
                or (contract.parent_task_id is None) != primary
                or (contract.purpose is SpecialistPurpose.PRIMARY) != primary
            ):
                raise TaskFeedbackConflict("feedback budget authority differs")
            if task.state == "running" or scheduled.state in {"leased", "reconciling"}:
                continue
            remaining = await budget.fit_reservation(
                run_id,
                task_id,
                replace(contract.budget, max_provider_attempts=1, max_repairs=0),
            )
            if remaining is not None:
                continue
            for row in candidates:
                if primary:
                    _close_before_forwarding(row, "budget_exhausted")
                else:
                    row.delivery_attempt_id = None
                    row.state = "closed"
                    row.closed_reason = "budget_exhausted"
                closed += 1
        if closed:
            await self._session.flush()
        return closed

    async def has_pending_primary(self, run_id: UUID, primary_task_id: UUID) -> bool:
        return (
            await self._session.scalar(
                select(SubscriptionTaskFeedback.id)
                .where(
                    SubscriptionTaskFeedback.run_id == run_id,
                    SubscriptionTaskFeedback.primary_task_id == primary_task_id,
                    SubscriptionTaskFeedback.state == "pending_primary",
                )
                .limit(1)
            )
            is not None
        )

    async def requeue_failed_primary(
        self, run_id: UUID, primary_task_id: UUID, attempt_id: UUID
    ) -> bool:
        pending = await self._session.scalar(
            select(SubscriptionTaskFeedback)
            .where(
                SubscriptionTaskFeedback.run_id == run_id,
                SubscriptionTaskFeedback.primary_task_id == primary_task_id,
                SubscriptionTaskFeedback.state == "pending_primary",
            )
            .limit(1)
            .with_for_update()
        )
        if pending is None:
            return False
        task = await self._session.get(
            SubscriptionTask, primary_task_id, with_for_update=True, populate_existing=True
        )
        scheduled = await self._session.get(
            SubscriptionScheduledTask,
            primary_task_id,
            with_for_update=True,
            populate_existing=True,
        )
        try:
            contract = None if task is None else decode_subscription_record(task.payload)
        except TypeError, ValueError:
            contract = None
        if (
            task is None
            or scheduled is None
            or not isinstance(contract, LogicalTaskContract)
            or contract.run_id != run_id
            or contract.task_id != primary_task_id
            or contract.parent_task_id is not None
            or contract.purpose is not SpecialistPurpose.PRIMARY
            or task.run_id != run_id
            or scheduled.run_id != run_id
            or task.state != "terminal"
            or scheduled.state != "terminal"
            or task.pause_requested
            or scheduled.pause_requested
            or task.cancel_requested
            or scheduled.cancel_requested
        ):
            return False
        remaining = await PostgresSubscriptionBudgetRepository(self._session).fit_reservation(
            run_id,
            primary_task_id,
            replace(contract.budget, max_provider_attempts=1, max_repairs=0),
        )
        capacity_pending = remaining is None and await self._has_unsettled_reservation(run_id)
        if remaining is None:
            if pending.primary_attempt_id not in (None, attempt_id):
                prior = await self._session.get(
                    SubscriptionAttempt,
                    pending.primary_attempt_id,
                    with_for_update=True,
                    populate_existing=True,
                )
                if (
                    prior is None
                    or prior.run_id != run_id
                    or prior.task_row_id != primary_task_id
                    or prior.status != "terminal"
                ):
                    return False
            attempt = await self._session.get(
                SubscriptionAttempt,
                attempt_id,
                with_for_update=True,
                populate_existing=True,
            )
            result = (
                None
                if attempt is None
                else await self._session.get(
                    SubscriptionAttemptResult,
                    attempt.id,
                    with_for_update=True,
                    populate_existing=True,
                )
            )
            if (
                attempt is None
                or result is None
                or attempt.run_id != run_id
                or attempt.task_row_id != primary_task_id
                or attempt.status != "terminal"
                or result.disposition not in {"failed", "handoff"}
            ):
                return False
            if not capacity_pending:
                application = {
                    "schema_version": 1,
                    "kind": "feedback_forwarding_closed",
                    "feedback_receipt_id": str(pending.id),
                    "feedback_digest": pending.feedback_digest,
                    "binding_digest": _binding_digest(pending),
                    "primary_attempt_id": str(attempt.id),
                    "primary_result_digest": result.result_digest,
                    "reason": "budget_exhausted",
                }
                pending.primary_attempt_id = attempt.id
                pending.state = "closed"
                pending.closed_reason = "budget_exhausted"
                pending.application_digest = canonical_digest(application)
                await self._session.flush()
                return False
        task.state = scheduled.state = "queued"
        task.version += 1
        await self._session.flush()
        return True

    async def requeue_undelivered(self, run_id: UUID, task_id: UUID) -> bool:
        rows = list(
            await self._session.scalars(
                select(SubscriptionTaskFeedback)
                .where(
                    SubscriptionTaskFeedback.run_id == run_id,
                    SubscriptionTaskFeedback.task_id == task_id,
                    SubscriptionTaskFeedback.state == "forwarded",
                )
                .order_by(SubscriptionTaskFeedback.created_at, SubscriptionTaskFeedback.id)
                .limit(MAX_FEEDBACK_PER_TASK + 1)
                .with_for_update()
            )
        )
        if not rows:
            return False
        if len(rows) > MAX_FEEDBACK_PER_TASK:
            raise TaskFeedbackConflict("feedback delivery history exceeds its bound")
        for row in rows:
            if row.delivery_attempt_id is not None:
                delivery = await self._session.get(SubscriptionAttempt, row.delivery_attempt_id)
                if delivery is None or delivery.status != "terminal":
                    return False
        task = await self._session.get(
            SubscriptionTask, task_id, with_for_update=True, populate_existing=True
        )
        scheduled = await self._session.get(
            SubscriptionScheduledTask, task_id, with_for_update=True, populate_existing=True
        )
        try:
            contract = None if task is None else decode_subscription_record(task.payload)
        except TypeError, ValueError:
            contract = None
        if (
            task is None
            or scheduled is None
            or not isinstance(contract, LogicalTaskContract)
            or contract.run_id != run_id
            or contract.task_id != task_id
            or contract.parent_task_id is None
            or contract.purpose is SpecialistPurpose.PRIMARY
            or task.run_id != run_id
            or scheduled.run_id != run_id
            or task.pause_requested
            or scheduled.pause_requested
            or task.state != "terminal"
            or scheduled.state != "terminal"
        ):
            return False
        cancelled = task.cancel_requested or scheduled.cancel_requested
        remaining = None
        if not cancelled:
            remaining = await PostgresSubscriptionBudgetRepository(self._session).fit_reservation(
                run_id,
                task_id,
                replace(contract.budget, max_provider_attempts=1, max_repairs=0),
            )
        capacity_pending = (
            not cancelled and remaining is None and await self._has_unsettled_reservation(run_id)
        )
        for row in rows:
            row.delivery_attempt_id = None
        if cancelled or (remaining is None and not capacity_pending):
            reason = "cancelled" if cancelled else "budget_exhausted"
            for row in rows:
                row.state = "closed"
                row.closed_reason = reason
            await self._session.flush()
            return False
        task.state = scheduled.state = "queued"
        task.version += 1
        await self._session.flush()
        return True

    async def _target_is_accepted(self, run_id: UUID, task_id: UUID) -> bool:
        # Version-one AcceptDecision has run_id then task_id in ordered fields.
        target = SubscriptionDecisionRecord.payload["record"]["fields"][1][1]["$uuid"].astext
        accepted = await self._session.scalar(
            select(SubscriptionDecisionRecord.id)
            .join(
                SubscriptionAttemptResult,
                SubscriptionAttemptResult.attempt_id == SubscriptionDecisionRecord.attempt_id,
            )
            .where(
                SubscriptionDecisionRecord.run_id == run_id,
                SubscriptionDecisionRecord.record_type == "AcceptDecision",
                target == str(task_id),
                SubscriptionAttemptResult.accepted.is_(True),
                SubscriptionAttemptResult.disposition == "task_accepted",
            )
            .order_by(SubscriptionDecisionRecord.created_at.desc())
            .limit(1)
        )
        return accepted is not None


__all__ = ["MAX_FEEDBACK_PER_TASK", "PostgresSubscriptionFeedbackRepository"]
