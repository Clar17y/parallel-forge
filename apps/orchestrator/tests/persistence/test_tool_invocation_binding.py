"""PostgreSQL integration tests for tool invocation identity binding persistence."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from forge.application.ports.tools import ToolCallRecord
from forge.domain.tool import ToolCallStatus, ToolName
from forge.observability.redaction import Redactor
from forge.persistence.models import AgentExecution, ToolCall
from forge.persistence.repositories.runs import PersistenceDataError
from forge.persistence.repositories.tool_calls import (
    PostgresToolCallRepository,
    ToolCallConflict,
    ToolCallNotFound,
    ToolCallRepositoryError,
)
from sqlalchemy import select


def _sample_record(
    *,
    record_id: UUID | None = None,
    run_id: UUID | None = None,
    agent_execution_id: UUID | None = None,
    status: ToolCallStatus = ToolCallStatus.RUNNING,
    authorized: bool = True,
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
    result_metadata: Mapping[str, object] | None = None,
    result_metadata_schema_version: int | None = None,
    request_digest: str | None = None,
    resource_id: str | None = None,
    invocation_schema_version: int | None = None,
    normalized_arguments: Mapping[str, object] | None = None,
) -> ToolCallRecord:
    now = datetime.now(UTC)
    return ToolCallRecord(
        id=uuid4() if record_id is None else record_id,
        run_id=uuid4() if run_id is None else run_id,
        agent_execution_id=uuid4() if agent_execution_id is None else agent_execution_id,
        tool_name=ToolName.BUILD_RUN_NAMED_CHECK,
        normalized_arguments={"command": "build"}
        if normalized_arguments is None
        else normalized_arguments,
        authorized=authorized,
        status=status,
        started_at=now if started_at is None else started_at,
        completed_at=completed_at,
        result_metadata=result_metadata,
        result_metadata_schema_version=result_metadata_schema_version,
        request_digest=request_digest,
        resource_id=resource_id,
        invocation_schema_version=invocation_schema_version,
    )


def test_tool_call_record_binding_validation_partial_and_invalid() -> None:
    valid_digest = "a" * 64
    valid_resource = "repo://clar17y/forge/main"

    # All 3 absent is valid legacy shape
    legacy = _sample_record(
        request_digest=None,
        resource_id=None,
        invocation_schema_version=None,
    )
    assert legacy.request_digest is None
    assert legacy.resource_id is None
    assert legacy.invocation_schema_version is None

    # All 3 present is valid
    bound = _sample_record(
        request_digest=valid_digest,
        resource_id=valid_resource,
        invocation_schema_version=1,
    )
    assert bound.request_digest == valid_digest
    assert bound.resource_id == valid_resource
    assert bound.invocation_schema_version == 1

    # Partial combinations must be rejected
    with pytest.raises(ValueError, match="binding"):
        _sample_record(request_digest=valid_digest, resource_id=None, invocation_schema_version=1)
    with pytest.raises(ValueError, match="binding"):
        _sample_record(request_digest=None, resource_id=valid_resource, invocation_schema_version=1)
    with pytest.raises(ValueError, match="binding"):
        _sample_record(
            request_digest=valid_digest, resource_id=valid_resource, invocation_schema_version=None
        )
    with pytest.raises(ValueError, match="binding"):
        _sample_record(
            request_digest=valid_digest, resource_id=None, invocation_schema_version=None
        )
    with pytest.raises(ValueError, match="binding"):
        _sample_record(
            request_digest=None, resource_id=valid_resource, invocation_schema_version=None
        )
    with pytest.raises(ValueError, match="binding"):
        _sample_record(request_digest=None, resource_id=None, invocation_schema_version=1)

    # Invocation schema version must be strict int 1
    with pytest.raises(ValueError, match="schema"):
        _sample_record(
            request_digest=valid_digest, resource_id=valid_resource, invocation_schema_version=2
        )
    with pytest.raises(ValueError, match="schema"):
        _sample_record(
            request_digest=valid_digest, resource_id=valid_resource, invocation_schema_version=0
        )
    with pytest.raises((TypeError, ValueError)):
        _sample_record(
            request_digest=valid_digest, resource_id=valid_resource, invocation_schema_version="1"
        )  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="schema"):
        _sample_record(
            request_digest=valid_digest, resource_id=valid_resource, invocation_schema_version=True
        )  # type: ignore[arg-type]

    # Digest must be lowercase 64 hex
    with pytest.raises(ValueError, match="digest"):
        _sample_record(
            request_digest="A" * 64, resource_id=valid_resource, invocation_schema_version=1
        )
    with pytest.raises(ValueError, match="digest"):
        _sample_record(
            request_digest="g" * 64, resource_id=valid_resource, invocation_schema_version=1
        )
    with pytest.raises(ValueError, match="digest"):
        _sample_record(
            request_digest="a" * 63, resource_id=valid_resource, invocation_schema_version=1
        )
    with pytest.raises(ValueError, match="digest"):
        _sample_record(
            request_digest="a" * 65, resource_id=valid_resource, invocation_schema_version=1
        )
    with pytest.raises((TypeError, ValueError)):
        _sample_record(request_digest=123, resource_id=valid_resource, invocation_schema_version=1)  # type: ignore[arg-type]

    # Resource must be nonblank, at most 512 chars, without control chars
    with pytest.raises(ValueError, match="resource"):
        _sample_record(request_digest=valid_digest, resource_id="", invocation_schema_version=1)
    with pytest.raises(ValueError, match="resource"):
        _sample_record(request_digest=valid_digest, resource_id="   ", invocation_schema_version=1)
    with pytest.raises(ValueError, match="resource"):
        _sample_record(
            request_digest=valid_digest, resource_id="x" * 513, invocation_schema_version=1
        )
    for ctrl in ("\x00", "\r", "\n", "\x1f", "\x7f"):
        with pytest.raises(ValueError, match="control"):
            _sample_record(
                request_digest=valid_digest, resource_id=f"res{ctrl}id", invocation_schema_version=1
            )


def test_tool_call_record_metadata_lineage_conflict_validation() -> None:
    valid_digest = "b" * 64
    valid_resource = "repo://clar17y/forge/dev"
    now = datetime.now(UTC)

    # Legacy record cannot carry invocation binding in result_metadata
    with pytest.raises(ValueError, match="result metadata"):
        _sample_record(
            status=ToolCallStatus.SUCCEEDED,
            completed_at=now,
            result_metadata={"request_digest": valid_digest},
            result_metadata_schema_version=1,
            request_digest=None,
            resource_id=None,
            invocation_schema_version=None,
        )

    # Bound record cannot have conflicting metadata fields
    with pytest.raises(ValueError, match="conflict"):
        _sample_record(
            status=ToolCallStatus.SUCCEEDED,
            completed_at=now,
            result_metadata={
                "request_digest": "c" * 64,
                "resource_id": valid_resource,
                "invocation_schema_version": 1,
            },
            result_metadata_schema_version=1,
            request_digest=valid_digest,
            resource_id=valid_resource,
            invocation_schema_version=1,
        )

    with pytest.raises(ValueError, match="conflict"):
        _sample_record(
            status=ToolCallStatus.SUCCEEDED,
            completed_at=now,
            result_metadata={
                "request_digest": valid_digest,
                "resource_id": "other-resource",
                "invocation_schema_version": 1,
            },
            result_metadata_schema_version=1,
            request_digest=valid_digest,
            resource_id=valid_resource,
            invocation_schema_version=1,
        )

    # Bound record cannot have partial binding fields in metadata
    with pytest.raises(ValueError, match="partial"):
        _sample_record(
            status=ToolCallStatus.SUCCEEDED,
            completed_at=now,
            result_metadata={"request_digest": valid_digest},
            result_metadata_schema_version=1,
            request_digest=valid_digest,
            resource_id=valid_resource,
            invocation_schema_version=1,
        )

    # Bound record with matching metadata fields succeeds
    matching = _sample_record(
        status=ToolCallStatus.SUCCEEDED,
        completed_at=now,
        result_metadata={
            "output": "ok",
            "request_digest": valid_digest,
            "resource_id": valid_resource,
            "invocation_schema_version": 1,
        },
        result_metadata_schema_version=1,
        request_digest=valid_digest,
        resource_id=valid_resource,
        invocation_schema_version=1,
    )
    assert matching.result_metadata is not None
    assert matching.result_metadata["request_digest"] == valid_digest


@pytest.mark.parametrize("schema_value", [True, 1.0])
def test_tool_call_record_rejects_type_alias_invocation_schema_metadata(
    schema_value: object,
) -> None:
    """The durable decoder requires an exact int, so construction must too."""

    now = datetime.now(UTC)
    with pytest.raises(ValueError, match="conflict"):
        _sample_record(
            status=ToolCallStatus.SUCCEEDED,
            completed_at=now,
            result_metadata={
                "request_digest": "d" * 64,
                "resource_id": "repo://clar17y/forge/type-alias",
                "invocation_schema_version": schema_value,
            },
            result_metadata_schema_version=1,
            request_digest="d" * 64,
            resource_id="repo://clar17y/forge/type-alias",
            invocation_schema_version=1,
        )


@pytest.mark.integration
async def test_exact_new_binding_reserve_finalize_roundtrip(
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

    call_id = uuid4()
    valid_digest = "e" * 64
    valid_resource = "repo://clar17y/forge/run-exact"

    initial_record = _sample_record(
        record_id=call_id,
        run_id=run_id,
        agent_execution_id=execution_id,
        status=ToolCallStatus.RUNNING,
        authorized=True,
        request_digest=valid_digest,
        resource_id=valid_resource,
        invocation_schema_version=1,
    )

    async with session_factory() as session:  # type: ignore[operator]
        repo = PostgresToolCallRepository(session)
        reserved = await repo.reserve(initial_record)
        assert reserved.id == call_id
        assert reserved.request_digest == valid_digest
        assert reserved.resource_id == valid_resource
        assert reserved.invocation_schema_version == 1
        assert reserved.status is ToolCallStatus.RUNNING

        # find returns exact reserved record
        found_running = await repo.find(call_id)
        assert found_running is not None
        assert found_running.id == call_id
        assert found_running.request_digest == valid_digest
        assert found_running.resource_id == valid_resource
        assert found_running.invocation_schema_version == 1

        # finalize call
        now = datetime.now(UTC)
        terminal_record = _sample_record(
            record_id=call_id,
            run_id=run_id,
            agent_execution_id=execution_id,
            status=ToolCallStatus.SUCCEEDED,
            authorized=True,
            started_at=reserved.started_at,
            completed_at=now,
            result_metadata={"exit_code": 0, "output_preview": "done"},
            result_metadata_schema_version=1,
            request_digest=valid_digest,
            resource_id=valid_resource,
            invocation_schema_version=1,
        )
        finalized = await repo.finalize(terminal_record)
        assert finalized.status is ToolCallStatus.SUCCEEDED
        assert finalized.request_digest == valid_digest
        assert finalized.resource_id == valid_resource
        assert finalized.invocation_schema_version == 1
        assert finalized.result_metadata is not None
        assert finalized.result_metadata["exit_code"] == 0

        # get, find, and list_for_run roundtrip
        got = await repo.get(call_id)
        assert got.id == call_id
        assert got.request_digest == valid_digest
        assert got.resource_id == valid_resource
        assert got.invocation_schema_version == 1
        assert got.status is ToolCallStatus.SUCCEEDED

        found = await repo.find(call_id)
        assert found is not None
        assert found.id == call_id
        assert found.request_digest == valid_digest
        assert found.resource_id == valid_resource
        assert found.invocation_schema_version == 1

        for_run = await repo.list_for_run(run_id)
        matching = [r for r in for_run if r.id == call_id]
        assert len(matching) == 1
        assert matching[0].request_digest == valid_digest
        assert matching[0].resource_id == valid_resource
        assert matching[0].invocation_schema_version == 1


@pytest.mark.integration
async def test_legacy_rows_without_fields(
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

    # 1. Legacy reservation and finalization through repository
    call_id = uuid4()
    legacy_running = _sample_record(
        record_id=call_id,
        run_id=run_id,
        agent_execution_id=execution_id,
        status=ToolCallStatus.RUNNING,
        authorized=True,
    )
    async with session_factory() as session:  # type: ignore[operator]
        repo = PostgresToolCallRepository(session)
        reserved = await repo.reserve(legacy_running)
        assert reserved.request_digest is None
        assert reserved.resource_id is None
        assert reserved.invocation_schema_version is None

        legacy_terminal = _sample_record(
            record_id=call_id,
            run_id=run_id,
            agent_execution_id=execution_id,
            status=ToolCallStatus.SUCCEEDED,
            authorized=True,
            started_at=reserved.started_at,
            completed_at=datetime.now(UTC),
            result_metadata={"legacy": "value"},
            result_metadata_schema_version=1,
        )
        finalized = await repo.finalize(legacy_terminal)
        assert finalized.request_digest is None
        assert finalized.resource_id is None
        assert finalized.invocation_schema_version is None

        found = await repo.find(call_id)
        assert found is not None
        assert found.request_digest is None
        assert found.resource_id is None
        assert found.invocation_schema_version is None

    # 2. Raw database row inserted without any lineage binding fields in result_metadata
    raw_call_id = uuid4()
    now = datetime.now(UTC)
    async with session_factory() as session, session.begin():  # type: ignore[operator]
        session.add(
            ToolCall(
                id=raw_call_id,
                run_id=run_id,
                agent_execution_id=execution_id,
                tool_name="build.run_named_check",
                arguments_schema_version=1,
                normalized_arguments={"command": "test"},
                authorized=True,
                status="SUCCEEDED",
                result_metadata_schema_version=1,
                result_metadata={"stdout": "legacy row output"},
                started_at=now,
                completed_at=now,
            )
        )

    async with session_factory() as session:  # type: ignore[operator]
        repo = PostgresToolCallRepository(session)
        loaded = await repo.get(raw_call_id)
        assert loaded.request_digest is None
        assert loaded.resource_id is None
        assert loaded.invocation_schema_version is None
        assert loaded.result_metadata is not None
        assert loaded.result_metadata["stdout"] == "legacy row output"


@pytest.mark.integration
async def test_request_digest_conflict_despite_identical_redacted_arguments(
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

    call_id = uuid4()
    args = {"target": "all", "command": "run"}
    rec1 = _sample_record(
        record_id=call_id,
        run_id=run_id,
        agent_execution_id=execution_id,
        request_digest="1" * 64,
        resource_id="repo://clar17y/forge/conflict",
        invocation_schema_version=1,
        normalized_arguments=args,
    )
    rec2_different_digest = _sample_record(
        record_id=call_id,
        run_id=run_id,
        agent_execution_id=execution_id,
        request_digest="2" * 64,
        resource_id="repo://clar17y/forge/conflict",
        invocation_schema_version=1,
        normalized_arguments=args,
        started_at=rec1.started_at,
    )

    async with session_factory() as session:  # type: ignore[operator]
        repo = PostgresToolCallRepository(session)
        await repo.reserve(rec1)

        # Attempt to reserve same ID with different request digest
        with pytest.raises(ToolCallConflict, match="reused for different evidence"):
            await repo.reserve(rec2_different_digest)


@pytest.mark.integration
async def test_resource_mismatch_and_schema_mutation_denial(
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

    call_id = uuid4()
    reserved_rec = _sample_record(
        record_id=call_id,
        run_id=run_id,
        agent_execution_id=execution_id,
        request_digest="3" * 64,
        resource_id="repo://clar17y/forge/resource-a",
        invocation_schema_version=1,
    )

    async with session_factory() as session:  # type: ignore[operator]
        repo = PostgresToolCallRepository(session)
        reserved = await repo.reserve(reserved_rec)

        now = datetime.now(UTC)
        # Attempt to finalize with changed resource_id
        tampered_resource = _sample_record(
            record_id=call_id,
            run_id=run_id,
            agent_execution_id=execution_id,
            status=ToolCallStatus.SUCCEEDED,
            authorized=True,
            started_at=reserved.started_at,
            completed_at=now,
            result_metadata={"done": True},
            result_metadata_schema_version=1,
            request_digest="3" * 64,
            resource_id="repo://clar17y/forge/resource-MUTATED",
            invocation_schema_version=1,
        )
        with pytest.raises(ToolCallConflict, match="immutable evidence cannot be changed"):
            await repo.finalize(tampered_resource)

        # Attempt to finalize with changed request_digest
        tampered_digest = _sample_record(
            record_id=call_id,
            run_id=run_id,
            agent_execution_id=execution_id,
            status=ToolCallStatus.SUCCEEDED,
            authorized=True,
            started_at=reserved.started_at,
            completed_at=now,
            result_metadata={"done": True},
            result_metadata_schema_version=1,
            request_digest="4" * 64,
            resource_id="repo://clar17y/forge/resource-a",
            invocation_schema_version=1,
        )
        with pytest.raises(ToolCallConflict, match="immutable evidence cannot be changed"):
            await repo.finalize(tampered_digest)


@pytest.mark.integration
async def test_find_absent_and_existing(
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

    async with session_factory() as session:  # type: ignore[operator]
        repo = PostgresToolCallRepository(session)

        # Missing ID returns None on find, raises ToolCallNotFound on get
        missing_id = uuid4()
        assert await repo.find(missing_id) is None
        with pytest.raises(ToolCallNotFound):
            await repo.get(missing_id)

        # Existing record found on both
        call_id = uuid4()
        rec = _sample_record(
            record_id=call_id,
            run_id=run_id,
            agent_execution_id=execution_id,
            request_digest="5" * 64,
            resource_id="repo://clar17y/forge/find-test",
            invocation_schema_version=1,
        )
        await repo.reserve(rec)

        found = await repo.find(call_id)
        assert found is not None
        assert found.id == call_id

        got = await repo.get(call_id)
        assert got.id == call_id


@pytest.mark.integration
async def test_immutable_terminal_replay(
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

    call_id = uuid4()
    rec = _sample_record(
        record_id=call_id,
        run_id=run_id,
        agent_execution_id=execution_id,
        request_digest="6" * 64,
        resource_id="repo://clar17y/forge/replay-test",
        invocation_schema_version=1,
    )

    async with session_factory() as session:  # type: ignore[operator]
        repo = PostgresToolCallRepository(session)
        reserved = await repo.reserve(rec)

        now = datetime.now(UTC)
        final_record = _sample_record(
            record_id=call_id,
            run_id=run_id,
            agent_execution_id=execution_id,
            status=ToolCallStatus.SUCCEEDED,
            authorized=True,
            started_at=reserved.started_at,
            completed_at=now,
            result_metadata={"result": "exact"},
            result_metadata_schema_version=1,
            request_digest="6" * 64,
            resource_id="repo://clar17y/forge/replay-test",
            invocation_schema_version=1,
        )

        finalized = await repo.finalize(final_record)
        assert finalized.status is ToolCallStatus.SUCCEEDED

        # Replaying identical finalization succeeds
        replayed = await repo.finalize(final_record)
        assert replayed.status is ToolCallStatus.SUCCEEDED
        assert replayed.id == call_id

        # Attempting different terminal outcome fails
        different_outcome = _sample_record(
            record_id=call_id,
            run_id=run_id,
            agent_execution_id=execution_id,
            status=ToolCallStatus.FAILED,
            authorized=True,
            started_at=reserved.started_at,
            completed_at=now,
            result_metadata={"result": "different"},
            result_metadata_schema_version=1,
            request_digest="6" * 64,
            resource_id="repo://clar17y/forge/replay-test",
            invocation_schema_version=1,
        )
        with pytest.raises(ToolCallConflict, match="lifecycle state cannot be rewritten"):
            await repo.finalize(different_outcome)


@pytest.mark.integration
async def test_no_raw_canary_in_stored_evidence_and_redaction_failure_safety(
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

    call_id = uuid4()
    canary_token = "canary_identifier_9999"
    rec = _sample_record(
        record_id=call_id,
        run_id=run_id,
        agent_execution_id=execution_id,
        request_digest="7" * 64,
        resource_id="repo://clar17y/forge/safe-resource",
        invocation_schema_version=1,
        normalized_arguments={"command": f"deploy --id={canary_token}"},
    )

    async with session_factory() as session, session.begin():  # type: ignore[operator]
        repo = PostgresToolCallRepository(session, redactor=Redactor(secrets=[canary_token]))
        await repo.reserve(rec)

    # Query raw database row directly using a fresh session
    async with session_factory() as session:  # type: ignore[operator]
        raw_row = (
            await session.execute(select(ToolCall).where(ToolCall.id == call_id))
        ).scalar_one()
        stored_args = raw_row.normalized_arguments
        assert canary_token not in str(stored_args)
        assert "[REDACTED]" in str(stored_args)
        # Lineage in result_metadata preserves digest and resource without altering them
        stored_meta = raw_row.result_metadata
        assert stored_meta["request_digest"] == "7" * 64
        assert stored_meta["resource_id"] == "repo://clar17y/forge/safe-resource"

    # Redaction may never silently alter resource_id / request_digest:
    # If a redactor altered resource_id, the repository fails closed.
    class _TamperingRedactor(Redactor):
        def redact(self, payload: object) -> object:
            redacted = super().redact(payload)
            if isinstance(redacted, dict) and "resource_id" in redacted:
                redacted = dict(redacted)
                redacted["resource_id"] = "MUTATED_BY_REDACTOR"
            return redacted

    async with session_factory() as session:  # type: ignore[operator]
        tampering_repo = PostgresToolCallRepository(session, redactor=_TamperingRedactor())
        tamper_call_id = uuid4()
        tamper_rec = _sample_record(
            record_id=tamper_call_id,
            run_id=run_id,
            agent_execution_id=execution_id,
            request_digest="8" * 64,
            resource_id="repo://clar17y/forge/resource-to-tamper",
            invocation_schema_version=1,
        )
        with pytest.raises(ToolCallRepositoryError):
            await tampering_repo.reserve(tamper_rec)


@pytest.mark.integration
@pytest.mark.parametrize("schema_value", [True, 1.0])
async def test_redactor_type_alias_schema_mutation_is_rejected_without_row(
    session_factory: object,
    persisted_run: Any,
    schema_value: object,
) -> None:
    """A redactor cannot turn strict schema metadata into an equality alias."""

    execution_id = uuid4()
    async with session_factory() as session, session.begin():  # type: ignore[operator]
        session.add(
            AgentExecution(
                id=execution_id,
                run_id=persisted_run.id,
                role="planner",
                instruction_version="v1",
                provider="gemini",
                model="test",
                status="RUNNING",
            )
        )

    class _SchemaAliasRedactor(Redactor):
        def redact(self, payload: object) -> object:
            redacted = super().redact(payload)
            if isinstance(redacted, dict) and "invocation_schema_version" in redacted:
                redacted = dict(redacted)
                redacted["invocation_schema_version"] = schema_value
            return redacted

    call_id = uuid4()
    record = _sample_record(
        record_id=call_id,
        run_id=persisted_run.id,
        agent_execution_id=execution_id,
        request_digest="e" * 64,
        resource_id="repo://clar17y/forge/redactor-type-alias",
        invocation_schema_version=1,
    )
    async with session_factory() as session:  # type: ignore[operator]
        repo = PostgresToolCallRepository(session, redactor=_SchemaAliasRedactor())
        with pytest.raises(ToolCallRepositoryError):
            await repo.reserve(record)
        assert (
            await session.execute(select(ToolCall.id).where(ToolCall.id == call_id))
        ).scalar_one_or_none() is None


@pytest.mark.integration
async def test_malformed_persisted_records_fail_closed(
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

    now = datetime.now(UTC)

    async def _insert_malformed(meta: dict[str, object]) -> UUID:
        row_id = uuid4()
        async with session_factory() as session, session.begin():  # type: ignore[operator]
            session.add(
                ToolCall(
                    id=row_id,
                    run_id=run_id,
                    agent_execution_id=execution_id,
                    tool_name="build.run_named_check",
                    arguments_schema_version=1,
                    normalized_arguments={"command": "check"},
                    authorized=True,
                    status="RUNNING",
                    result_metadata_schema_version=1,
                    result_metadata=meta,
                    started_at=now,
                )
            )
        return row_id

    # 1. Partial binding in DB: only request_digest
    partial_id1 = await _insert_malformed({"request_digest": "9" * 64})
    # 2. Invalid schema in DB: invocation_schema_version=2
    invalid_schema_id = await _insert_malformed(
        {
            "request_digest": "9" * 64,
            "resource_id": "valid-resource",
            "invocation_schema_version": 2,
        }
    )
    # 3. Invalid digest in DB: uppercase / malformed
    invalid_digest_id = await _insert_malformed(
        {
            "request_digest": "NOT_A_VALID_HEX_DIGEST",
            "resource_id": "valid-resource",
            "invocation_schema_version": 1,
        }
    )
    # 4. Control character in resource in DB
    invalid_resource_id = await _insert_malformed(
        {
            "request_digest": "9" * 64,
            "resource_id": "bad\x1fresource",
            "invocation_schema_version": 1,
        }
    )

    async with session_factory() as session:  # type: ignore[operator]
        repo = PostgresToolCallRepository(session)
        for bad_id in (partial_id1, invalid_schema_id, invalid_digest_id, invalid_resource_id):
            with pytest.raises(PersistenceDataError, match="malformed"):
                await repo.find(bad_id)
            with pytest.raises(PersistenceDataError, match="malformed"):
                await repo.get(bad_id)
