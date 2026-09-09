"""Project, policy, and local-repository application contracts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class RepositoryInspection:
    """Safe result of inspecting one local Git repository."""

    canonical_path: str
    github_repository: str
    default_branch: str
    base_ref: str
    base_sha: str


class RepositoryInspector(Protocol):
    """Narrow read-only local repository inspection boundary."""

    def inspect(
        self,
        *,
        repository_path: str,
        data_root: str,
        github_repository: str,
        default_branch: str,
    ) -> RepositoryInspection: ...


@dataclass(frozen=True, slots=True)
class ProjectPolicyRecord:
    """One immutable persisted policy version."""

    project_id: UUID
    version: int
    policy_digest: str
    document_schema_version: int
    document: dict[str, object]
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ProjectRecord:
    """Safe project identity plus its current policy version."""

    id: UUID
    name: str
    canonical_path: str
    canonical_path_key: str
    github_repository: str
    default_branch: str
    instructions_path: str | None
    current_policy_version: int | None
    policy: ProjectPolicyRecord | None = None


class ProjectRepository(Protocol):
    """Persistence operations for project identity and append-only policies."""

    async def create(
        self,
        *,
        project_id: UUID,
        name: str,
        canonical_path: str,
        canonical_path_key: str,
        github_repository: str,
        default_branch: str,
        policy_digest: str,
        policy_document: Mapping[str, object],
        instructions_path: str | None = None,
        document_schema_version: int = 1,
    ) -> ProjectRecord: ...

    async def get(self, project_id: UUID, *, for_update: bool = False) -> ProjectRecord: ...

    async def list(self) -> Sequence[ProjectRecord]: ...

    async def append_policy(
        self,
        *,
        project_id: UUID,
        expected_policy_version: int,
        policy_digest: str,
        policy_document: Mapping[str, object],
        document_schema_version: int = 1,
    ) -> ProjectPolicyRecord: ...

    async def get_policy(
        self, project_id: UUID, version: int, *, for_update: bool = False
    ) -> ProjectPolicyRecord: ...


__all__ = [
    "ProjectPolicyRecord",
    "ProjectRecord",
    "ProjectRepository",
    "RepositoryInspection",
    "RepositoryInspector",
]
