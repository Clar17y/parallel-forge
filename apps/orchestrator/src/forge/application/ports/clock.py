"""Injectable, timezone-aware clocks for security-sensitive expiry checks."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    """The small time source required by authentication and approvals."""

    def now(self) -> datetime: ...


class SystemClock:
    """Return the current UTC instant with an explicit aware timezone."""

    def now(self) -> datetime:
        return datetime.now(UTC)


__all__ = ["Clock", "SystemClock"]
