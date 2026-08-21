"""Domain errors raised when a run command violates the state contract."""

from __future__ import annotations

from forge.domain.run import RunState


class InvalidTransition(ValueError):
    """Raised when a requested run-state operation is not legal."""

    def __init__(
        self,
        current: RunState | None = None,
        target: RunState | None = None,
        *,
        reason: str | None = None,
    ) -> None:
        self.current = current
        self.target = target
        detail = reason or "the requested state change is not legal"
        if current is not None and target is not None:
            detail = f"{current.value} -> {target.value} is not legal: {detail}"
        elif current is not None:
            detail = f"{current.value} cannot be changed: {detail}"
        super().__init__(detail)
