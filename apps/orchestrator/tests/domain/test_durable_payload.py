"""Security regression coverage for durable command and operation payloads."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from forge.domain.command import CommandEnvelope, CommandStatus
from forge.domain.operation import OperationRequest, canonical_digest

SECRET_PAYLOADS = (
    {"description": "Authorization: Bearer ghp_0123456789abcdefghijklmnopqrstuv"},
    {"description": "Basic dXNlcjpzdXBlci1zZWNyZXQ="},
    {"description": "headers include x-api-key=api-secret-value"},
    {"description": "token=do-not-persist"},
    {"description": "postgresql://forge:super-secret@db.internal/forge"},
    {"description": "password=[REDACTED]still-secret"},
    {"description": "postgresql://forge:[REDACTED]still-secret@db.internal/forge"},
    {"description": "-----BEGIN PRIVATE KEY-----\nsecret-material\n-----END PRIVATE KEY-----"},
    {"description": "github_pat_0123456789abcdefghijklmnopqrstuv"},
)


@pytest.mark.parametrize("payload", SECRET_PAYLOADS)
def test_command_payload_rejects_credentials_under_benign_keys_without_echoing_values(
    payload: dict[str, str],
) -> None:
    with pytest.raises(ValueError) as error:
        CommandEnvelope(
            id=uuid4(),
            run_id=uuid4(),
            command_type="safe-command",
            idempotency_key="payload-security",
            payload=payload,
            status=CommandStatus.PENDING,
            expected_run_version=0,
            actor_id=None,
            payload_schema_version=1,
            attempt=0,
            available_at=datetime.now(UTC),
            lease_owner=None,
            lease_expires_at=None,
        )

    assert "secret" not in str(error.value).lower()
    assert "ghp_0123456789abcdefghijklmnopqrstuv" not in str(error.value)
    assert "super-secret" not in str(error.value)


@pytest.mark.parametrize("payload", SECRET_PAYLOADS)
def test_operation_payload_rejects_credentials_under_benign_keys_without_echoing_values(
    payload: dict[str, str],
) -> None:
    with pytest.raises(ValueError) as error:
        OperationRequest(
            run_id=uuid4(),
            kind="safe-operation",
            idempotency_key="payload-security",
            request_digest=canonical_digest(payload),
            request_payload=payload,
        )

    assert "secret" not in str(error.value).lower()
    assert "ghp_0123456789abcdefghijklmnopqrstuv" not in str(error.value)
    assert "super-secret" not in str(error.value)


def test_redacted_secret_fields_remain_allowed() -> None:
    payload = {
        "authorization": "[REDACTED]",
        "api_key": "<redacted>",
        "private_key": None,
    }
    CommandEnvelope(
        id=uuid4(),
        run_id=uuid4(),
        command_type="safe-command",
        idempotency_key="redacted-payload",
        payload=payload,
        status=CommandStatus.PENDING,
        expected_run_version=0,
        actor_id=None,
        payload_schema_version=1,
        attempt=0,
        available_at=datetime.now(UTC),
        lease_owner=None,
        lease_expires_at=None,
    )
