"""Immutable causal events emitted by run-state transitions."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any
from uuid import UUID, uuid4

ACTOR_CLASSES = frozenset({"system", "worker", "agent", "operator", "unauthenticated"})
MAX_EVENT_TYPE_LENGTH = 96


def _freeze(value: Any) -> Any:
    """Take an immutable snapshot of a JSON-compatible payload value."""

    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze(item) for item in value)
    return value


def thaw_payload(value: Any) -> Any:
    """Return a mutable JSON-ready copy without sharing event-owned values."""

    if isinstance(value, Mapping):
        return {key: thaw_payload(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [thaw_payload(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return [thaw_payload(item) for item in value]
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class RunEvent:
    """An immutable, versioned causal event for one run.

    ``sequence`` is optional while an event is being assembled.  Persistence
    assigns the next positive per-run sequence before returning the stored
    event.  Every event that crosses the persistence boundary therefore has a
    concrete positive sequence.
    """

    run_id: UUID
    run_version: int
    event_type: str
    payload: Mapping[str, object] = field(default_factory=dict)
    event_id: UUID = field(default_factory=uuid4)
    sequence: int | None = None
    actor_class: str = "system"
    actor_id: UUID | None = None
    payload_schema_version: int = 1
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not isinstance(self.event_id, UUID) or not isinstance(self.run_id, UUID):
            raise TypeError("event and run identifiers must be UUID values")
        if self.actor_id is not None and not isinstance(self.actor_id, UUID):
            raise TypeError("event actor identifier must be a UUID")
        if self.sequence is not None and self.sequence < 1:
            raise ValueError("event sequence must be positive when assigned")
        if self.run_version < 0:
            raise ValueError("run version must be nonnegative")
        if not self.event_type or len(self.event_type) > MAX_EVENT_TYPE_LENGTH:
            raise ValueError(f"event type must contain 1-{MAX_EVENT_TYPE_LENGTH} characters")
        if self.actor_class not in ACTOR_CLASSES:
            raise ValueError(f"unsupported event actor class: {self.actor_class!r}")
        if self.payload_schema_version < 1:
            raise ValueError("payload schema version must be positive")
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("event timestamp must be timezone-aware")
        if any(not isinstance(key, str) for key in self.payload):
            raise ValueError("event payload keys must be strings")
        object.__setattr__(self, "payload", _freeze(dict(self.payload)))

    @property
    def id(self) -> UUID:
        """Alias matching the persistence model's primary-key attribute."""

        return self.event_id


__all__ = [
    "ACTOR_CLASSES",
    "MAX_EVENT_TYPE_LENGTH",
    "RunEvent",
    "thaw_payload",
]
