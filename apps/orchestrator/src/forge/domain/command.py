"""Immutable durable command value types."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any
from uuid import UUID


class CommandStatus(StrEnum):
    """Lifecycle states exposed by the command domain."""

    PENDING = "pending"
    LEASED = "leased"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


SUPPORTED_PAYLOAD_SCHEMA_VERSIONS = frozenset({1})
_SECRET_KEY = re.compile(
    r"^(?:password|secret|credential|api[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"client[_-]?secret|private[_-]?key|authorization|token)$",
    re.IGNORECASE,
)
_REDACTED = frozenset({"[REDACTED]", "<redacted>", "REDACTED"})
_RAW_SECRET_VALUE = re.compile(
    r"(?i)(?:password|secret|credential|token|authorization|api[_-]?key)\s*[:=]\s*[^\s,;]+"
)


def _validate_json(value: Any, *, key: str | None = None) -> None:
    if isinstance(value, Mapping):
        for item_key, item_value in value.items():
            if not isinstance(item_key, str):
                raise TypeError("payload keys must be strings")
            normalized_key = item_key.replace("_", "").replace("-", "").lower()
            secret_key = _SECRET_KEY.fullmatch(item_key) or (
                normalized_key.endswith(("password", "secret", "credential", "token"))
                and normalized_key not in {"tokenbudget", "tokenlimit"}
            )
            if secret_key and not (
                item_value is None or (isinstance(item_value, str) and item_value in _REDACTED)
            ):
                raise ValueError(f"raw credential field is not permitted: {item_key}")
            _validate_json(item_value, key=item_key)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _validate_json(item, key=key)
    elif isinstance(value, str) and _RAW_SECRET_VALUE.search(value):
        raise ValueError("raw credential value is not permitted")
    elif value is not None and not isinstance(value, (str, int, float, bool)):
        raise ValueError("payload must contain JSON-compatible values")


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    return value


def thaw_payload(value: Any) -> Any:
    """Return a mutable JSON-ready copy of a command payload."""

    if isinstance(value, Mapping):
        return {key: thaw_payload(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [thaw_payload(item) for item in value]
    return value


def _aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _validate_error(value: str | None, name: str) -> None:
    if value is None:
        return
    if len(value) > 1024:
        raise ValueError(f"{name} is too long")
    if _RAW_SECRET_VALUE.search(value):
        raise ValueError(f"{name} contains a raw credential")


@dataclass(frozen=True, slots=True, kw_only=True)
class CommandEnvelope:
    """The immutable snapshot of one durable command."""

    id: UUID
    run_id: UUID
    command_type: str
    idempotency_key: str
    payload: Mapping[str, object]
    status: CommandStatus
    expected_run_version: int
    actor_id: UUID | None
    payload_schema_version: int
    attempt: int
    available_at: datetime
    lease_owner: str | None
    lease_expires_at: datetime | None
    created_at: datetime | None = None
    completed_at: datetime | None = None
    error_summary: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.id, UUID) or not isinstance(self.run_id, UUID):
            raise TypeError("command and run identifiers must be UUID values")
        if not self.command_type or len(self.command_type) > 96:
            raise ValueError("command type must contain 1-96 characters")
        if not self.idempotency_key or len(self.idempotency_key) > 255:
            raise ValueError("idempotency key must contain 1-255 characters")
        if not isinstance(self.status, CommandStatus):
            raise TypeError(f"unknown command status: {self.status!r}")
        if self.expected_run_version < 0 or self.attempt < 0:
            raise ValueError("command versions and attempts must be nonnegative")
        if self.payload_schema_version not in SUPPORTED_PAYLOAD_SCHEMA_VERSIONS:
            raise ValueError(f"unsupported command payload schema: {self.payload_schema_version}")
        _validate_json(self.payload)
        json.dumps(thaw_payload(self.payload))
        object.__setattr__(self, "payload", _freeze(dict(self.payload)))
        _aware(self.available_at, "command availability")
        if self.created_at is not None:
            _aware(self.created_at, "command creation time")
        if self.completed_at is not None:
            _aware(self.completed_at, "command completion time")
        if self.status is CommandStatus.LEASED:
            if not self.lease_owner or self.lease_expires_at is None:
                raise ValueError("leased commands require an owner and expiry")
            _aware(self.lease_expires_at, "command lease expiry")
        elif self.lease_owner is not None or self.lease_expires_at is not None:
            raise ValueError("non-leased commands cannot retain a lease")
        if (
            self.status
            in {
                CommandStatus.COMPLETED,
                CommandStatus.FAILED,
                CommandStatus.CANCELLED,
            }
            and self.completed_at is None
        ):
            raise ValueError("terminal commands require a completion time")
        if (
            self.status in {CommandStatus.PENDING, CommandStatus.LEASED}
            and self.completed_at is not None
        ):
            raise ValueError("non-terminal commands cannot have a completion time")
        _validate_error(self.error_summary, "command error summary")

    @property
    def type(self) -> str:
        """Compatibility alias for the contract's concise command type name."""

        return self.command_type

    @property
    def attempt_count(self) -> int:
        """Compatibility alias for persistence-oriented callers."""

        return self.attempt


__all__ = [
    "SUPPORTED_PAYLOAD_SCHEMA_VERSIONS",
    "CommandEnvelope",
    "CommandStatus",
    "thaw_payload",
]
