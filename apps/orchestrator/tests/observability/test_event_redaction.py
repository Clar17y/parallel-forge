"""Integration coverage for event correlation and the redaction boundary."""

from __future__ import annotations

from uuid import uuid4

import pytest
from forge.domain.event import RunEvent
from forge.observability.context import bind_context
from forge.observability.redaction import RedactionPolicy, Redactor
from forge.persistence.repositories.runs import PersistenceDataError
from forge.persistence.unit_of_work import PostgresUnitOfWork


@pytest.mark.integration
async def test_event_append_persists_redacted_bounded_payload_and_current_context(
    session_factory, persisted_run
) -> None:
    command_id = uuid4()
    redactor = Redactor(
        secrets=("literal-secret",),
        policy=RedactionPolicy(
            max_string_bytes=64,
            max_collection_items=4,
            max_depth=6,
            max_nodes=100,
        ),
    )
    with bind_context(run_id=persisted_run.id, command_id=command_id):
        async with PostgresUnitOfWork(session_factory, redactor=redactor) as work:
            stored = await work.events.append(
                RunEvent(
                    run_id=persisted_run.id,
                    run_version=0,
                    event_type="observability.test",
                    payload={
                        "token": "raw-token",
                        "message": "literal-secret " + "x" * 80,
                        "correlation": {"run_id": "spoofed", "safe": "visible"},
                    },
                )
            )
            await work.commit()

    assert stored.payload["token"] == "[REDACTED]"
    assert "literal-secret" not in str(stored.payload)
    assert len(str(stored.payload["message"]).encode()) <= 64
    assert stored.payload["correlation"] == {
        "run_id": str(persisted_run.id),
        "command_id": str(command_id),
        "safe": "visible",
    }

    async with PostgresUnitOfWork(session_factory) as work:
        loaded = await work.events.list_after(persisted_run.id, 0)
    assert loaded == [stored]


@pytest.mark.integration
async def test_event_append_rejects_a_different_ambient_run(session_factory, persisted_run) -> None:
    with (
        bind_context(run_id=uuid4()),
        pytest.raises(PersistenceDataError, match="active correlation"),
    ):
        async with PostgresUnitOfWork(session_factory) as work:
            await work.events.append(
                RunEvent(
                    run_id=persisted_run.id,
                    run_version=0,
                    event_type="observability.mismatch",
                    payload={},
                )
            )
