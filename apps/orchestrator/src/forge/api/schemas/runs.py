"""Run and closed command HTTP schemas."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict

from forge.application.services.runs import RunCommandRequest as ServiceRunCommandRequest
from forge.domain.command import CommandEnvelope
from forge.domain.run import RunSnapshot, RunState, SuspensionKind


class RunCreateRequest(BaseModel):
    """Closed run creation body."""

    model_config = ConfigDict(extra="forbid")

    task_id: UUID


class RunCommandRequest(ServiceRunCommandRequest):
    """Closed command body accepted by the run command route."""


RunCreate = RunCreateRequest
RunCommandInput = RunCommandRequest


class RunResponse(BaseModel):
    """Safe authoritative run snapshot."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    project_id: UUID
    task_id: UUID
    state: RunState
    version: int
    suspended_state: RunState | None
    suspension_kind: SuspensionKind | None
    local_remediation_count: int
    remote_remediation_count: int
    policy_version: int | None
    base_ref: str | None
    base_sha: str | None
    branch_name: str | None

    @classmethod
    def from_snapshot(cls, run: RunSnapshot) -> RunResponse:
        return cls(
            id=run.id,
            project_id=run.project_id,
            task_id=run.task_id,
            state=run.state,
            version=run.version,
            suspended_state=run.suspended_state,
            suspension_kind=run.suspension_kind,
            local_remediation_count=run.local_remediation_count,
            remote_remediation_count=run.remote_remediation_count,
            policy_version=run.policy_version,
            base_ref=run.base_ref,
            base_sha=run.base_sha,
            branch_name=run.branch_name,
        )


class RunCommandResponse(BaseModel):
    """Bounded command acknowledgement; payloads are never echoed."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    run_id: UUID
    command_type: str
    status: str
    expected_run_version: int

    @classmethod
    def from_command(cls, command: CommandEnvelope) -> RunCommandResponse:
        return cls(
            id=command.id,
            run_id=command.run_id,
            command_type=command.command_type,
            status=command.status.value,
            expected_run_version=command.expected_run_version,
        )


__all__ = [
    "RunCommandInput",
    "RunCommandRequest",
    "RunCommandResponse",
    "RunCreate",
    "RunCreateRequest",
    "RunResponse",
]
