"""Shared durable predicates for isolating unresolved startup work."""

from uuid import UUID

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.orm import InstrumentedAttribute, aliased
from sqlalchemy.sql.elements import ColumnElement

from forge.persistence.models import AgentExecution, OperationIntent, RunEvent, Step, ToolCall


def unresolved_work(run_id: UUID | InstrumentedAttribute[UUID]) -> ColumnElement[bool]:
    return or_(
        exists(
            select(OperationIntent.id).where(
                OperationIntent.run_id == run_id,
                OperationIntent.status.in_(("PENDING", "NEEDS_RECONCILIATION")),
            )
        ),
        exists(
            select(AgentExecution.id).where(
                AgentExecution.run_id == run_id,
                AgentExecution.status == "RUNNING",
            )
        ),
        exists(select(Step.id).where(Step.run_id == run_id, Step.status == "RUNNING")),
        exists(select(ToolCall.id).where(ToolCall.run_id == run_id, ToolCall.status == "RUNNING")),
    )


def startup_intervention_hold(run_id: UUID) -> ColumnElement[bool]:
    # Owner recovery settles the entire recorded interruption before resuming.
    # Later ordinary work must not revive its historical startup marker, while
    # a newer interruption still fences commands regardless of business state.
    recovery = aliased(RunEvent)
    recovered_later = exists(
        select(recovery.id).where(
            recovery.run_id == RunEvent.run_id,
            recovery.event_type == "run.interrupted_check_recovered",
            recovery.sequence > RunEvent.sequence,
        )
    ).correlate(RunEvent)
    return and_(
        unresolved_work(run_id),
        exists(
            select(RunEvent.id).where(
                RunEvent.run_id == run_id,
                RunEvent.event_type == "run.recovery_intervention",
                ~recovered_later,
            )
        ),
    )
