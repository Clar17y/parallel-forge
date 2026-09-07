"""PostgreSQL integration tests for orphan tool call discovery."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from forge.application.ports.tools import ToolCallRecord
from forge.domain.tool import ToolCallStatus
from forge.persistence.models import (
    AgentExecution,
    ToolCall,
)
from forge.persistence.models import (
    OperationIntent as OperationIntentRecord,
)
from forge.persistence.repositories.runs import PersistenceDataError
from forge.persistence.repositories.tool_calls import PostgresToolCallRepository


def _make_tool_call(
    *,
    call_id: UUID | None = None,
    run_id: UUID,
    execution_id: UUID,
    status: str = "RUNNING",
    operation_intent_id: UUID | None = None,
    result_metadata: dict[str, Any] | None = None,
    result_metadata_schema_version: int | None = None,
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
    tool_name: str = "build.run_named_check",
    authorized: bool = True,
) -> ToolCall:
    now = datetime.now(UTC)
    meta: dict[str, Any] | None = None
    schema_version = result_metadata_schema_version
    if result_metadata is not None:
        meta = dict(result_metadata)
    elif operation_intent_id is not None:
        meta = {"operation_intent_id": str(operation_intent_id)}

    if meta is not None and schema_version is None:
        schema_version = 1
    elif meta is None and schema_version is None:
        schema_version = None

    return ToolCall(
        id=uuid4() if call_id is None else call_id,
        run_id=run_id,
        agent_execution_id=execution_id,
        tool_name=tool_name,
        arguments_schema_version=1,
        normalized_arguments={"command": "build"},
        authorized=authorized,
        status=status,
        result_metadata_schema_version=schema_version,
        result_metadata=meta,
        started_at=now if started_at is None else started_at,
        completed_at=completed_at,
    )


@pytest.mark.integration
async def test_list_running_with_operations_parameter_validation(
    session_factory: object,
) -> None:
    async with session_factory() as session:  # type: ignore[operator]
        repo = PostgresToolCallRepository(session)

        # limit validation: reject bool
        with pytest.raises((TypeError, ValueError)):
            await repo.list_running_with_operations(limit=True)

        with pytest.raises((TypeError, ValueError)):
            await repo.list_running_with_operations(limit=False)

        # limit validation: reject non-int
        with pytest.raises((TypeError, ValueError)):
            await repo.list_running_with_operations(limit="50")  # type: ignore[arg-type]

        with pytest.raises((TypeError, ValueError)):
            await repo.list_running_with_operations(limit=1.5)  # type: ignore[arg-type]

        # limit validation: out of bounds (< 1 or > 1000)
        with pytest.raises(ValueError):
            await repo.list_running_with_operations(limit=0)

        with pytest.raises(ValueError):
            await repo.list_running_with_operations(limit=-1)

        with pytest.raises(ValueError):
            await repo.list_running_with_operations(limit=1001)

        # after_id validation: reject non-UUID
        with pytest.raises((TypeError, ValueError)):
            await repo.list_running_with_operations(after_id="not-a-uuid")  # type: ignore[arg-type]

        with pytest.raises((TypeError, ValueError)):
            await repo.list_running_with_operations(after_id=123)  # type: ignore[arg-type]

        # after_id validation: reject nil UUID
        with pytest.raises(ValueError):
            await repo.list_running_with_operations(after_id=UUID(int=0))

        # Valid boundary limits return empty sequence without error on clean DB
        res_min = await repo.list_running_with_operations(limit=1)
        assert isinstance(res_min, tuple)
        assert len(res_min) == 0

        res_max = await repo.list_running_with_operations(limit=1000)
        assert isinstance(res_max, tuple)
        assert len(res_max) == 0

        res_after = await repo.list_running_with_operations(after_id=uuid4())
        assert isinstance(res_after, tuple)
        assert len(res_after) == 0


@pytest.mark.integration
async def test_list_running_with_operations_excludes_terminal_and_unlinked_calls(
    session_factory: object,
    persisted_run: Any,
) -> None:
    execution_id = uuid4()
    run_id = persisted_run.id
    now = datetime.now(UTC)

    linked_running_id = uuid4()
    intent_id = uuid4()

    async with session_factory() as session, session.begin():  # type: ignore[operator]
        session.add(
            AgentExecution(
                id=execution_id,
                run_id=run_id,
                role="planner",
                instruction_version="v1",
                provider="gemini",
                model="test",
                status="RUNNING",
            )
        )

        calls = [
            # 1. Matching: RUNNING with non-null operation_intent_id
            _make_tool_call(
                call_id=linked_running_id,
                run_id=run_id,
                execution_id=execution_id,
                status="RUNNING",
                operation_intent_id=intent_id,
            ),
            # 2. Excluded: SUCCEEDED with operation_intent_id
            _make_tool_call(
                run_id=run_id,
                execution_id=execution_id,
                status="SUCCEEDED",
                operation_intent_id=uuid4(),
                completed_at=now,
            ),
            # 3. Excluded: FAILED with operation_intent_id
            _make_tool_call(
                run_id=run_id,
                execution_id=execution_id,
                status="FAILED",
                operation_intent_id=uuid4(),
                completed_at=now,
            ),
            # 4. Excluded: DENIED with operation_intent_id
            _make_tool_call(
                run_id=run_id,
                execution_id=execution_id,
                status="DENIED",
                operation_intent_id=uuid4(),
                authorized=False,
                completed_at=now,
            ),
            # 5. Excluded: CANCELLED with operation_intent_id
            _make_tool_call(
                run_id=run_id,
                execution_id=execution_id,
                status="CANCELLED",
                operation_intent_id=uuid4(),
                completed_at=now,
            ),
            # 6. Excluded: PENDING with operation_intent_id
            _make_tool_call(
                run_id=run_id,
                execution_id=execution_id,
                status="PENDING",
                operation_intent_id=uuid4(),
            ),
            # 7. Excluded: RUNNING with no metadata
            _make_tool_call(
                run_id=run_id,
                execution_id=execution_id,
                status="RUNNING",
                result_metadata=None,
            ),
            # 8. Excluded: RUNNING with empty metadata
            _make_tool_call(
                run_id=run_id,
                execution_id=execution_id,
                status="RUNNING",
                result_metadata={},
                result_metadata_schema_version=1,
            ),
            # 9. Excluded: RUNNING with null operation_intent_id in metadata
            _make_tool_call(
                run_id=run_id,
                execution_id=execution_id,
                status="RUNNING",
                result_metadata={"operation_intent_id": None},
                result_metadata_schema_version=1,
            ),
            # 10. Excluded: RUNNING with other metadata but no operation_intent_id
            _make_tool_call(
                run_id=run_id,
                execution_id=execution_id,
                status="RUNNING",
                result_metadata={"step_id": str(uuid4())},
                result_metadata_schema_version=1,
            ),
        ]
        session.add_all(calls)

    async with session_factory() as session:  # type: ignore[operator]
        repo = PostgresToolCallRepository(session)
        records = await repo.list_running_with_operations()

        assert isinstance(records, tuple)
        assert len(records) == 1
        record = records[0]
        assert isinstance(record, ToolCallRecord)
        assert record.id == linked_running_id
        assert record.status == ToolCallStatus.RUNNING
        assert record.operation_intent_id == intent_id


@pytest.mark.integration
async def test_list_running_with_operations_includes_succeeded_linked_operation(
    session_factory: object,
    persisted_run: Any,
) -> None:
    execution_id = uuid4()
    run_id = persisted_run.id
    now = datetime.now(UTC)
    succeeded_intent_id = uuid4()
    call_id = uuid4()

    async with session_factory() as session, session.begin():  # type: ignore[operator]
        session.add(
            AgentExecution(
                id=execution_id,
                run_id=run_id,
                role="planner",
                instruction_version="v1",
                provider="gemini",
                model="test",
                status="RUNNING",
            )
        )
        session.add(
            OperationIntentRecord(
                id=succeeded_intent_id,
                run_id=run_id,
                operation_kind="build.run_named_check",
                idempotency_key=f"op:{uuid4().hex}",
                request_digest="a" * 64,
                request_payload={"cmd": "test"},
                request_schema_version=1,
                status="SUCCEEDED",
                outcome_schema_version=1,
                outcome_payload={"ok": True},
                completed_at=now,
            )
        )
        session.add(
            _make_tool_call(
                call_id=call_id,
                run_id=run_id,
                execution_id=execution_id,
                status="RUNNING",
                operation_intent_id=succeeded_intent_id,
            )
        )

    async with session_factory() as session:  # type: ignore[operator]
        repo = PostgresToolCallRepository(session)
        records = await repo.list_running_with_operations()

        assert isinstance(records, tuple)
        assert len(records) == 1
        assert records[0].id == call_id
        assert records[0].operation_intent_id == succeeded_intent_id


@pytest.mark.integration
async def test_list_running_with_operations_deterministic_keyset_pagination(
    session_factory: object,
    persisted_run: Any,
) -> None:
    execution_id = uuid4()
    run_id = persisted_run.id
    total_calls = 7

    call_ids = sorted([uuid4() for _ in range(total_calls)])

    async with session_factory() as session, session.begin():  # type: ignore[operator]
        session.add(
            AgentExecution(
                id=execution_id,
                run_id=run_id,
                role="planner",
                instruction_version="v1",
                provider="gemini",
                model="test",
                status="RUNNING",
            )
        )
        for cid in call_ids:
            session.add(
                _make_tool_call(
                    call_id=cid,
                    run_id=run_id,
                    execution_id=execution_id,
                    status="RUNNING",
                    operation_intent_id=uuid4(),
                )
            )

    async with session_factory() as session:  # type: ignore[operator]
        repo = PostgresToolCallRepository(session)

        collected: list[ToolCallRecord] = []
        after_id: UUID | None = None
        page_size = 2

        while True:
            page = await repo.list_running_with_operations(after_id=after_id, limit=page_size)
            assert isinstance(page, tuple)
            if not page:
                break
            # Verify each page is strictly ascending
            assert list(page) == sorted(page, key=lambda r: r.id)
            collected.extend(page)
            after_id = page[-1].id

        assert len(collected) == total_calls
        collected_ids = [r.id for r in collected]
        assert collected_ids == call_ids


@pytest.mark.integration
async def test_list_running_with_operations_fails_closed_on_malformed_durable_binding(
    session_factory: object,
    persisted_run: Any,
) -> None:
    execution_id = uuid4()
    run_id = persisted_run.id

    async with session_factory() as session, session.begin():  # type: ignore[operator]
        session.add(
            AgentExecution(
                id=execution_id,
                run_id=run_id,
                role="planner",
                instruction_version="v1",
                provider="gemini",
                model="test",
                status="RUNNING",
            )
        )
        session.add(
            _make_tool_call(
                run_id=run_id,
                execution_id=execution_id,
                status="RUNNING",
                result_metadata={"operation_intent_id": "malformed-not-a-uuid"},
                result_metadata_schema_version=1,
            )
        )

    async with session_factory() as session:  # type: ignore[operator]
        repo = PostgresToolCallRepository(session)
        with pytest.raises(PersistenceDataError):
            await repo.list_running_with_operations()
