"""Framework-free run persistence contracts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from forge.domain.approval import ApprovalGate, PlanApprovalEvidence
from forge.domain.resource import ResourceState
from forge.domain.run import RunSnapshot, RunState


@dataclass(frozen=True, slots=True)
class RunQuiescence:
    """Durable proof that a run has no work which can race a resume."""

    pending_or_leased_commands: int
    running_steps: int
    running_executions: int
    running_tools: int
    unresolved_operations: int

    @property
    def is_quiescent(self) -> bool:
        return not any(
            (
                self.pending_or_leased_commands,
                self.running_steps,
                self.running_executions,
                self.running_tools,
                self.unresolved_operations,
            )
        )


class RunRepository(Protocol):
    """Persistence operations for the authoritative run snapshot."""

    async def get(self, run_id: UUID) -> RunSnapshot: ...

    async def get_for_update(self, run_id: UUID) -> RunSnapshot: ...

    async def duration_deadline(self, run_id: UUID) -> datetime:
        """Return creation time plus the persisted run duration budget."""
        ...

    async def approve_plan(
        self,
        run_id: UUID,
        expected_version: int,
        evidence: PlanApprovalEvidence,
        event_type: str,
        event_payload: Mapping[str, object],
        *,
        actor_class: str = "system",
        actor_id: UUID | None = None,
        occurred_at: datetime | None = None,
        payload_schema_version: int = 1,
    ) -> RunSnapshot: ...

    async def prove_quiescent(self, run_id: UUID, *, exclude_command_id: UUID) -> RunQuiescence: ...

    async def list(
        self, *, project_id: UUID | None = None, task_id: UUID | None = None
    ) -> Sequence[RunSnapshot]: ...

    async def create(self, run: RunSnapshot) -> None: ...

    async def transition(
        self,
        run_id: UUID,
        expected_version: int,
        target: RunState,
        event_type: str,
        event_payload: Mapping[str, object],
        *,
        actor_class: str = "system",
        actor_id: UUID | None = None,
        occurred_at: datetime | None = None,
        payload_schema_version: int = 1,
    ) -> RunSnapshot: ...

    async def await_approval(
        self,
        run_id: UUID,
        expected_version: int,
        gate: ApprovalGate,
        evidence_digest: str,
        event_type: str,
        event_payload: Mapping[str, object],
        *,
        actor_class: str = "system",
        actor_id: UUID | None = None,
        occurred_at: datetime | None = None,
        payload_schema_version: int = 1,
    ) -> RunSnapshot: ...

    async def intervene(
        self,
        run_id: UUID,
        expected_version: int,
        event_type: str,
        event_payload: Mapping[str, object],
        *,
        actor_class: str = "system",
        actor_id: UUID | None = None,
        occurred_at: datetime | None = None,
        payload_schema_version: int = 1,
    ) -> RunSnapshot: ...

    async def pause(
        self,
        run_id: UUID,
        expected_version: int,
        event_type: str,
        event_payload: Mapping[str, object],
        *,
        actor_class: str = "system",
        actor_id: UUID | None = None,
        occurred_at: datetime | None = None,
        payload_schema_version: int = 1,
    ) -> RunSnapshot: ...

    async def resume(
        self,
        run_id: UUID,
        expected_version: int,
        event_type: str,
        event_payload: Mapping[str, object],
        *,
        actor_class: str = "system",
        actor_id: UUID | None = None,
        occurred_at: datetime | None = None,
        payload_schema_version: int = 1,
    ) -> RunSnapshot: ...

    async def begin_local_remediation(
        self,
        run_id: UUID,
        expected_version: int,
        *,
        automatic: bool,
        limit: int,
        event_type: str,
        event_payload: Mapping[str, object],
        actor_class: str = "system",
        actor_id: UUID | None = None,
        occurred_at: datetime | None = None,
        payload_schema_version: int = 1,
    ) -> RunSnapshot: ...

    async def begin_remote_remediation(
        self,
        run_id: UUID,
        expected_version: int,
        *,
        limit: int,
        event_type: str,
        event_payload: Mapping[str, object],
        actor_class: str = "system",
        actor_id: UUID | None = None,
        occurred_at: datetime | None = None,
        payload_schema_version: int = 1,
    ) -> RunSnapshot: ...

    async def restart_planning(
        self,
        run_id: UUID,
        expected_version: int,
        *,
        policy_version: int,
        base_ref: str,
        base_sha: str,
        event_type: str,
        event_payload: Mapping[str, object],
        actor_class: str = "system",
        actor_id: UUID | None = None,
        occurred_at: datetime | None = None,
    ) -> RunSnapshot: ...

    async def update_resource(
        self,
        run_id: UUID,
        expected_version: int,
        *,
        worktree_path: str | None = None,
        database_state: ResourceState,
        database_name: str | None = None,
        database_role: str | None = None,
        secret_id: str | None = None,
        event_type: str,
        event_payload: Mapping[str, object],
        actor_class: str = "system",
        actor_id: UUID | None = None,
        occurred_at: datetime | None = None,
        payload_schema_version: int = 1,
    ) -> RunSnapshot: ...

    async def bind_preparation_branch(
        self,
        run_id: UUID,
        expected_version: int,
        *,
        branch_name: str,
        event_type: str,
        event_payload: Mapping[str, object],
        actor_class: str = "system",
        actor_id: UUID | None = None,
        occurred_at: datetime | None = None,
        payload_schema_version: int = 1,
    ) -> RunSnapshot: ...


__all__ = ["RunQuiescence", "RunRepository"]
