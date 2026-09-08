"""Framework-free durable command contracts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from forge.domain.command import CommandEnvelope


class CommandLeaseLost(RuntimeError):
    """A delivery no longer owns the command lease that admitted it."""


class CommandRecoveryRequired(RuntimeError):
    """A delivery found durable work whose outcome needs explicit recovery."""


class CommandSuspended(RuntimeError):
    """A known completed delivery stopped at a durable pause or cancellation."""


class CommandLane(StrEnum):
    """Closed worker lanes for mutually constrained durable command leases."""

    NORMAL = "normal"
    CONTROL = "control"


class CommandRepository(Protocol):
    """Persistence boundary for idempotent commands and worker leases."""

    async def enqueue(
        self,
        *,
        run_id: UUID,
        command_type: str,
        idempotency_key: str,
        payload: Mapping[str, object],
        expected_run_version: int = 0,
        actor_id: UUID | None = None,
        payload_schema_version: int = 1,
        available_at: datetime | None = None,
    ) -> CommandEnvelope: ...

    async def get(self, command_id: UUID) -> CommandEnvelope: ...

    async def list_expired_terminal_commands(self) -> Sequence[CommandEnvelope]:
        """Discover expired deliveries that normal terminal-state dispatch excludes."""

    async def get_by_idempotency_key(self, idempotency_key: str) -> CommandEnvelope | None: ...

    async def claim_next(
        self, *, worker_id: str, lease_seconds: float, lane: CommandLane = CommandLane.NORMAL
    ) -> CommandEnvelope | None: ...

    async def renew(
        self, command_id: UUID, *, worker_id: str, lease_seconds: float
    ) -> CommandEnvelope: ...

    async def assert_current_lease(self, command: CommandEnvelope) -> CommandEnvelope:
        """Fence a delivery before it makes durable effects in its UoW."""

    async def has_pending_current_control_stop(
        self, *, run_id: UUID, expected_run_version: int
    ) -> bool:
        """Check for an admitted, still-actionable pause or cancellation.

        Callers hold the run row lock for ``expected_run_version``.  That lock
        serializes this check with control admission, preventing a finalizer
        from advancing a run after a stop was accepted for its current version.
        """

    async def list_outstanding_normal(
        self, *, run_id: UUID, exclude_command_id: UUID
    ) -> Sequence[CommandEnvelope]:
        """List pending or leased normal work while the caller holds the run lock."""

    async def list_failed_normal(
        self, *, run_id: UUID, exclude_command_id: UUID
    ) -> Sequence[CommandEnvelope]:
        """List terminal failed normal commands under the caller's run-row lock."""

    async def cancel_expired_observed_lease(
        self, command: CommandEnvelope, *, reason: str
    ) -> CommandEnvelope | None:
        """Cancel only the exact expired lease observed by paused-run recovery.

        This caller-transaction-bound operation never claims work. ``None``
        means another delivery changed the observed lease before settlement.
        """

    async def cancel_pending_unstarted(self, command: CommandEnvelope) -> CommandEnvelope | None:
        """Cancel exactly an observed pending, never-admitted normal command.

        This caller-transaction-bound operation matches the complete immutable
        command envelope and ``attempt == 0``. ``None`` means the command was
        claimed, renewed, tampered with, or otherwise changed before settlement.
        """

    async def complete_expired_observed_lease(
        self, command: CommandEnvelope
    ) -> CommandEnvelope | None:
        """Acknowledge an exact expired delivery after its outcome is verified in this UoW."""

    async def complete(
        self, command_id: UUID, *, worker_id: str, result: Mapping[str, object] | None = None
    ) -> CommandEnvelope: ...

    async def fail(
        self,
        command_id: UUID,
        *,
        worker_id: str,
        error: str,
        transient: bool = False,
    ) -> CommandEnvelope: ...


__all__ = [
    "CommandLane",
    "CommandLeaseLost",
    "CommandRecoveryRequired",
    "CommandRepository",
    "CommandSuspended",
]
