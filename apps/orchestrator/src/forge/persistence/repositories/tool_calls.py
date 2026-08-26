"""PostgreSQL persistence for audited controlled-tool invocations."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from forge.application.ports.tools import ToolCallRecord
from forge.domain.actor import AgentRole
from forge.domain.operation import canonical_payload
from forge.domain.tool import ToolCallStatus, ToolName
from forge.observability.redaction import Redactor
from forge.persistence.models import AgentExecution, Step, ToolCall
from forge.persistence.repositories.runs import PersistenceDataError


class ToolCallRepositoryError(RuntimeError):
    """A controlled-tool evidence record could not be persisted safely."""


class ToolCallNotFound(ToolCallRepositoryError):
    """No tool-call evidence exists for the requested identifier."""


class ToolCallConflict(ToolCallRepositoryError):
    """A tool-call identifier was reused for different immutable evidence."""


_TERMINAL_STATUSES = frozenset(
    {
        ToolCallStatus.SUCCEEDED,
        ToolCallStatus.FAILED,
        ToolCallStatus.DENIED,
        ToolCallStatus.CANCELLED,
    }
)


class PostgresToolCallRepository:
    """Persist one immutable tool-call projection on the caller's session."""

    def __init__(self, session: AsyncSession, *, redactor: Redactor | None = None) -> None:
        self._session = session
        self._redactor = redactor or Redactor()

    async def reserve(self, record: ToolCallRecord) -> ToolCallRecord:
        """Reserve one authorized running call or replay the exact reservation."""

        safe_record = _prepare_reservation(record, self._redactor)
        try:
            inserted = await self._session.execute(
                insert(ToolCall)
                .values(**_row_values(safe_record))
                .on_conflict_do_nothing(index_elements=[ToolCall.id])
                .returning(ToolCall.id)
            )
            inserted_id = inserted.scalar_one_or_none()
            if inserted_id is not None:
                stored = await self._session.get(ToolCall, inserted_id)
                if stored is None:
                    raise ToolCallRepositoryError("tool call reservation disappeared")
                return _record_from_row(stored)

            existing = (
                await self._session.execute(
                    select(ToolCall).where(ToolCall.id == safe_record.id).with_for_update()
                )
            ).scalar_one_or_none()
            if existing is None:
                raise ToolCallRepositoryError("tool call reservation could not be loaded")
            loaded = _record_from_row(existing)
            if _record_json(loaded) != _record_json(safe_record):
                raise ToolCallConflict("tool call reservation was reused for different evidence")
            return loaded
        except ToolCallConflict:
            raise
        except Exception:  # noqa: BLE001 - public boundary must hide adapter/driver details
            raise ToolCallRepositoryError("tool call reservation failed") from None

    async def finalize(self, record: ToolCallRecord) -> ToolCallRecord:
        """Finalize one running call, preserving its immutable admission evidence."""

        safe_record = _prepare_finalization(record, self._redactor)
        try:
            existing = (
                await self._session.execute(
                    select(ToolCall).where(ToolCall.id == safe_record.id).with_for_update()
                )
            ).scalar_one_or_none()
            if existing is None:
                raise ToolCallNotFound("tool call evidence was not found")
            loaded = _record_from_row(existing)

            if loaded.status is not ToolCallStatus.RUNNING:
                if loaded.status in _TERMINAL_STATUSES and _record_json(loaded) == _record_json(
                    safe_record
                ):
                    return loaded
                raise ToolCallConflict("tool call lifecycle state cannot be rewritten")
            if _immutable_record_json(loaded) != _immutable_record_json(safe_record):
                raise ToolCallConflict("tool call immutable evidence cannot be changed")

            existing.status = safe_record.status.value.upper()
            existing.result_metadata_schema_version = safe_record.result_metadata_schema_version
            existing.result_metadata = (
                None if safe_record.result_metadata is None else dict(safe_record.result_metadata)
            )
            existing.completed_at = safe_record.completed_at
            await self._session.flush()
            return _record_from_row(existing)
        except ToolCallConflict, ToolCallNotFound:
            raise
        except Exception:  # noqa: BLE001 - public boundary must hide adapter/driver details
            raise ToolCallRepositoryError("tool call finalization failed") from None

    async def record(self, record: ToolCallRecord) -> ToolCallRecord:
        """Insert evidence or return the exact existing record on replay."""

        if not isinstance(record, ToolCallRecord):
            raise TypeError("tool call repository requires a ToolCallRecord")
        safe_record = _redact_record(record, self._redactor)
        existing = (
            await self._session.execute(
                select(ToolCall).where(ToolCall.id == safe_record.id).with_for_update()
            )
        ).scalar_one_or_none()
        if existing is None:
            self._session.add(
                ToolCall(
                    id=safe_record.id,
                    run_id=safe_record.run_id,
                    agent_execution_id=safe_record.agent_execution_id,
                    tool_name=safe_record.tool_name.value,
                    arguments_schema_version=safe_record.arguments_schema_version,
                    normalized_arguments=dict(safe_record.normalized_arguments),
                    authorized=safe_record.authorized,
                    status=safe_record.status.value.upper(),
                    result_metadata_schema_version=safe_record.result_metadata_schema_version,
                    result_metadata=(
                        None
                        if safe_record.result_metadata is None
                        else dict(safe_record.result_metadata)
                    ),
                    started_at=safe_record.started_at,
                    completed_at=safe_record.completed_at,
                )
            )
            await self._session.flush()
            return safe_record

        loaded = _record_from_row(existing)
        if _record_json(loaded) != _record_json(safe_record):
            raise ToolCallConflict("tool call identifier was reused for different evidence")
        return loaded

    async def create(self, record: ToolCallRecord) -> ToolCallRecord:
        """Descriptive alias for ``record`` used by repository callers."""

        return await self.record(record)

    async def append(self, record: ToolCallRecord) -> ToolCallRecord:
        """Append-compatible alias retained for append-only persistence callers."""

        return await self.record(record)

    async def get(self, tool_call_id: UUID) -> ToolCallRecord:
        if not isinstance(tool_call_id, UUID):
            raise TypeError("tool call identifier must be a UUID")
        row = await self._session.get(ToolCall, tool_call_id)
        if row is None:
            raise ToolCallNotFound("tool call evidence was not found")
        return _record_from_row(row)

    async def list_for_run(self, run_id: UUID) -> Sequence[ToolCallRecord]:
        if not isinstance(run_id, UUID):
            raise TypeError("tool call run identifier must be a UUID")
        rows = (
            (
                await self._session.execute(
                    select(ToolCall)
                    .where(ToolCall.run_id == run_id)
                    .order_by(ToolCall.started_at, ToolCall.id)
                )
            )
            .scalars()
            .all()
        )
        return tuple(_record_from_row(row) for row in rows)

    async def count_for_execution(self, agent_execution_id: UUID) -> int:
        if not isinstance(agent_execution_id, UUID):
            raise TypeError("tool call agent execution identifier must be a UUID")
        count = await self._session.scalar(
            select(func.count())
            .select_from(ToolCall)
            .where(ToolCall.agent_execution_id == agent_execution_id)
        )
        if not isinstance(count, int):
            raise PersistenceDataError("tool call count is malformed")
        return count

    async def validate_execution_context(
        self,
        run_id: UUID,
        agent_execution_id: UUID,
        step_id: UUID,
        role: AgentRole,
    ) -> bool:
        """Prove the execution and step belong to the same locked run context."""

        for value, name in (
            (run_id, "run"),
            (agent_execution_id, "agent execution"),
            (step_id, "step"),
        ):
            if not isinstance(value, UUID) or value.int == 0:
                raise TypeError(f"tool call {name} identifier must be a UUID")
        if not isinstance(role, AgentRole):
            raise TypeError("tool call execution role must be an AgentRole")
        execution_id = (
            await self._session.execute(
                select(AgentExecution.id)
                .join(Step, Step.id == AgentExecution.step_id)
                .where(
                    AgentExecution.id == agent_execution_id,
                    AgentExecution.run_id == run_id,
                    AgentExecution.step_id == step_id,
                    AgentExecution.role == role.value,
                    AgentExecution.status == "RUNNING",
                    Step.run_id == run_id,
                )
            )
        ).scalar_one_or_none()
        return execution_id == agent_execution_id


def _redact_record(record: ToolCallRecord, redactor: Redactor) -> ToolCallRecord:
    arguments = redactor.redact(record.normalized_arguments)
    raw_metadata = dict(record.result_metadata or {})
    lineage = {
        "step_id": None if record.step_id is None else str(record.step_id),
        "role": None if record.role is None else record.role.value,
        "policy_version": record.policy_version,
        "artifact_digests": list(record.artifact_digests),
        "correlation_id": None if record.correlation_id is None else str(record.correlation_id),
        "operation_intent_id": (
            None if record.operation_intent_id is None else str(record.operation_intent_id)
        ),
    }
    for key, expected in lineage.items():
        if key in raw_metadata:
            if not _same_lineage_value(raw_metadata[key], expected):
                raise ToolCallRepositoryError("tool call evidence lineage is inconsistent")
        elif expected is not None and (key != "artifact_digests" or expected):
            raw_metadata[key] = expected
    if record.duration_ms is not None:
        raw_metadata.setdefault("duration_ms", record.duration_ms)
    metadata = (
        None
        if record.result_metadata is None and not raw_metadata
        else redactor.redact(raw_metadata)
    )
    if not isinstance(arguments, Mapping) or (
        metadata is not None and not isinstance(metadata, Mapping)
    ):
        raise ToolCallRepositoryError("tool call evidence must contain object payloads")
    try:
        safe_record = ToolCallRecord(
            id=record.id,
            run_id=record.run_id,
            agent_execution_id=record.agent_execution_id,
            tool_name=record.tool_name,
            normalized_arguments=arguments,
            authorized=record.authorized,
            status=record.status,
            started_at=record.started_at,
            completed_at=record.completed_at,
            result_metadata=metadata,
            step_id=record.step_id,
            role=record.role,
            policy_version=record.policy_version,
            duration_ms=record.duration_ms,
            artifact_digests=record.artifact_digests,
            correlation_id=record.correlation_id,
            operation_intent_id=record.operation_intent_id,
            arguments_schema_version=record.arguments_schema_version,
            result_metadata_schema_version=(
                record.result_metadata_schema_version
                if record.result_metadata_schema_version is not None
                else (1 if metadata is not None else None)
            ),
        )
        for key, expected in lineage.items():
            if expected is not None and (key != "artifact_digests" or expected):
                actual = (
                    None
                    if safe_record.result_metadata is None
                    else safe_record.result_metadata.get(key)
                )
                if not _same_lineage_value(actual, expected):
                    raise ToolCallRepositoryError("tool call evidence lineage is inconsistent")
        return safe_record
    except (TypeError, ValueError) as error:
        raise ToolCallRepositoryError("tool call evidence is malformed") from error


def _prepare_reservation(record: ToolCallRecord, redactor: Redactor) -> ToolCallRecord:
    if not isinstance(record, ToolCallRecord):
        raise ToolCallRepositoryError("tool call reservation evidence is invalid")
    if record.status is not ToolCallStatus.RUNNING or not record.authorized:
        raise ToolCallConflict("tool call reservation requires an authorized running call")
    if (
        record.completed_at is not None
        or record.result_metadata is not None
        or record.result_metadata_schema_version is not None
        or record.duration_ms is not None
        or record.artifact_digests
    ):
        raise ToolCallConflict("tool call reservation cannot contain terminal evidence")
    safe_record = _prepare_lifecycle_record(record, redactor)
    if safe_record is None:
        raise ToolCallRepositoryError("tool call reservation evidence is invalid")
    return safe_record


def _prepare_finalization(record: ToolCallRecord, redactor: Redactor) -> ToolCallRecord:
    safe_record = _prepare_lifecycle_record(record, redactor)
    if safe_record is None:
        raise ToolCallRepositoryError("tool call finalization evidence is invalid")
    if safe_record.status not in _TERMINAL_STATUSES:
        raise ToolCallConflict("tool call finalization requires a terminal status")
    return safe_record


def _prepare_lifecycle_record(
    record: ToolCallRecord,
    redactor: Redactor,
) -> ToolCallRecord | None:
    prepared: ToolCallRecord | None = None
    try:
        if not isinstance(record, ToolCallRecord):
            return None
        prepared = _redact_record(record, redactor)
    except Exception:  # noqa: BLE001 - lifecycle validation must not expose input details
        prepared = None
    if prepared is None:
        raise ToolCallRepositoryError("tool call lifecycle evidence is invalid")
    return prepared


def _same_lineage_value(actual: object, expected: object) -> bool:
    if expected == []:
        return actual in (None, [], ())
    if isinstance(expected, list):
        return isinstance(actual, (list, tuple)) and tuple(actual) == tuple(expected)
    return actual == expected


def _row_values(record: ToolCallRecord) -> dict[str, object]:
    return {
        "id": record.id,
        "run_id": record.run_id,
        "agent_execution_id": record.agent_execution_id,
        "tool_name": record.tool_name.value,
        "arguments_schema_version": record.arguments_schema_version,
        "normalized_arguments": dict(record.normalized_arguments),
        "authorized": record.authorized,
        "status": record.status.value.upper(),
        "result_metadata_schema_version": record.result_metadata_schema_version,
        "result_metadata": (
            None if record.result_metadata is None else dict(record.result_metadata)
        ),
        "started_at": record.started_at,
        "completed_at": record.completed_at,
    }


def _immutable_record_json(record: ToolCallRecord) -> str:
    payload: dict[str, object] = {
        "id": str(record.id),
        "run_id": str(record.run_id),
        "agent_execution_id": str(record.agent_execution_id),
        "tool_name": record.tool_name.value,
        "normalized_arguments": dict(record.normalized_arguments),
        "authorized": record.authorized,
        "started_at": record.started_at.isoformat(),
        "step_id": None if record.step_id is None else str(record.step_id),
        "role": None if record.role is None else record.role.value,
        "policy_version": record.policy_version,
        "correlation_id": None if record.correlation_id is None else str(record.correlation_id),
        "operation_intent_id": (
            None if record.operation_intent_id is None else str(record.operation_intent_id)
        ),
        "arguments_schema_version": record.arguments_schema_version,
    }
    return canonical_payload(payload)


def _record_from_row(row: ToolCall) -> ToolCallRecord:
    try:
        status = ToolCallStatus(row.status.casefold())
        tool_name = ToolName(row.tool_name)
        normalized = row.normalized_arguments
        metadata = row.result_metadata
        if not isinstance(normalized, Mapping):
            raise PersistenceDataError("stored tool call arguments are not an object")
        if metadata is not None and not isinstance(metadata, Mapping):
            raise PersistenceDataError("stored tool call metadata is not an object")
        values = dict(metadata or {})
        step_id = _uuid_value(values.get("step_id"), "step_id")
        correlation_id = _uuid_value(values.get("correlation_id"), "correlation_id")
        operation_intent_id = _uuid_value(values.get("operation_intent_id"), "operation_intent_id")
        role = _role_value(values.get("role"))
        policy_version = _int_value(values.get("policy_version"), "policy_version")
        duration_ms = _int_value(values.get("duration_ms"), "duration_ms")
        artifact_values = values.get("artifact_digests", ())
        if not isinstance(artifact_values, (list, tuple)):
            raise PersistenceDataError("stored tool call artifact digests are malformed")
        artifact_digests = tuple(artifact_values)
        result_metadata = values
        result_schema = row.result_metadata_schema_version
        if result_schema is not None and not values and metadata is None:
            result_schema = None
        return ToolCallRecord(
            id=row.id,
            run_id=row.run_id,
            agent_execution_id=row.agent_execution_id,
            tool_name=tool_name,
            normalized_arguments=normalized,
            authorized=row.authorized,
            status=status,
            started_at=row.started_at,
            completed_at=row.completed_at,
            result_metadata=result_metadata if metadata is not None else None,
            step_id=step_id,
            role=role,
            policy_version=policy_version,
            duration_ms=duration_ms,
            artifact_digests=artifact_digests,
            correlation_id=correlation_id,
            operation_intent_id=operation_intent_id,
            arguments_schema_version=row.arguments_schema_version,
            result_metadata_schema_version=result_schema,
        )
    except PersistenceDataError:
        raise
    except (AttributeError, TypeError, ValueError) as error:
        raise PersistenceDataError("stored tool call evidence is malformed") from error


def _uuid_value(value: object, field_name: str) -> UUID | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise PersistenceDataError(f"stored tool call {field_name} is malformed")
    try:
        parsed = UUID(value)
    except ValueError as error:
        raise PersistenceDataError(f"stored tool call {field_name} is malformed") from error
    if parsed.int == 0:
        raise PersistenceDataError(f"stored tool call {field_name} is malformed")
    return parsed


def _role_value(value: object) -> AgentRole | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise PersistenceDataError("stored tool call role is malformed")
    try:
        return AgentRole(value)
    except ValueError as error:
        raise PersistenceDataError("stored tool call role is malformed") from error


def _int_value(value: object, field_name: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise PersistenceDataError(f"stored tool call {field_name} is malformed")
    return value


def _record_json(record: ToolCallRecord) -> str:
    payload: dict[str, object] = {
        "id": str(record.id),
        "run_id": str(record.run_id),
        "agent_execution_id": str(record.agent_execution_id),
        "tool_name": record.tool_name.value,
        "normalized_arguments": dict(record.normalized_arguments),
        "authorized": record.authorized,
        "status": record.status.value,
        "started_at": record.started_at.isoformat(),
        "completed_at": None if record.completed_at is None else record.completed_at.isoformat(),
        "result_metadata": None if record.result_metadata is None else dict(record.result_metadata),
        "step_id": None if record.step_id is None else str(record.step_id),
        "role": None if record.role is None else record.role.value,
        "policy_version": record.policy_version,
        "duration_ms": record.duration_ms,
        "artifact_digests": list(record.artifact_digests),
        "correlation_id": None if record.correlation_id is None else str(record.correlation_id),
        "operation_intent_id": (
            None if record.operation_intent_id is None else str(record.operation_intent_id)
        ),
        "arguments_schema_version": record.arguments_schema_version,
        "result_metadata_schema_version": record.result_metadata_schema_version,
    }
    return canonical_payload(payload)


__all__ = [
    "PostgresToolCallRepository",
    "ToolCallConflict",
    "ToolCallNotFound",
    "ToolCallRepositoryError",
]
