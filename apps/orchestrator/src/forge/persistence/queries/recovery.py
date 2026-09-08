"""Shared durable predicates for isolating unresolved startup work."""

from uuid import UUID

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.orm import InstrumentedAttribute
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
    return and_(
        unresolved_work(run_id),
        exists(
            select(RunEvent.id).where(
                RunEvent.run_id == run_id,
                RunEvent.event_type == "run.recovery_intervention",
            )
        ),
    )
