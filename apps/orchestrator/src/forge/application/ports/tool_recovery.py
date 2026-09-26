"""Read-only, content-bound proof of terminal controlled effects."""

from collections.abc import Mapping
from dataclasses import dataclass, fields
from datetime import datetime
from enum import Enum
from typing import Protocol
from uuid import UUID

from forge.application.ports.tools import ToolCallRecord
from forge.domain.artifact import validate_artifact_digest
from forge.domain.operation import OperationIntent, canonical_digest


def _value(value: object) -> object:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {key: _value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_value(item) for item in value]
    return value


def terminal_call_digest(call: ToolCallRecord) -> str:
    return canonical_digest(
        {field.name: _value(getattr(call, field.name)) for field in fields(call)}
    )


def terminal_intent_digest(intent: OperationIntent) -> str:
    return canonical_digest(
        {
            field.name: _value(getattr(intent, field.name))
            for field in fields(intent)
            if field.name != "is_new"
        }
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class VerifiedTerminalEffect:
    effect_id: UUID
    call_digest: str
    intent_digests: tuple[tuple[UUID, str], ...]

    def __post_init__(self) -> None:
        if not isinstance(self.effect_id, UUID) or self.effect_id.int == 0:
            raise ValueError("terminal effect identity is required")
        validate_artifact_digest(self.call_digest)
        values = tuple(self.intent_digests)
        if not 1 <= len(values) <= 2 or len({identity for identity, _ in values}) != len(values):
            raise ValueError("terminal proof must bind distinct operation intents")
        for identity, digest in values:
            if not isinstance(identity, UUID) or identity.int == 0:
                raise ValueError("terminal operation identity is required")
            validate_artifact_digest(digest)
        object.__setattr__(self, "intent_digests", values)


class TerminalEffectVerifier(Protocol):
    async def verify_terminal_effect(self, effect_id: UUID) -> VerifiedTerminalEffect | None: ...
