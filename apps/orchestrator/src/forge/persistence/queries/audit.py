"""Source-aware audit reads; operation statuses are current, not historical."""

from uuid import UUID

from sqlalchemy import String, and_, case, cast, exists, false, literal, or_, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from forge.application.services.public_data import public_payload
from forge.persistence.models import OperationIntent, OperatorAuditEvent, Run, RunEvent, Task
from forge.persistence.repositories.runs import PersistenceDataError

_OPERATION_KEYS = (
    "operation_intent_id",
    "intent_id",
    "push_intent_id",
    "publication_intent_id",
    "merge_intent_id",
    "base_update_intent_id",
    "base_adoption_intent_id",
    "reviewed_push_intent_id",
)


def _project() -> ColumnElement[UUID | None]:
    return case(
        (OperatorAuditEvent.subject_type == "project", OperatorAuditEvent.subject_id),
        (
            OperatorAuditEvent.subject_type == "task",
            select(Task.project_id)
            .where(Task.id == OperatorAuditEvent.subject_id)
            .scalar_subquery(),
        ),
        (
            OperatorAuditEvent.subject_type == "run",
            select(Run.project_id).where(Run.id == OperatorAuditEvent.subject_id).scalar_subquery(),
        ),
        else_=None,
    )


def _related_operation() -> ColumnElement[bool]:
    return and_(
        OperationIntent.run_id == RunEvent.run_id,
        or_(
            *(
                cast(OperationIntent.id, String) == RunEvent.payload[key].astext
                for key in _OPERATION_KEYS
            )
        ),
    )


class AuditQuery:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    async def page(
        self,
        offset: int = 0,
        limit: int = 50,
        *,
        run_id: UUID | None = None,
        project_id: UUID | None = None,
        actor_id: UUID | None = None,
        operation_id: UUID | None = None,
        operation_status: str | None = None,
    ) -> tuple[list[dict[str, object]], bool]:
        operator = select(
            literal("operator").label("source"),
            OperatorAuditEvent.id.label("id"),
            OperatorAuditEvent.created_at.label("timestamp"),
        )
        causal = select(
            literal("run").label("source"),
            RunEvent.id.label("id"),
            RunEvent.occurred_at.label("timestamp"),
        ).join(Run, Run.id == RunEvent.run_id)
        if run_id is not None:
            operator = operator.where(
                or_(
                    and_(
                        OperatorAuditEvent.subject_type == "run",
                        OperatorAuditEvent.subject_id == run_id,
                    ),
                    and_(
                        OperatorAuditEvent.subject_type == "task",
                        exists(
                            select(Run.id).where(
                                Run.id == run_id, Run.task_id == OperatorAuditEvent.subject_id
                            )
                        ),
                    ),
                )
            )
            causal = causal.where(RunEvent.run_id == run_id)
        if project_id is not None:
            operator = operator.where(_project() == project_id)
            causal = causal.where(Run.project_id == project_id)
        if actor_id is not None:
            operator = operator.where(OperatorAuditEvent.actor_id == actor_id)
            causal = causal.where(RunEvent.actor_id == actor_id)
        if operation_id is not None or operation_status is not None:
            operator = operator.where(false())
            related = select(OperationIntent.id).where(_related_operation())
            if operation_id is not None:
                related = related.where(OperationIntent.id == operation_id)
            if operation_status is not None:
                related = related.where(OperationIntent.status == operation_status)
            causal = causal.where(exists(related))
        combined = union_all(operator, causal).subquery()
        async with self._factory() as session:
            await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            keys = (
                await session.execute(
                    select(combined.c.source, combined.c.id)
                    .order_by(
                        combined.c.timestamp.desc(),
                        combined.c.source,
                        combined.c.id,
                    )
                    .offset(offset)
                    .limit(limit + 1)
                )
            ).all()
            items = await self._load(session, [(row.source, row.id) for row in keys[:limit]])
            return items, len(keys) > limit

    async def detail(self, source: str, event_id: UUID) -> dict[str, object] | None:
        async with self._factory() as session:
            await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            items = await self._load(session, [(source, event_id)], include_operations=False)
            if not items:
                return None
            return {key: value for key, value in items[0].items() if key != "operations"}

    @staticmethod
    async def _load(session: AsyncSession, keys: list[tuple[str, UUID]], *, include_operations: bool = True) -> list[dict[str, object]]:
        operator_ids = [event_id for source, event_id in keys if source == "operator"]
        event_ids = [event_id for source, event_id in keys if source == "run"]
        items: dict[tuple[str, UUID], dict[str, object]] = {}
        for record, project_id in (
            await session.execute(
                select(OperatorAuditEvent, _project()).where(
                    OperatorAuditEvent.id.in_(operator_ids)
                )
            )
        ).all():
            if record.schema_version != 1:
                raise PersistenceDataError("unsupported operator audit schema")
            items["operator", record.id] = {
                "id": record.id,
                "source": "operator",
                "actor_id": record.actor_id,
                "actor_class": "operator",
                "event_type": record.event_type,
                "subject_type": record.subject_type,
                "subject_id": record.subject_id,
                "run_id": record.subject_id if record.subject_type == "run" else None,
                "project_id": project_id,
                "created_at": record.created_at,
                "payload": public_payload(record.payload),
                "operations": [],
            }
        events = (
            await session.execute(
                select(RunEvent, Run.project_id)
                .join(Run, Run.id == RunEvent.run_id)
                .where(RunEvent.id.in_(event_ids))
            )
        ).all()
        references = {
            value
            for record, _ in events
            for key in _OPERATION_KEYS
            if isinstance(value := record.payload.get(key), str)
        }
        operations = (
            list(
                await session.scalars(
                    select(OperationIntent).where(cast(OperationIntent.id, String).in_(references))
                )
            )
            if references and include_operations
            else []
        )
        for record, project_id in events:
            if record.payload_schema_version != 1:
                raise PersistenceDataError("unsupported run audit schema")
            refs = {
                record.payload.get(key)
                for key in _OPERATION_KEYS
                if isinstance(record.payload.get(key), str)
            }
            linked = sorted(
                (op for op in operations if op.run_id == record.run_id and str(op.id) in refs),
                key=lambda op: str(op.id),
            )
            items["run", record.id] = {
                "id": record.id,
                "source": "run",
                "actor_id": record.actor_id,
                "actor_class": record.actor_class,
                "event_type": record.event_type,
                "subject_type": "run",
                "subject_id": record.run_id,
                "run_id": record.run_id,
                "project_id": project_id,
                "created_at": record.occurred_at,
                "payload": public_payload(record.payload),
                "operations": [
                    {"id": op.id, "kind": op.operation_kind, "status": op.status} for op in linked
                ],
            }
        return [items[key] for key in keys if key in items]
