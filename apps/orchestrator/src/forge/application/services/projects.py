"""Project registration and immutable policy-version application services."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol, Self
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from forge.application.adapters.git import LocalGitRepositoryInspector, canonical_path_key
from forge.application.ports.audit import AuditRepository
from forge.application.ports.mutations import ApiMutationRecord, MutationRepository
from forge.application.ports.projects import (
    ProjectPolicyRecord,
    ProjectRecord,
    ProjectRepository,
    RepositoryInspection,
    RepositoryInspector,
)
from forge.application.services.auth import AuthenticatedActor
from forge.domain.policy import (
    AgentModelPolicy,
    CommandSpec,
    DatabaseProvisioningPolicy,
    ProjectPolicy,
    RunnerMode,
)
from forge.domain.review import FindingSeverity
from forge.persistence.repositories.projects import PolicyVersionConflict
from forge.settings import Settings


class ProjectServiceError(RuntimeError):
    """A bounded project application failure."""


class ProjectUnitOfWork(Protocol):
    projects: ProjectRepository
    mutations: MutationRepository
    audit: AuditRepository

    async def __aenter__(self) -> Self: ...

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None: ...

    async def commit(self) -> None: ...


class ProjectRegistrationRequest(BaseModel):
    """Closed registration input shared by later HTTP schemas."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    repository_path: str = Field(min_length=1)
    github_repository: str = Field(min_length=1, max_length=512)
    default_branch: str = Field(min_length=1, max_length=255)
    instructions_path: str | None = None
    runner_mode: RunnerMode = RunnerMode.DOCKER
    trusted_project: bool = False
    local_remediation_limit: int = Field(default=3, ge=0, le=20)
    remote_remediation_limit: int = Field(default=3, ge=0, le=20)
    planner_model: AgentModelPolicy = Field(default_factory=AgentModelPolicy)
    developer_model: AgentModelPolicy = Field(default_factory=AgentModelPolicy)
    reviewer_model: AgentModelPolicy = Field(default_factory=AgentModelPolicy)
    database: DatabaseProvisioningPolicy = Field(default_factory=DatabaseProvisioningPolicy)
    commands: tuple[CommandSpec, ...] = ()
    allowed_environment_files: tuple[str, ...] = ()
    secret_paths: tuple[str, ...] = (".env", ".env.local")
    allowed_merge_methods: tuple[str, ...] = ("squash",)
    publication_blocking_severities: frozenset[FindingSeverity] = frozenset(
        {FindingSeverity.BLOCKER, FindingSeverity.MAJOR}
    )
    merge_blocking_severities: frozenset[FindingSeverity] = frozenset(
        {FindingSeverity.BLOCKER, FindingSeverity.MAJOR}
    )

    @field_validator("name")
    @classmethod
    def name_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("project name must not be blank")
        return value


# Short aliases keep the application boundary pleasant for non-HTTP callers.
ProjectRegistration = ProjectRegistrationRequest


class PolicyUpdateRequest(BaseModel):
    """Only mutable policy fields may be supplied for a new policy version."""

    model_config = ConfigDict(extra="forbid")

    expected_policy_version: int = Field(ge=1)
    runner_mode: RunnerMode | None = None
    trusted_project: bool | None = None
    local_remediation_limit: int | None = Field(default=None, ge=0, le=20)
    remote_remediation_limit: int | None = Field(default=None, ge=0, le=20)
    planner_model: AgentModelPolicy | None = None
    developer_model: AgentModelPolicy | None = None
    reviewer_model: AgentModelPolicy | None = None
    database: DatabaseProvisioningPolicy | None = None
    commands: tuple[CommandSpec, ...] | None = None
    allowed_environment_files: tuple[str, ...] | None = None
    secret_paths: tuple[str, ...] | None = None
    allowed_merge_methods: tuple[str, ...] | None = None
    publication_blocking_severities: frozenset[FindingSeverity] | None = None
    merge_blocking_severities: frozenset[FindingSeverity] | None = None


PolicyUpdate = PolicyUpdateRequest


class ProjectService:
    """Coordinate repository inspection, policy persistence, receipts, and audit."""

    def __init__(
        self,
        unit_of_work_factory: Callable[[], ProjectUnitOfWork],
        *,
        repository_inspector: RepositoryInspector | None = None,
        settings: Settings | None = None,
        data_root: str | Path | None = None,
        secret_resolver: object | None = None,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._repository_inspector = repository_inspector or LocalGitRepositoryInspector()
        resolved_settings = settings
        configured_root: str | Path
        if resolved_settings is not None:
            configured_root = resolved_settings.data_root
        elif data_root is not None:
            configured_root = data_root
        else:
            configured_root = Settings().data_root
        self._data_root = str(configured_root)
        # Kept as an explicit constructor dependency to make the no-resolution
        # guarantee testable.  Validation never invokes it.
        self._secret_resolver = secret_resolver

    async def register(
        self,
        *,
        actor: AuthenticatedActor,
        idempotency_key: str,
        request: ProjectRegistrationRequest,
    ) -> ProjectRecord:
        request = _coerce_request(request, ProjectRegistrationRequest)
        request_digest = _digest(request.model_dump(mode="json"))
        async with self._unit_of_work_factory() as work:
            receipt = await work.mutations.reserve(
                actor_id=actor.actor_id,
                action="project.register",
                scope="projects",
                idempotency_key=idempotency_key,
                request_digest=request_digest,
            )
            if receipt.is_replay:
                project_id = _resource_id(receipt, "project")
                project = await work.projects.get(project_id)
                version = _response_version(receipt)
                if project.policy is None or project.policy.version != version:
                    policy = await work.projects.get_policy(project_id, version)
                    project = ProjectRecord(
                        id=project.id,
                        name=project.name,
                        canonical_path=project.canonical_path,
                        canonical_path_key=project.canonical_path_key,
                        github_repository=project.github_repository,
                        default_branch=project.default_branch,
                        instructions_path=project.instructions_path,
                        current_policy_version=version,
                        policy=policy,
                    )
                await work.commit()
                return project

            inspection = self._repository_inspector.inspect(
                repository_path=request.repository_path,
                data_root=self._data_root,
                github_repository=request.github_repository,
                default_branch=request.default_branch,
            )
            project_id = uuid4()
            registration_policy = _registration_policy(
                request, project_id=project_id, inspection=inspection
            )
            policy_document = registration_policy.model_dump(mode="json")
            policy_digest = _digest(policy_document)
            project = await work.projects.create(
                project_id=project_id,
                name=request.name,
                canonical_path=inspection.canonical_path,
                canonical_path_key=canonical_path_key(inspection.canonical_path),
                github_repository=inspection.github_repository,
                default_branch=inspection.default_branch,
                instructions_path=request.instructions_path,
                policy_digest=policy_digest,
                policy_document=policy_document,
            )
            await work.audit.append(
                actor_id=actor.actor_id,
                event_type="project.registered",
                subject_type="project",
                subject_id=project_id,
                correlation_id=receipt.id,
                payload={
                    "request_digest": request_digest,
                    "policy_digest": policy_digest,
                    "repository_identity_digest": _digest(
                        {
                            "canonical_path": inspection.canonical_path,
                            "github_repository": inspection.github_repository,
                            "default_branch": inspection.default_branch,
                        }
                    ),
                },
            )
            await work.mutations.complete(
                receipt.id,
                response_status=201,
                response_payload={"id": str(project_id), "policy_version": 1},
                resource_kind="project",
                resource_id=project_id,
            )
            await work.commit()
            return project

    async def update_policy(
        self,
        *,
        actor: AuthenticatedActor,
        project_id: UUID,
        idempotency_key: str,
        request: PolicyUpdateRequest,
    ) -> ProjectPolicyRecord:
        request = _coerce_request(request, PolicyUpdateRequest)
        request_digest = _digest(
            {"project_id": str(project_id), "request": request.model_dump(mode="json")}
        )
        async with self._unit_of_work_factory() as work:
            receipt = await work.mutations.reserve(
                actor_id=actor.actor_id,
                action="project.policy.update",
                scope=f"project:{project_id}",
                idempotency_key=idempotency_key,
                request_digest=request_digest,
            )
            if receipt.is_replay:
                version = _response_version(receipt)
                replayed_policy = await work.projects.get_policy(project_id, version)
                await work.commit()
                return replayed_policy

            project = await work.projects.get(project_id, for_update=True)
            current = project.policy
            if current is None or project.current_policy_version is None:
                raise ProjectServiceError("project policy is unavailable")
            if request.expected_policy_version != project.current_policy_version:
                raise PolicyVersionConflict("expected policy version is stale")
            next_version = project.current_policy_version + 1
            policy: ProjectPolicy = _updated_policy(project, current, request, version=next_version)
            document = policy.model_dump(mode="json")
            digest = _digest(document)
            appended = await work.projects.append_policy(
                project_id=project_id,
                expected_policy_version=request.expected_policy_version,
                policy_digest=digest,
                policy_document=document,
            )
            await work.audit.append(
                actor_id=actor.actor_id,
                event_type="project.policy_updated",
                subject_type="project",
                subject_id=project_id,
                correlation_id=receipt.id,
                payload={
                    "request_digest": request_digest,
                    "policy_digest": digest,
                    "expected_policy_version": request.expected_policy_version,
                    "policy_version": appended.version,
                },
            )
            await work.mutations.complete(
                receipt.id,
                response_status=201,
                response_payload={
                    "project_id": str(project_id),
                    "policy_version": appended.version,
                },
                resource_kind="project_policy",
                resource_id=project_id,
            )
            await work.commit()
            return appended

    async def list(self) -> Sequence[ProjectRecord]:
        async with self._unit_of_work_factory() as work:
            records = await work.projects.list()
            await work.commit()
            return records

    async def get(self, project_id: UUID) -> ProjectRecord:
        async with self._unit_of_work_factory() as work:
            record = await work.projects.get(project_id)
            await work.commit()
            return record


def _registration_policy(
    request: ProjectRegistrationRequest,
    *,
    project_id: UUID,
    inspection: RepositoryInspection,
) -> ProjectPolicy:
    values = request.model_dump(mode="python")
    values.pop("name", None)
    values.pop("instructions_path", None)
    values["id"] = project_id
    values["version"] = 1
    values["repository_path"] = inspection.canonical_path
    values["github_repository"] = inspection.github_repository
    values["default_branch"] = inspection.default_branch
    return ProjectPolicy.model_validate(values)


def _updated_policy(
    project: ProjectRecord,
    current: ProjectPolicyRecord,
    request: PolicyUpdateRequest,
    *,
    version: int,
) -> ProjectPolicy:
    values: dict[str, object] = dict(current.document)
    values.update(
        {
            "id": project.id,
            "version": version,
            "repository_path": project.canonical_path,
            "github_repository": project.github_repository,
            "default_branch": project.default_branch,
        }
    )
    values.update(
        request.model_dump(mode="python", exclude_unset=True, exclude={"expected_policy_version"})
    )
    return ProjectPolicy.model_validate(values)


def _coerce_request(value: object, model: type[BaseModel]) -> Any:
    if isinstance(value, model):
        return value
    if isinstance(value, Mapping):
        return model.model_validate(value)
    raise TypeError("request must be a validated project request")


def _resource_id(receipt: ApiMutationRecord, expected_kind: str) -> UUID:
    if receipt.resource_kind != expected_kind or receipt.resource_id is None:
        raise ProjectServiceError("mutation receipt resource is unavailable")
    return receipt.resource_id


def _response_version(receipt: ApiMutationRecord) -> int:
    payload = receipt.response_payload
    value = None if payload is None else payload.get("policy_version")
    if type(value) is not int or value < 1:
        raise ProjectServiceError("mutation receipt response is unavailable")
    return value


def _digest(value: object) -> str:
    encoded = json.dumps(
        _canonicalize(value), ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _canonicalize(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): _canonicalize(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonicalize(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_canonicalize(item) for item in value), key=lambda item: repr(item))
    return value


__all__ = [
    "PolicyUpdate",
    "PolicyUpdateRequest",
    "ProjectRegistration",
    "ProjectRegistrationRequest",
    "ProjectService",
    "ProjectServiceError",
]
