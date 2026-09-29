"""Current, actionable subscription recovery diagnostics for read projections."""

from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from forge.domain.run import RunState
from forge.persistence.models.run import Run
from forge.persistence.models.subscription import SubscriptionAttempt, SubscriptionTask
from forge.persistence.models.subscription_recovery import SubscriptionApplicationDiagnostic
from forge.persistence.models.subscription_results import SubscriptionAttemptResult


async def current_attention(
    session: AsyncSession, run_id: UUID
) -> dict[UUID, SubscriptionApplicationDiagnostic]:
    latest_attempt = aliased(SubscriptionAttempt)
    latest_number = (
        select(func.max(latest_attempt.attempt_number))
        .where(latest_attempt.task_row_id == SubscriptionApplicationDiagnostic.task_id)
        .correlate(SubscriptionApplicationDiagnostic)
        .scalar_subquery()
    )
    rows = (
        await session.scalars(
            select(SubscriptionApplicationDiagnostic)
            .join(SubscriptionAttempt, SubscriptionAttempt.id == SubscriptionApplicationDiagnostic.attempt_id)
            .join(SubscriptionAttemptResult, SubscriptionAttemptResult.attempt_id == SubscriptionAttempt.id)
            .join(SubscriptionTask, SubscriptionTask.id == SubscriptionApplicationDiagnostic.task_id)
            .join(Run, Run.id == SubscriptionApplicationDiagnostic.run_id)
            .where(
                SubscriptionApplicationDiagnostic.run_id == run_id,
                SubscriptionApplicationDiagnostic.resolution == "attention",
                or_(
                    SubscriptionAttemptResult.disposition.in_((
                        "decision_pending", "candidate_prepared", "acceptance_prepared", "role_rejected"
                    )),
                    SubscriptionApplicationDiagnostic.reason_code == "approved_plan_contract_stale",
                ),
                SubscriptionAttempt.attempt_number == latest_number,
                SubscriptionTask.cancel_requested.is_(False),
                Run.state.notin_((RunState.CANCELLED.value, RunState.FAILED.value, RunState.COMPLETED.value)),
            )
        )
    ).all()
    return {row.task_id: row for row in rows}
