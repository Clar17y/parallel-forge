"""Run and closed command HTTP schemas."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from forge.application.services.runs import RunCommandRequest as ServiceRunCommandRequest
from forge.domain.command import CommandEnvelope
from forge.domain.run import RunSnapshot, RunState, SuspensionKind


class RunCreateRequest(BaseModel):
    """Closed run creation body."""

    model_config = ConfigDict(extra="forbid")

    task_id: UUID
    profile_id: UUID | None = None
    profile_version: StrictInt | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def complete_profile_identity(self) -> RunCreateRequest:
        if (self.profile_id is None) != (self.profile_version is None):
            raise ValueError("profile_id and profile_version must be supplied together")
        return self


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
    subscription_profile: RunProfileSelection | None = None

    @classmethod
    def from_snapshot(
        cls, run: RunSnapshot, subscription_profile: RunProfileSelection | dict[str, object] | None = None
    ) -> RunResponse:
        if isinstance(subscription_profile, dict):
            subscription_profile = RunProfileSelection.model_validate(subscription_profile)
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
            subscription_profile=subscription_profile,
        )


class RunProfileSelection(BaseModel):
    """Frozen profile identity; provenance can be unknown on retained history."""

    model_config = ConfigDict(extra="forbid")

    profile_id: UUID
    profile_version: int
    selection_source: Literal["project_default", "run_override"] | None


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
