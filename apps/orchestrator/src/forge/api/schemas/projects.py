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


class ProjectCreateRequest(ServiceProjectRegistrationRequest):
    """Closed project registration body."""


class ProjectPolicyUpdateRequest(ServicePolicyUpdateRequest):
    """Closed mutable policy update body."""


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

    @classmethod
    def from_record(cls, project: ProjectRecord) -> ProjectResponse:
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
