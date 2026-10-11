"""Project and policy HTTP schemas."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from forge.application.ports.projects import ProjectPolicyRecord, ProjectRecord
from forge.application.services.projects import (
    PolicyUpdateRequest as ServicePolicyUpdateRequest,
)
from forge.application.services.projects import (
    ProjectRegistrationRequest as ServiceProjectRegistrationRequest,
)
from forge.application.services.task_usage_policy import MonitoringPolicyRevisionResult
from forge.domain.task_usage_contract import MonitoringPolicy


class ProjectCreateRequest(ServiceProjectRegistrationRequest):
    """Closed project registration body."""


class ProjectPolicyUpdateRequest(ServicePolicyUpdateRequest):
    """Closed mutable policy update body."""


class MonitoringOwnerActionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor_id: UUID
    action: str
    affected_snapshot: str
    warnings: tuple[str, ...]
    replayed: bool


class MonitoringPolicyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: UUID
    revision: int
    mode: str
    warning_multiplier: float
    checkpoint_multiplier: float
    history_limit: int
    window_days: int | None
    minimum_comparables: int
    hard_token_cap: int | None
    hard_context_cap: int | None
    manual_estimate_tokens: int | None
    allow_cross_project_fallback: bool
    owner_action: MonitoringOwnerActionResponse | None = None

    @classmethod
    def from_policy(cls, policy: MonitoringPolicy) -> MonitoringPolicyResponse:
        return cls(
            project_id=policy.project_id,
            revision=policy.revision,
            mode=policy.mode.value,
            warning_multiplier=policy.warning_multiplier,
            checkpoint_multiplier=policy.checkpoint_multiplier,
            history_limit=policy.history_limit,
            window_days=policy.window_days,
            minimum_comparables=policy.minimum_comparables,
            hard_token_cap=policy.hard_token_cap,
            hard_context_cap=policy.hard_context_cap,
            manual_estimate_tokens=policy.manual_estimate_tokens,
            allow_cross_project_fallback=policy.allow_cross_project_fallback,
        )

    @classmethod
    def from_revision(cls, result: MonitoringPolicyRevisionResult) -> MonitoringPolicyResponse:
        return cls.from_policy(result.policy).model_copy(
            update={
                "owner_action": MonitoringOwnerActionResponse(
                    actor_id=result.actor_id,
                    action=result.action,
                    affected_snapshot=result.affected_snapshot,
                    warnings=result.warnings,
                    replayed=result.replayed,
                )
            }
        )


ProjectRegistrationRequest = ProjectCreateRequest
PolicyUpdate = ProjectPolicyUpdateRequest


class ProjectResponse(BaseModel):
    """Safe project identity and current policy summary."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    name: str
    repository_path: str
    canonical_path_key: str
    github_repository: str
    default_branch: str
    instructions_path: str | None
    policy_version: int | None
    policy_digest: str | None
    policy: dict[str, Any] | None = None
    issue_import_available: bool = False

    @classmethod
    def from_record(
        cls, project: ProjectRecord, *, issue_import_available: bool = False
    ) -> ProjectResponse:
        policy = project.policy
        return cls(
            id=project.id,
            name=project.name,
            repository_path=project.canonical_path,
            canonical_path_key=project.canonical_path_key,
            github_repository=project.github_repository,
            default_branch=project.default_branch,
            instructions_path=project.instructions_path,
            policy_version=project.current_policy_version,
            policy_digest=None if policy is None else policy.policy_digest,
            policy=None if policy is None else dict(policy.document),
            issue_import_available=issue_import_available,
        )


class ProjectPolicyResponse(BaseModel):
    """Safe immutable policy-version response."""

    model_config = ConfigDict(extra="forbid")

    project_id: UUID
    version: int
    policy_version: int
    policy_digest: str
    document_schema_version: int
    document: dict[str, Any]

    @classmethod
    def from_record(cls, policy: ProjectPolicyRecord) -> ProjectPolicyResponse:
        return cls(
            project_id=policy.project_id,
            version=policy.version,
            policy_version=policy.version,
            policy_digest=policy.policy_digest,
            document_schema_version=policy.document_schema_version,
            document=dict(policy.document),
        )


__all__ = [
    "PolicyUpdate",
    "ProjectCreateRequest",
    "ProjectPolicyResponse",
    "ProjectPolicyUpdateRequest",
    "ProjectRegistrationRequest",
    "ProjectResponse",
]
