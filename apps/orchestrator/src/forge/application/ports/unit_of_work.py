"""Framework-free unit-of-work and event persistence contracts."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, Self
from uuid import UUID

from forge.application.ports.runs import RunRepository
from forge.domain.event import RunEvent


class EventRepository(Protocol):
    """Append-only causal event operations."""

    async def append(self, event: RunEvent) -> RunEvent: ...

    async def list_after(self, run_id: UUID, sequence: int) -> Sequence[RunEvent]: ...


class UnitOfWork(Protocol):
    """One explicit transaction shared by run and event repositories."""

    runs: RunRepository
    events: EventRepository

    async def __aenter__(self) -> Self: ...

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None: ...

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...


__all__ = ["EventRepository", "UnitOfWork"]
