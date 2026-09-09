"""Task-local correlation identifiers shared by traces and durable events."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, fields, replace
from uuid import UUID


@dataclass(frozen=True, slots=True, kw_only=True)
class CorrelationContext:
    """Non-secret identifiers that connect one unit of Forge work."""

    run_id: UUID | None = None
    step_id: UUID | None = None
    command_id: UUID | None = None
    agent_execution_id: UUID | None = None
    tool_call_id: UUID | None = None
    operation_intent_id: UUID | None = None

    def __post_init__(self) -> None:
        for item in fields(self):
            value = getattr(self, item.name)
            if value is not None and not isinstance(value, UUID):
                raise TypeError(f"{item.name} must be a UUID or None")

    def to_dict(self) -> dict[str, str]:
        """Serialize only present identifiers for JSON and span attributes."""

        return {
            item.name: str(value)
            for item in fields(self)
            if (value := getattr(self, item.name)) is not None
        }

    def merged(self, other: CorrelationContext) -> CorrelationContext:
        """Overlay non-null identifiers from ``other`` onto this context."""

        updates = {
            item.name: value
            for item in fields(other)
            if (value := getattr(other, item.name)) is not None
        }
        return replace(self, **updates)


_EMPTY_CONTEXT = CorrelationContext()
_CURRENT_CONTEXT: ContextVar[CorrelationContext] = ContextVar(
    "forge_correlation_context", default=_EMPTY_CONTEXT
)


def current_context() -> CorrelationContext:
    """Return the correlation context isolated to the current async task."""

    return _CURRENT_CONTEXT.get()


@contextmanager
def bind_context(
    context: CorrelationContext | None = None,
    *,
    run_id: UUID | None = None,
    step_id: UUID | None = None,
    command_id: UUID | None = None,
    agent_execution_id: UUID | None = None,
    tool_call_id: UUID | None = None,
    operation_intent_id: UUID | None = None,
) -> Iterator[CorrelationContext]:
    """Bind a merged context and restore the prior value on every exit path."""

    overlay = context or CorrelationContext(
        run_id=run_id,
        step_id=step_id,
        command_id=command_id,
        agent_execution_id=agent_execution_id,
        tool_call_id=tool_call_id,
        operation_intent_id=operation_intent_id,
    )
    if context is not None and any(
        value is not None
        for value in (
            run_id,
            step_id,
            command_id,
            agent_execution_id,
            tool_call_id,
            operation_intent_id,
        )
    ):
        raise ValueError("bind either a context or individual identifiers, not both")
    bound = current_context().merged(overlay)
    token = _CURRENT_CONTEXT.set(bound)
    try:
        yield bound
    finally:
        _CURRENT_CONTEXT.reset(token)


__all__ = ["CorrelationContext", "bind_context", "current_context"]
