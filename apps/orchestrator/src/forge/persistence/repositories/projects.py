"""PostgreSQL repositories for project identity and immutable policies."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from forge.application.ports.projects import ProjectPolicyRecord, ProjectRecord
from forge.persistence.models import Project, ProjectPolicyVersion

_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class ProjectRepositoryError(RuntimeError):
    """Base for safe project persistence errors."""


class ProjectNotFound(ProjectRepositoryError):
    """The requested project does not exist."""


class PolicyNotFound(ProjectRepositoryError):
    """The requested immutable policy version does not exist."""


class ProjectIdentityConflict(ProjectRepositoryError):
    """A canonical project identity is already registered."""


class PolicyVersionConflict(ProjectRepositoryError):
    """The expected current policy version is stale or malformed."""


class PostgresProjectRepository:
    """Map project and policy records inside the caller's transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

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
    ) -> ProjectRecord:
        """Create identity plus policy version one in this transaction."""

        _validate_digest(policy_digest)
        _validate_policy_version(document_schema_version)
        normalized_repository = _normalize_repository(github_repository)
        existing = (
            await self._session.execute(
                select(Project.id).where(
                    or_(
                        Project.canonical_path_key == canonical_path_key,
                        Project.github_repository == normalized_repository,
                    )
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise ProjectIdentityConflict("project identity is already registered")

        project = Project(
            id=project_id,
            name=name,
            canonical_path=canonical_path,
            canonical_path_key=canonical_path_key,
            github_repository=normalized_repository,
            default_branch=default_branch,
            instructions_path=instructions_path,
            current_policy_version=None,
        )
        self._session.add(project)
        await self._session.flush()
        policy = ProjectPolicyVersion(
            project_id=project_id,
            version=1,
            policy_digest=policy_digest,
            document_schema_version=document_schema_version,
            document=dict(policy_document),
        )
        self._session.add(policy)
        await self._session.flush()
        project.current_policy_version = 1
        await self._session.flush()
        return _project_from_record(project, policy=policy)

    async def get(self, project_id: UUID, *, for_update: bool = False) -> ProjectRecord:
        """Load one project and its current immutable policy."""

        if for_update:
            result = await self._session.execute(
                select(Project).where(Project.id == project_id).with_for_update()
            )
            project = result.scalar_one_or_none()
        else:
            project = await self._session.get(Project, project_id)
        if project is None:
            raise ProjectNotFound("project was not found")
        policy = None
        if project.current_policy_version is not None:
            policy = await self._session.get(
                ProjectPolicyVersion, (project.id, project.current_policy_version)
            )
            if policy is None:
                raise ProjectRepositoryError("current project policy is unavailable")
        return _project_from_record(project, policy=policy)

    async def list(self) -> Sequence[ProjectRecord]:
        """Return projects in deterministic identity order."""

        result = await self._session.execute(select(Project).order_by(Project.name, Project.id))
        records: list[ProjectRecord] = []
        for project in result.scalars().all():
            policy = None
            if project.current_policy_version is not None:
                policy = await self._session.get(
                    ProjectPolicyVersion, (project.id, project.current_policy_version)
                )
                if policy is None:
                    raise ProjectRepositoryError("current project policy is unavailable")
            records.append(_project_from_record(project, policy=policy))
        return records

    async def append_policy(
        self,
        *,
        project_id: UUID,
        expected_policy_version: int,
        policy_digest: str,
        policy_document: Mapping[str, object],
        document_schema_version: int = 1,
    ) -> ProjectPolicyRecord:
        """Append version N+1 while locking the project identity row."""

        _validate_digest(policy_digest)
        _validate_policy_version(document_schema_version)
        result = await self._session.execute(
            select(Project).where(Project.id == project_id).with_for_update()
        )
        project = result.scalar_one_or_none()
        if project is None:
            raise ProjectNotFound("project was not found")
        current = project.current_policy_version
        if current is None or expected_policy_version != current:
            raise PolicyVersionConflict("expected policy version is stale")
        version = current + 1
        policy = ProjectPolicyVersion(
            project_id=project_id,
            version=version,
            policy_digest=policy_digest,
            document_schema_version=document_schema_version,
            document=dict(policy_document),
        )
        self._session.add(policy)
        await self._session.flush()
        project.current_policy_version = version
        await self._session.flush()
        return _policy_from_record(policy)

    async def get_policy(
        self, project_id: UUID, version: int, *, for_update: bool = False
    ) -> ProjectPolicyRecord:
        """Load one immutable policy version without permitting mutation."""

        statement = select(ProjectPolicyVersion).where(
            ProjectPolicyVersion.project_id == project_id,
            ProjectPolicyVersion.version == version,
        )
        if for_update:
            statement = statement.with_for_update()
        policy = (await self._session.execute(statement)).scalar_one_or_none()
        if policy is None:
            raise PolicyNotFound("project policy was not found")
        return _policy_from_record(policy)


def _project_from_record(project: Project, *, policy: ProjectPolicyVersion | None) -> ProjectRecord:
    return ProjectRecord(
        id=project.id,
        name=project.name,
        canonical_path=project.canonical_path,
        canonical_path_key=project.canonical_path_key,
        github_repository=project.github_repository,
        default_branch=project.default_branch,
        instructions_path=project.instructions_path,
        current_policy_version=project.current_policy_version,
        policy=None if policy is None else _policy_from_record(policy),
    )


def _policy_from_record(policy: ProjectPolicyVersion) -> ProjectPolicyRecord:
    if not isinstance(policy.document, Mapping):
        raise ProjectRepositoryError("stored project policy is malformed")
    return ProjectPolicyRecord(
        project_id=policy.project_id,
        version=policy.version,
        policy_digest=policy.policy_digest,
        document_schema_version=policy.document_schema_version,
        document=dict(policy.document),
        created_at=policy.created_at,
    )


def _validate_digest(value: str) -> None:
    if _DIGEST.fullmatch(value) is None:
        raise ValueError("policy digest must be a lowercase SHA-256")


def _validate_policy_version(value: int) -> None:
    if type(value) is not int or value < 1:
        raise ValueError("policy schema version must be positive")


def _normalize_repository(value: str) -> str:
    normalized = value.strip().casefold()
    if not normalized or "/" not in normalized:
        raise ValueError("GitHub repository identity is malformed")
    return normalized


__all__ = [
    "PolicyNotFound",
    "PolicyVersionConflict",
    "PostgresProjectRepository",
    "ProjectIdentityConflict",
    "ProjectNotFound",
    "ProjectRepositoryError",
]
