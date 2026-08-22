"""Immutable operation-intent and adapter outcome value types."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any
from uuid import UUID, uuid4

from forge.domain.command import _validate_error, _validate_json


class OperationStatus(StrEnum):
    """Domain vocabulary for durable side-effect intents."""

    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    NEEDS_RECONCILIATION = "needs_reconciliation"


SUPPORTED_OPERATION_SCHEMA_VERSIONS = frozenset({1})
_DIGEST = re.compile(r"\A[0-9a-f]{64}\Z")


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    return value


def thaw_payload(value: Any) -> Any:
    """Return a mutable copy suitable for JSON persistence."""

    if isinstance(value, Mapping):
        return {key: thaw_payload(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [thaw_payload(item) for item in value]
    return value


def canonical_payload(value: Mapping[str, object]) -> str:
    """Serialize a redacted JSON payload deterministically for idempotency."""

    _validate_json(value)
    return json.dumps(thaw_payload(value), sort_keys=True, separators=(",", ":"))


def canonical_digest(value: Mapping[str, object]) -> str:
    """Compute the lowercase SHA-256 digest used by request contracts."""

    return hashlib.sha256(canonical_payload(value).encode()).hexdigest()


def _aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


@dataclass(frozen=True, slots=True, kw_only=True)
class OperationRequest:
    """A redacted request that can be persisted before invoking an adapter."""

    run_id: UUID
    kind: str
    idempotency_key: str
    request_digest: str
    request_payload: Mapping[str, object]
    request_schema_version: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, UUID):
            raise TypeError("operation run identifier must be a UUID")
        if not self.kind or len(self.kind) > 96:
            raise ValueError("operation kind must contain 1-96 characters")
        if not self.idempotency_key or len(self.idempotency_key) > 255:
            raise ValueError("idempotency key must contain 1-255 characters")
        if not _DIGEST.fullmatch(self.request_digest):
            raise ValueError("request digest must be lowercase hexadecimal SHA-256")
        if self.request_schema_version not in SUPPORTED_OPERATION_SCHEMA_VERSIONS:
            raise ValueError(f"unsupported operation request schema: {self.request_schema_version}")
        _validate_json(self.request_payload)
        json.dumps(thaw_payload(self.request_payload))
        object.__setattr__(self, "request_payload", _freeze(dict(self.request_payload)))


@dataclass(frozen=True, slots=True, kw_only=True)
class OperationOutcome:
    """The redacted result returned by invocation or reconciliation."""

    remote_resource_id: str | None = None
    payload: Mapping[str, object] = field(default_factory=dict)
    status: OperationStatus = OperationStatus.SUCCEEDED
    error: str | None = None
    outcome_schema_version: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.status, OperationStatus):
            raise TypeError(f"unknown operation status: {self.status!r}")
        if self.status is OperationStatus.PENDING:
            raise ValueError("operation outcomes cannot remain pending")
        if self.outcome_schema_version not in SUPPORTED_OPERATION_SCHEMA_VERSIONS:
            raise ValueError(f"unsupported operation outcome schema: {self.outcome_schema_version}")
        _validate_json(self.payload)
        json.dumps(thaw_payload(self.payload))
        object.__setattr__(self, "payload", _freeze(dict(self.payload)))
        if self.remote_resource_id is not None and len(self.remote_resource_id) > 1024:
            raise ValueError("remote resource identifier is too long")
        _validate_error(self.error, "operation outcome error")


@dataclass(frozen=True, slots=True, kw_only=True)
class OperationIntent:
    """The immutable durable record for one external/local side effect."""

    id: UUID = field(default_factory=uuid4)
    run_id: UUID = field(default_factory=uuid4)
    kind: str = ""
    idempotency_key: str = ""
    request_digest: str = ""
    request_payload: Mapping[str, object] = field(default_factory=dict)
    status: OperationStatus = OperationStatus.PENDING
    remote_resource_id: str | None = None
    attempt: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    outcome: Mapping[str, object] | None = None
    error: str | None = None
    request_schema_version: int = 1
    outcome_schema_version: int | None = None
    # Repository begin marks a new row so an executor never mistakes an
    # existing unresolved intent for a definitely-new side effect.
    is_new: bool = field(default=False, compare=False, repr=False)

    def __post_init__(self) -> None:
        request = OperationRequest(
            run_id=self.run_id,
            kind=self.kind,
            idempotency_key=self.idempotency_key,
            request_digest=self.request_digest,
            request_payload=self.request_payload,
            request_schema_version=self.request_schema_version,
        )
        object.__setattr__(self, "request_payload", request.request_payload)
        if not isinstance(self.id, UUID):
            raise TypeError("operation identifier must be a UUID")
        if not isinstance(self.status, OperationStatus):
            raise TypeError(f"unknown operation status: {self.status!r}")
        if self.attempt < 0:
            raise ValueError("operation attempt must be nonnegative")
        if self.outcome_schema_version is not None and (
            self.outcome_schema_version not in SUPPORTED_OPERATION_SCHEMA_VERSIONS
        ):
            raise ValueError(f"unsupported operation outcome schema: {self.outcome_schema_version}")
        if self.outcome is not None:
            _validate_json(self.outcome)
            json.dumps(thaw_payload(self.outcome))
            object.__setattr__(self, "outcome", _freeze(dict(self.outcome)))
            if self.outcome_schema_version is None:
                raise ValueError("an outcome requires its schema version")
        elif self.outcome_schema_version is not None:
            raise ValueError("an outcome schema requires an outcome")
        if self.status is not OperationStatus.SUCCEEDED and (
            self.outcome is not None or self.outcome_schema_version is not None
        ):
            raise ValueError("only succeeded operation intents may contain an outcome")
        _validate_error(self.error, "operation error")
        for value, name in (
            (self.created_at, "operation creation time"),
            (self.updated_at, "operation update time"),
            (self.started_at, "operation start time"),
            (self.completed_at, "operation completion time"),
        ):
            if value is not None:
                _aware(value, name)
        if self.status is OperationStatus.SUCCEEDED and self.completed_at is None:
            raise ValueError("succeeded operation intents require completion time")
        if self.status is OperationStatus.SUCCEEDED and self.outcome is None:
            raise ValueError("succeeded operation intents require an outcome")
        if self.status is OperationStatus.FAILED and self.completed_at is None:
            raise ValueError("failed operation intents require completion time")
        if self.status is OperationStatus.FAILED and not self.error:
            raise ValueError("failed operation intents require an error")
        if (
            self.status in {OperationStatus.PENDING, OperationStatus.NEEDS_RECONCILIATION}
            and self.completed_at is not None
        ):
            raise ValueError("unresolved operation intents cannot be completed")

    @property
    def operation_type(self) -> str:
        """Compatibility alias for callers using the plan's sample name."""

        return self.kind

    @property
    def operation_kind(self) -> str:
        """Compatibility alias matching the SQLAlchemy column name."""

        return self.kind

    @property
    def attempt_count(self) -> int:
        """Compatibility alias for persistence-oriented callers."""

        return self.attempt

    @property
    def outcome_payload(self) -> Mapping[str, object] | None:
        """Compatibility alias for the stored JSON outcome snapshot."""

        return self.outcome

    def to_outcome(self) -> OperationOutcome:
        """Convert a succeeded intent's persisted result into an outcome."""

        if self.status is not OperationStatus.SUCCEEDED:
            raise ValueError("only succeeded intents have outcomes")
        return OperationOutcome(
            remote_resource_id=self.remote_resource_id,
            payload=self.outcome or {},
            status=self.status,
            outcome_schema_version=self.outcome_schema_version or 1,
        )


__all__ = [
    "SUPPORTED_OPERATION_SCHEMA_VERSIONS",
    "OperationIntent",
    "OperationOutcome",
    "OperationRequest",
    "OperationStatus",
    "canonical_digest",
    "canonical_payload",
    "thaw_payload",
]
