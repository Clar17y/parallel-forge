"""Closed, bounded public authoring diagnostics."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from forge.domain.epic_brainstorm import BrainstormDiagnosticEvent, BrainstormDiagnostics
from pydantic import ValidationError


def test_diagnostic_event_rejects_unstructured_or_secret_fields() -> None:
    safe = {
        "sequence": 0,
        "kind": "tool_started",
        "occurred_at": datetime.now(UTC),
        "elapsed_ms": 1,
        "operation": "repository_read",
        "tool_name": "list_files",
    }
    event = BrainstormDiagnosticEvent.model_validate(safe)
    assert event.tool_name == "list_files"
    for unsafe in ({**safe, "prompt": "private"}, {**safe, "tool_name": "cat_secrets"}):
        with pytest.raises(ValidationError):
            BrainstormDiagnosticEvent.model_validate(unsafe)
    with pytest.raises(ValidationError):
        BrainstormDiagnosticEvent.model_validate(
            {**safe, "occurred_at": datetime.now(UTC).replace(tzinfo=None)}
        )


def test_diagnostics_limit_is_public_contract() -> None:
    event = BrainstormDiagnosticEvent(
        sequence=0, kind="launch", occurred_at=datetime.now(UTC), elapsed_ms=0
    )
    assert BrainstormDiagnostics(attempt_id=uuid4(), events=(event,) * 128).dropped_events == 0
    with pytest.raises(ValidationError):
        BrainstormDiagnostics(attempt_id=uuid4(), events=(event,) * 129)
