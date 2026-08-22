from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self
from uuid import UUID, uuid4

import pytest
from forge.application.ports.audit import OperatorAuditRecord
from forge.application.ports.mutations import ApiMutationRecord
from forge.application.ports.projects import (
    ProjectPolicyRecord,
    ProjectRecord,
    RepositoryInspection,
)
from forge.application.ports.tasks import TaskRecord
from forge.application.services.auth import AuthenticatedActor
from forge.application.services.projects import (
    PolicyUpdateRequest,
    ProjectRegistrationRequest,
    ProjectService,
)
from forge.application.services.tasks import (
    ExternalTaskRequest,
    PlainTextTaskRequest,
    TaskService,
)
from forge.domain.policy import DatabaseProvisioningPolicy
from pydantic import ValidationError

ACTOR = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())


@dataclass
class FakeMutations:
    receipts: dict[tuple[UUID, str, str], ApiMutationRecord] = field(default_factory=dict)

    async def reserve(
        self, *, actor_id: UUID, action: str, scope: str, idempotency_key: str, request_digest: str
    ) -> ApiMutationRecord:
        import hashlib

        key_hash = hashlib.sha256(idempotency_key.encode()).hexdigest()
        key = (actor_id, action, key_hash)
        existing = self.receipts.get(key)
        if existing is not None:
            if existing.request_digest != request_digest or existing.scope != scope:
                from forge.persistence.repositories.mutations import MutationConflict

                raise MutationConflict("idempotency key was reused for a different request")
            return ApiMutationRecord(
                id=existing.id,
                actor_id=existing.actor_id,
                action=existing.action,
                scope=existing.scope,
                key_hash=existing.key_hash,
                request_digest=existing.request_digest,
                lifecycle_state=existing.lifecycle_state,
                response_status=existing.response_status,
                response_payload=existing.response_payload,
                resource_kind=existing.resource_kind,
                resource_id=existing.resource_id,
                is_replay=True,
            )
        receipt = ApiMutationRecord(
            id=uuid4(),
            actor_id=actor_id,
            action=action,
            scope=scope,
            key_hash=key_hash,
            request_digest=request_digest,
            lifecycle_state="RESERVED",
            response_status=None,
            response_payload=None,
            resource_kind=None,
            resource_id=None,
            is_replay=False,
        )
        self.receipts[key] = receipt
        return receipt

    async def complete(
        self,
        mutation_id: UUID,
        *,
        response_status: int,
        response_payload: dict[str, object],
        resource_kind: str | None = None,
        resource_id: UUID | None = None,
    ) -> ApiMutationRecord:
        for key, receipt in self.receipts.items():
            if receipt.id == mutation_id:
                completed = ApiMutationRecord(
                    id=receipt.id,
                    actor_id=receipt.actor_id,
                    action=receipt.action,
                    scope=receipt.scope,
                    key_hash=receipt.key_hash,
                    request_digest=receipt.request_digest,
                    lifecycle_state="COMPLETED",
                    response_status=response_status,
                    response_payload=dict(response_payload),
                    resource_kind=resource_kind,
                    resource_id=resource_id,
                    is_replay=False,
                )
                self.receipts[key] = completed
                return completed
        raise AssertionError("unknown receipt")


@dataclass
class FakeProjects:
    records: dict[UUID, ProjectRecord] = field(default_factory=dict)
    audit_count: int = 0

    async def create(self, **kwargs: Any) -> ProjectRecord:
        project_id = kwargs["project_id"]
        policy = ProjectPolicyRecord(
            project_id=project_id,
            version=1,
            policy_digest=kwargs["policy_digest"],
            document_schema_version=1,
            document=dict(kwargs["policy_document"]),
        )
        project = ProjectRecord(
            id=project_id,
            name=kwargs["name"],
            canonical_path=kwargs["canonical_path"],
            canonical_path_key=kwargs["canonical_path_key"],
            github_repository=kwargs["github_repository"],
            default_branch=kwargs["default_branch"],
            instructions_path=kwargs.get("instructions_path"),
            current_policy_version=1,
            policy=policy,
        )
        self.records[project_id] = project
        return project

    async def get(self, project_id: UUID, *, for_update: bool = False) -> ProjectRecord:
        del for_update
        return self.records[project_id]

    async def list(self) -> list[ProjectRecord]:
        return list(self.records.values())

    async def append_policy(
        self,
        *,
        project_id: UUID,
        expected_policy_version: int,
        policy_digest: str,
        policy_document: dict[str, object],
        document_schema_version: int = 1,
    ) -> ProjectPolicyRecord:
        current = self.records[project_id]
        assert current.current_policy_version == expected_policy_version
        policy = ProjectPolicyRecord(
            project_id=project_id,
            version=expected_policy_version + 1,
            policy_digest=policy_digest,
            document_schema_version=document_schema_version,
            document=dict(policy_document),
        )
        self.records[project_id] = ProjectRecord(
            id=current.id,
            name=current.name,
            canonical_path=current.canonical_path,
            canonical_path_key=current.canonical_path_key,
            github_repository=current.github_repository,
            default_branch=current.default_branch,
            instructions_path=current.instructions_path,
            current_policy_version=policy.version,
            policy=policy,
        )
        return policy

    async def get_policy(
        self, project_id: UUID, version: int, *, for_update: bool = False
    ) -> ProjectPolicyRecord:
        del for_update
        policy = self.records[project_id].policy
        assert policy is not None and policy.version == version
        return policy


@dataclass
class FakeTasks:
    records: dict[UUID, TaskRecord] = field(default_factory=dict)

    async def create(self, **kwargs: Any) -> TaskRecord:
        from forge.persistence.repositories.tasks import compute_task_digest, derive_normalized_text

        task = TaskRecord(
            id=kwargs["task_id"],
            project_id=kwargs["project_id"],
            title=kwargs["title"],
            body=kwargs["body"],
            source_url=kwargs.get("source_url"),
            source_updated_at=kwargs.get("source_updated_at"),
            untrusted_external_content=kwargs.get("external_source") is not None,
            normalized_text=derive_normalized_text(kwargs["title"], kwargs["body"]),
            task_digest=compute_task_digest(
                title=kwargs["title"],
                body=kwargs["body"],
                source_url=kwargs.get("source_url"),
                source_updated_at=kwargs.get("source_updated_at"),
                external_source=kwargs.get("external_source"),
                external_id=kwargs.get("external_id"),
            ),
            external_source=kwargs.get("external_source"),
            external_id=kwargs.get("external_id"),
        )
        self.records[task.id] = task
        return task

    async def get(self, task_id: UUID) -> TaskRecord:
        return self.records[task_id]

    async def list(self, project_id: UUID) -> list[TaskRecord]:
        return [record for record in self.records.values() if record.project_id == project_id]


@dataclass
class FakeAudit:
    records: list[OperatorAuditRecord] = field(default_factory=list)

    async def append(self, **kwargs: Any) -> OperatorAuditRecord:
        record = OperatorAuditRecord(
            id=uuid4(),
            correlation_id=kwargs.pop("correlation_id", uuid4()),
            created_at=datetime.now(UTC),
            schema_version=kwargs.pop("schema_version", 1),
            **kwargs,
        )
        self.records.append(record)
        return record

    async def list_for_subject(
        self, *, subject_type: str, subject_id: UUID
    ) -> list[OperatorAuditRecord]:
        return [
            record
            for record in self.records
            if record.subject_type == subject_type and record.subject_id == subject_id
        ]


@dataclass
class FakeUow:
    projects: FakeProjects
    tasks: FakeTasks
    mutations: FakeMutations
    audit: FakeAudit
    committed: bool = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.committed = False


class FakeInspector:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.calls: list[dict[str, str]] = []

    def inspect(self, **kwargs: str) -> RepositoryInspection:
        self.calls.append(kwargs)
        return RepositoryInspection(
            canonical_path=str(self.path.resolve()),
            github_repository="owner/repo",
            default_branch="main",
            base_ref="refs/heads/main",
            base_sha="a" * 40,
        )


def _registration(
    path: Path, *, database: DatabaseProvisioningPolicy | None = None
) -> ProjectRegistrationRequest:
    return ProjectRegistrationRequest(
        name="Forge",
        repository_path=str(path),
        github_repository="Owner/Repo",
        default_branch="main",
        database=database or DatabaseProvisioningPolicy(enabled=False),
    )


@pytest.mark.asyncio
async def test_project_registration_is_atomic_hashed_and_does_not_resolve_database_secret(
    tmp_path: Path,
) -> None:
    path = tmp_path / "repo"
    path.mkdir()
    inspector = FakeInspector(path)
    projects = FakeProjects()
    uow = FakeUow(projects, FakeTasks(), FakeMutations(), FakeAudit())
    resolver_called = False

    def resolver(_: str) -> str:
        nonlocal resolver_called
        resolver_called = True
        raise AssertionError("database secrets must not be resolved")

    service = ProjectService(
        lambda: uow,
        repository_inspector=inspector,
        data_root=tmp_path / "data",
        secret_resolver=resolver,
    )
    (tmp_path / "data").mkdir()
    project = await service.register(
        actor=ACTOR, idempotency_key="project-1", request=_registration(path)
    )

    assert project.current_policy_version == 1
    assert project.canonical_path == str(path.resolve())
    assert len(uow.audit.records) == 1
    assert uow.audit.records[0].payload["request_digest"]
    assert resolver_called is False

    replay = await service.register(
        actor=ACTOR, idempotency_key="project-1", request=_registration(path)
    )
    assert replay.id == project.id
    assert len(uow.audit.records) == 1


def test_project_registration_validation_is_closed_and_database_secret_is_reference_only() -> None:
    valid = _registration(
        Path("C:/repo"),
        database=DatabaseProvisioningPolicy(
            enabled=True, admin_url_secret_reference="secret://forge/admin"
        ),
    )
    assert valid.database.admin_url_secret_reference == "secret://forge/admin"
    with pytest.raises(ValidationError):
        ProjectRegistrationRequest(
            name="Forge",
            repository_path="C:/repo",
            github_repository="owner/repo",
            default_branch="main",
            database={"enabled": True},
        )
    with pytest.raises(ValidationError):
        ProjectRegistrationRequest(
            name="Forge",
            repository_path="C:/repo",
            github_repository="owner/repo",
            default_branch="main",
            commands=[
                {
                    "kind": "test",
                    "name": "bad",
                    "argv": ["sh", "-c", "echo unsafe"],
                    "timeout_seconds": 10,
                }
            ],
        )


@pytest.mark.asyncio
async def test_enabled_database_policy_persists_only_secret_reference(tmp_path: Path) -> None:
    path = tmp_path / "repo"
    path.mkdir()
    (tmp_path / "data").mkdir()
    projects = FakeProjects()
    uow = FakeUow(projects, FakeTasks(), FakeMutations(), FakeAudit())
    service = ProjectService(
        lambda: uow,
        repository_inspector=FakeInspector(path),
        data_root=tmp_path / "data",
        secret_resolver=lambda _: (_ for _ in ()).throw(AssertionError("must not resolve")),
    )
    project = await service.register(
        actor=ACTOR,
        idempotency_key="enabled-db-project",
        request=_registration(
            path,
            database=DatabaseProvisioningPolicy(
                enabled=True, admin_url_secret_reference="secret://forge/admin"
            ),
        ),
    )
    assert project.policy is not None
    database = project.policy.document["database"]
    assert isinstance(database, dict)
    assert database["admin_url_secret_reference"] == "secret://forge/admin"


@pytest.mark.asyncio
async def test_policy_update_appends_and_same_key_replays_without_second_audit(
    tmp_path: Path,
) -> None:
    path = tmp_path / "repo"
    path.mkdir()
    projects = FakeProjects()
    inspector = FakeInspector(path)
    uow = FakeUow(projects, FakeTasks(), FakeMutations(), FakeAudit())
    service = ProjectService(lambda: uow, repository_inspector=inspector, data_root=tmp_path)
    project = await service.register(
        actor=ACTOR, idempotency_key="register", request=_registration(path)
    )

    updated = await service.update_policy(
        actor=ACTOR,
        project_id=project.id,
        idempotency_key="policy-1",
        request=PolicyUpdateRequest(expected_policy_version=1, trusted_project=True),
    )
    assert updated.version == 2
    replay = await service.update_policy(
        actor=ACTOR,
        project_id=project.id,
        idempotency_key="policy-1",
        request=PolicyUpdateRequest(expected_policy_version=1, trusted_project=True),
    )
    assert replay.version == 2
    assert len(uow.audit.records) == 2


@pytest.mark.asyncio
async def test_policy_update_requires_exact_version_and_cannot_change_identity(
    tmp_path: Path,
) -> None:
    path = tmp_path / "repo"
    path.mkdir()
    projects = FakeProjects()
    uow = FakeUow(projects, FakeTasks(), FakeMutations(), FakeAudit())
    service = ProjectService(
        lambda: uow,
        repository_inspector=FakeInspector(path),
        data_root=tmp_path,
    )
    project = await service.register(
        actor=ACTOR, idempotency_key="register-stale", request=_registration(path)
    )
    await service.update_policy(
        actor=ACTOR,
        project_id=project.id,
        idempotency_key="policy-current",
        request=PolicyUpdateRequest(expected_policy_version=1, trusted_project=True),
    )
    from forge.persistence.repositories.projects import PolicyVersionConflict

    with pytest.raises(PolicyVersionConflict):
        await service.update_policy(
            actor=ACTOR,
            project_id=project.id,
            idempotency_key="policy-stale",
            request=PolicyUpdateRequest(expected_policy_version=1, trusted_project=False),
        )
    with pytest.raises(ValidationError):
        PolicyUpdateRequest(expected_policy_version=2, default_branch="release")


@pytest.mark.asyncio
async def test_task_service_preserves_exact_source_and_rejects_changed_same_key() -> None:
    uow = FakeUow(FakeProjects(), FakeTasks(), FakeMutations(), FakeAudit())
    project_id = uuid4()
    uow.projects.records[project_id] = ProjectRecord(
        id=project_id,
        name="P",
        canonical_path="/repo",
        canonical_path_key="/repo",
        github_repository="owner/repo",
        default_branch="main",
        instructions_path=None,
        current_policy_version=1,
    )
    service = TaskService(lambda: uow)
    request = PlainTextTaskRequest(project_id=project_id, title="T\r\nitle", body="B\r\nody")
    first = await service.create_plain_text(actor=ACTOR, idempotency_key="task-1", request=request)
    assert first.title == request.title
    assert first.body == request.body
    assert first.normalized_text == "T\nitle\n\nB\nody"
    assert len(uow.audit.records) == 1

    replay = await service.create_plain_text(actor=ACTOR, idempotency_key="task-1", request=request)
    assert replay.id == first.id
    assert len(uow.audit.records) == 1

    from forge.persistence.repositories.mutations import MutationConflict

    with pytest.raises(MutationConflict):
        await service.create_plain_text(
            actor=ACTOR,
            idempotency_key="task-1",
            request=PlainTextTaskRequest(project_id=project_id, title="T", body="changed"),
        )


@pytest.mark.asyncio
async def test_task_external_identity_is_project_scoped() -> None:
    uow = FakeUow(FakeProjects(), FakeTasks(), FakeMutations(), FakeAudit())
    project_id = uuid4()
    uow.projects.records[project_id] = ProjectRecord(
        id=project_id,
        name="P",
        canonical_path="/repo",
        canonical_path_key="/repo",
        github_repository="owner/repo",
        default_branch="main",
        instructions_path=None,
        current_policy_version=1,
    )
    other_project_id = uuid4()
    uow.projects.records[other_project_id] = ProjectRecord(
        id=other_project_id,
        name="Q",
        canonical_path="/other",
        canonical_path_key="/other",
        github_repository="owner/other",
        default_branch="main",
        instructions_path=None,
        current_policy_version=1,
    )
    service = TaskService(lambda: uow)
    first = await service.create_from_external(
        actor=ACTOR,
        idempotency_key="external-1",
        request=ExternalTaskRequest(
            project_id=project_id,
            external_source="github",
            external_id="4",
            title="T",
            body="B",
            source_url="https://example.test/4",
            source_updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        ),
    )
    second = await service.create_from_external(
        actor=ACTOR,
        idempotency_key="external-2",
        request=ExternalTaskRequest(
            project_id=other_project_id,
            external_source="github",
            external_id="4",
            title="T",
            body="B",
            source_url="https://example.test/4",
            source_updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        ),
    )
    assert first.task_digest == second.task_digest
