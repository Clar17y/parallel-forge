from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Self
from uuid import UUID

import pytest
from forge.application.ports.artifacts import ArtifactDescriptor
from forge.application.ports.projects import ProjectPolicyRecord, ProjectRecord
from forge.application.ports.repository import (
    FileRead,
    FileWrite,
    InstructionDocument,
    RepositoryEntry,
    SearchMatch,
)
from forge.application.ports.tools import (
    ToolCallRecord,
)
from forge.application.ports.worktrees import ManagedWorktree
from forge.application.services.recovery import OperationExecutor
from forge.application.services.tools import ControlledToolService, ToolInvocationError
from forge.domain.actor import AgentRole
from forge.domain.event import RunEvent
from forge.domain.operation import (
    OperationIntent,
    OperationOutcome,
    OperationStatus,
    thaw_payload,
)
from forge.domain.policy import AgentModelPolicy, ProjectPolicy
from forge.domain.resource import WorktreeIdentity
from forge.domain.run import RunSnapshot, RunState
from forge.domain.tool import (
    ToolAuthorizationContext,
    ToolCallStatus,
    ToolErrorCode,
    ToolName,
    ToolRequest,
    repository_resource_identity,
)
from forge.observability.redaction import Redactor
from forge.tools.repository import RepositoryReader

PROJECT_ID = UUID("11111111-1111-4111-8111-111111111111")
RUN_ID = UUID("22222222-2222-4222-8222-222222222222")
TASK_ID = UUID("33333333-3333-4333-8333-333333333333")
EXECUTION_ID = UUID("44444444-4444-4444-8444-444444444444")
STEP_ID = UUID("55555555-5555-4555-8555-555555555555")
BASE_SHA = "0123456789abcdef0123456789abcdef01234567"


@dataclass
class _Runs:
    run: RunSnapshot | None

    async def get_for_update(self, run_id: UUID) -> RunSnapshot:
        if self.run is None or run_id != self.run.id:
            raise KeyError(f"run {run_id} not found")
        return self.run


@dataclass
class _Projects:
    project: ProjectRecord

    async def get(self, project_id: UUID, *, for_update: bool = False) -> ProjectRecord:
        assert for_update is True
        assert project_id == self.project.id
        return self.project


class _ToolCalls:
    def __init__(self) -> None:
        self.records: list[ToolCallRecord] = []
        self.current = True
        self.execution_count = 0
        self.record_error: Exception | None = None

    async def validate_execution_context(self, *_: object) -> bool:
        return self.current

    async def count_for_execution(self, _: UUID) -> int:
        return self.execution_count

    async def record(self, record: ToolCallRecord) -> ToolCallRecord:
        if self.record_error is not None:
            raise self.record_error
        self.records.append(record)
        return record

    async def reserve(self, record: ToolCallRecord) -> ToolCallRecord:
        for i, item in enumerate(self.records):
            if item.id == record.id:
                self.records[i] = record
                return record
        self.records.append(record)
        return record

    async def finalize(self, record: ToolCallRecord) -> ToolCallRecord:
        for i, item in enumerate(self.records):
            if item.id == record.id:
                self.records[i] = record
                return record
        self.records.append(record)
        return record

    async def get(self, tool_call_id: UUID) -> ToolCallRecord:
        for item in reversed(self.records):
            if item.id == tool_call_id:
                return item
        raise KeyError(f"tool call {tool_call_id} not found")

    async def find(self, tool_call_id: UUID) -> ToolCallRecord | None:
        try:
            return await self.get(tool_call_id)
        except KeyError:
            return None

    async def list_for_run(self, run_id: UUID) -> Sequence[ToolCallRecord]:
        return [record for record in self.records if record.run_id == run_id]


class _Events:
    def __init__(self) -> None:
        self.events: list[RunEvent] = []
        self.append_error: Exception | None = None

    async def append(self, event: RunEvent) -> RunEvent:
        if self.append_error is not None:
            raise self.append_error
        self.events.append(event)
        return event


class _Artifacts:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    async def record(
        self,
        descriptor: ArtifactDescriptor,
        *,
        run_id: UUID,
        producer_type: str,
        producer_id: UUID | None = None,
        parent_digests: Sequence[str] = (),
        metadata: Mapping[str, object] | None = None,
    ) -> ArtifactDescriptor:
        self.records.append(
            {
                "descriptor": descriptor,
                "run_id": run_id,
                "producer_type": producer_type,
                "producer_id": producer_id,
                "parent_digests": parent_digests,
                "metadata": metadata,
            }
        )
        return replace(
            descriptor,
            producer_type=producer_type,
            producer_id=producer_id,
            run_id=run_id,
            parent_digests=tuple(parent_digests),
            metadata=dict(metadata or {}),
        )

    async def get_by_digest(self, digest: str, *, run_id: UUID) -> ArtifactDescriptor:
        for item in self.records:
            descriptor = item["descriptor"]
            if descriptor.digest == digest and item["run_id"] == run_id:
                return replace(
                    descriptor,
                    producer_type=item["producer_type"],
                    producer_id=item["producer_id"],
                    run_id=run_id,
                    parent_digests=tuple(item["parent_digests"]),
                    metadata=dict(item["metadata"] or {}),
                )
        raise KeyError(digest)


class _UnitOfWork:
    def __init__(self, run: RunSnapshot | None, project: ProjectRecord) -> None:
        self.runs = _Runs(run)
        self.projects = _Projects(project)
        self.tool_calls = _ToolCalls()
        self.events = _Events()
        self.artifacts = _Artifacts()
        self.committed = False
        self.rolled_back = False
        self.commit_error: Exception | None = None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def commit(self) -> None:
        if self.commit_error is not None:
            raise self.commit_error
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True


class _TrackingReader:
    def __init__(self, reader: RepositoryReader) -> None:
        self._reader = reader
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.error: Exception | None = None

    @property
    def root(self) -> Any:
        return self._reader.root

    def list_files(self, path: str = ".") -> Sequence[RepositoryEntry]:
        self.calls.append(("list_files", (path,)))
        if self.error is not None:
            raise self.error
        return self._reader.list_files(path)

    def read_file(self, path: str) -> FileRead:
        self.calls.append(("read_file", (path,)))
        if self.error is not None:
            raise self.error
        return self._reader.read_file(path)

    def search(self, literal: str, path: str = ".") -> Sequence[SearchMatch]:
        self.calls.append(("search", (literal, path)))
        if self.error is not None:
            raise self.error
        return self._reader.search(literal, path)

    def read_instructions(self, target_path: str = ".") -> Sequence[InstructionDocument]:
        self.calls.append(("read_instructions", (target_path,)))
        if self.error is not None:
            raise self.error
        return self._reader.read_instructions(target_path)

    def excludes_paths(self, paths: Sequence[str]) -> bool:
        return self._reader.excludes_paths(paths)


class _Git:
    def __init__(self, repository: Path, worktree: ManagedWorktree) -> None:
        self.repository_path = repository
        self._worktree = worktree

    def expected_worktree(self, identity: WorktreeIdentity, base_sha: str) -> ManagedWorktree:
        assert identity == self._worktree.identity
        assert base_sha == self._worktree.base_sha
        return self._worktree


def _project_policy(repository: Path, *, max_tool_calls: int = 100) -> ProjectPolicy:
    return ProjectPolicy(
        id=PROJECT_ID,
        version=1,
        repository_path=str(repository),
        github_repository="Clar17y/forge-test",
        default_branch="main",
        planner_model=AgentModelPolicy(max_tool_calls=max_tool_calls),
        developer_model=AgentModelPolicy(max_tool_calls=max_tool_calls),
        reviewer_model=AgentModelPolicy(max_tool_calls=max_tool_calls),
    )


def _project_record(policy: ProjectPolicy) -> ProjectRecord:
    return ProjectRecord(
        id=PROJECT_ID,
        name="test",
        canonical_path=policy.repository_path,
        canonical_path_key=policy.repository_path,
        github_repository=policy.github_repository,
        default_branch=policy.default_branch,
        instructions_path=None,
        current_policy_version=policy.version,
        policy=ProjectPolicyRecord(
            project_id=PROJECT_ID,
            version=policy.version,
            policy_digest="a" * 64,
            document_schema_version=1,
            document=policy.model_dump(mode="json"),
        ),
    )


def _context(**overrides: object) -> ToolAuthorizationContext:
    values: dict[str, object] = {
        "role": AgentRole.PLANNER,
        "run_id": RUN_ID,
        "worktree_id": repository_resource_identity(PROJECT_ID),
        "policy_version": 1,
        "agent_execution_id": EXECUTION_ID,
        "step_id": STEP_ID,
        "invocation_id": UUID("66666666-6666-4666-8666-666666666666"),
    }
    values.update(overrides)
    return ToolAuthorizationContext(**values)  # type: ignore[arg-type]


def _service(
    tmp_path: Path,
    run: RunSnapshot | None = None,
    *,
    controlled_git: _Git | None = None,
    managed_worktree: ManagedWorktree | None = None,
    project_policy: ProjectPolicy | None = None,
) -> tuple[ControlledToolService, _UnitOfWork, _TrackingReader]:
    selected_policy = project_policy or _project_policy(tmp_path)
    selected_run = (
        run
        if run is not None
        else RunSnapshot(
            id=RUN_ID,
            project_id=PROJECT_ID,
            task_id=TASK_ID,
            state=RunState.PLANNING,
            policy_version=selected_policy.version,
        )
    )
    reader = _TrackingReader(
        RepositoryReader(
            tmp_path,
            secret_paths=selected_policy.effective_secret_paths,
            max_file_bytes=1024,
            force_python_search=True,
        )
    )
    work = _UnitOfWork(selected_run, _project_record(selected_policy))
    return (
        ControlledToolService(
            lambda: work,
            repository_reader=reader,
            controlled_git=controlled_git,
            worktree=managed_worktree,
        ),
        work,
        reader,
    )


# ---------------------------------------------------------------------------
# Acceptance Criterion 1: Successful bound reads with full audit evidence
# ---------------------------------------------------------------------------


async def test_successful_bound_read_creates_complete_matching_record_and_event(
    tmp_path: Path,
) -> None:
    readme = tmp_path / "README.md"
    readme.write_text("# Forge Test Project\nAudited controlled tools.", encoding="utf-8")

    service, work, reader = _service(tmp_path)
    context = _context()
    request = ToolRequest(name=ToolName.REPOSITORY_READ_FILE, arguments={"path": "README.md"})

    result = await service.invoke(context, request)

    # 1. Returned ToolResult structure
    assert result.status is ToolCallStatus.SUCCEEDED
    assert result.error is None
    assert result.tool_name is ToolName.REPOSITORY_READ_FILE
    assert result.tool_call_id is not None
    assert result.tool_call_id.int != 0
    assert result.correlation_id == result.tool_call_id
    assert result.agent_execution_id == EXECUTION_ID
    assert result.step_id == STEP_ID
    assert result.duration_ms is not None and result.duration_ms >= 0
    assert result.metadata["path"] == "README.md"
    assert "Forge Test Project" in str(result.metadata["content"])
    assert result.artifact_digests == ()

    # 2. UoW committed exactly once
    assert work.committed is True
    assert work.rolled_back is False
    assert len(work.tool_calls.records) == 1
    assert len(work.events.events) == 1
    assert len(reader.calls) == 1

    # 3. ToolCallRecord audit projection
    record = work.tool_calls.records[0]
    assert record.id == result.tool_call_id
    assert record.run_id == RUN_ID
    assert record.agent_execution_id == EXECUTION_ID
    assert record.step_id == STEP_ID
    assert record.role is AgentRole.PLANNER
    assert record.policy_version == 1
    assert record.tool_name is ToolName.REPOSITORY_READ_FILE
    assert record.authorized is True
    assert record.status is ToolCallStatus.SUCCEEDED
    assert record.correlation_id == result.tool_call_id
    assert record.operation_intent_id is None
    assert record.duration_ms == result.duration_ms
    assert record.artifact_digests == ()
    assert record.arguments_schema_version == 1
    assert record.result_metadata_schema_version == 1
    assert record.normalized_arguments == {"path": "README.md"}
    assert record.started_at.tzinfo is not None
    assert record.completed_at is not None and record.completed_at.tzinfo is not None
    assert record.started_at <= record.completed_at

    # Result metadata stored in record
    assert record.result_metadata is not None
    assert record.result_metadata["result_status"] == "succeeded"
    assert record.result_metadata["authorized"] is True
    assert record.result_metadata["path"] == "README.md"

    # 4. RunEvent causal lineage
    event = work.events.events[0]
    assert event.run_id == RUN_ID
    assert event.run_version == 0
    assert event.event_type == "tool_call.completed"
    assert event.actor_class == "agent"
    assert event.actor_id == EXECUTION_ID
    assert event.payload["tool_call_id"] == str(result.tool_call_id)
    assert event.payload["tool_name"] == ToolName.REPOSITORY_READ_FILE.value
    assert event.payload["status"] == "succeeded"
    assert event.payload["authorized"] is True
    assert event.payload["policy_version"] == 1
    assert event.payload["resource_id"] == repository_resource_identity(PROJECT_ID)
    assert event.payload["step_id"] == str(STEP_ID)
    assert event.payload["agent_execution_id"] == str(EXECUTION_ID)
    assert event.payload["correlation_id"] == str(result.tool_call_id)
    assert event.payload["duration_ms"] == result.duration_ms
    assert tuple(event.payload["artifact_digests"]) == ()
    assert "result_digest" in event.payload


async def test_developer_bound_worktree_read_audits_worktree_lineage(tmp_path: Path) -> None:
    worktree_path = tmp_path / "worktree"
    worktree_path.mkdir()
    (worktree_path / "app.py").write_text("print('hello')", encoding="utf-8")

    branch = "forge/feature-1"
    identity = WorktreeIdentity.for_run(PROJECT_ID, RUN_ID, branch, False)
    managed_wt = ManagedWorktree(identity=identity, path=worktree_path, base_sha=BASE_SHA)
    run = RunSnapshot(
        id=RUN_ID,
        project_id=PROJECT_ID,
        task_id=TASK_ID,
        state=RunState.IMPLEMENTING,
        policy_version=1,
        branch_name=branch,
        base_sha=BASE_SHA,
        worktree_path=str(worktree_path),
    )
    service, work, reader = _service(
        worktree_path,
        run=run,
        controlled_git=_Git(tmp_path, managed_wt),
        managed_worktree=managed_wt,
    )
    context = _context(role=AgentRole.DEVELOPER, worktree_id=identity.worktree_name)
    request = ToolRequest(name=ToolName.REPOSITORY_READ_FILE, arguments={"path": "app.py"})

    result = await service.invoke(context, request)

    assert result.status is ToolCallStatus.SUCCEEDED
    assert len(reader.calls) == 1
    assert len(work.tool_calls.records) == 1
    record = work.tool_calls.records[0]
    assert record.role is AgentRole.DEVELOPER
    assert record.authorized is True
    assert record.normalized_arguments == {"path": "app.py"}
    assert len(work.events.events) == 1
    event = work.events.events[0]
    assert event.payload["resource_id"] == identity.worktree_name
    assert event.payload["status"] == "succeeded"
    assert event.payload["authorized"] is True


# ---------------------------------------------------------------------------
# Acceptance Criterion 1 & 2: Audited denials for state, policy, resource,
# execution context, and budget
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("run_state", "expected_code"),
    [
        (RunState.CREATED, ToolErrorCode.RUN_NOT_ACTIVE),
        (RunState.COMPLETED, ToolErrorCode.RUN_NOT_ACTIVE),
        (RunState.FAILED, ToolErrorCode.RUN_NOT_ACTIVE),
        (RunState.CANCELLED, ToolErrorCode.RUN_NOT_ACTIVE),
    ],
)
async def test_inactive_run_state_denial_is_audited_without_calling_adapter(
    tmp_path: Path,
    run_state: RunState,
    expected_code: ToolErrorCode,
) -> None:
    (tmp_path / "README.md").write_text("Forge", encoding="utf-8")
    run = RunSnapshot(
        id=RUN_ID,
        project_id=PROJECT_ID,
        task_id=TASK_ID,
        state=run_state,
        policy_version=1,
    )
    service, work, reader = _service(tmp_path, run=run)
    context = _context()
    request = ToolRequest(name=ToolName.REPOSITORY_READ_FILE, arguments={"path": "README.md"})

    result = await service.invoke(context, request)

    assert result.status is ToolCallStatus.DENIED
    assert result.error is not None
    assert result.error.code is expected_code
    assert reader.calls == []
    assert work.committed is True
    assert len(work.tool_calls.records) == 1

    record = work.tool_calls.records[0]
    assert record.authorized is False
    assert record.status is ToolCallStatus.DENIED
    assert record.result_metadata is not None
    assert record.result_metadata["authorized"] is False
    assert record.result_metadata["result_status"] == "denied"
    assert record.result_metadata["error"] == {
        "code": expected_code.value,
        "message": "run is not active for this tool role",
    }
    assert len(work.events.events) == 1
    event = work.events.events[0]
    assert event.payload["resource_id"] == repository_resource_identity(PROJECT_ID)
    assert event.payload["status"] == "denied"
    assert event.payload["authorized"] is False


async def test_policy_version_mismatch_denial_is_audited_without_calling_adapter(
    tmp_path: Path,
) -> None:
    (tmp_path / "README.md").write_text("Forge", encoding="utf-8")
    service, work, reader = _service(tmp_path)
    context = _context(policy_version=2)
    request = ToolRequest(name=ToolName.REPOSITORY_READ_FILE, arguments={"path": "README.md"})

    result = await service.invoke(context, request)

    assert result.status is ToolCallStatus.DENIED
    assert result.error is not None
    assert result.error.code is ToolErrorCode.POLICY_MISMATCH
    assert reader.calls == []
    assert work.committed is True

    record = work.tool_calls.records[0]
    assert record.authorized is False
    assert record.status is ToolCallStatus.DENIED
    assert len(work.events.events) == 1
    event = work.events.events[0]
    assert event.payload["resource_id"] == repository_resource_identity(PROJECT_ID)
    assert event.payload["status"] == "denied"
    assert event.payload["authorized"] is False


async def test_resource_binding_mismatch_denial_is_audited_without_calling_adapter(
    tmp_path: Path,
) -> None:
    (tmp_path / "README.md").write_text("Forge", encoding="utf-8")
    service, work, reader = _service(tmp_path)
    context = _context(worktree_id="forge-unauthorized-worktree-name")
    request = ToolRequest(name=ToolName.REPOSITORY_READ_FILE, arguments={"path": "README.md"})

    result = await service.invoke(context, request)

    assert result.status is ToolCallStatus.DENIED
    assert result.error is not None
    assert result.error.code is ToolErrorCode.RESOURCE_MISMATCH
    assert reader.calls == []
    assert work.committed is True

    record = work.tool_calls.records[0]
    assert record.authorized is False
    assert record.status is ToolCallStatus.DENIED
    assert len(work.events.events) == 1
    event = work.events.events[0]
    assert event.payload["resource_id"] == "forge-unauthorized-worktree-name"
    assert event.payload["status"] == "denied"
    assert event.payload["authorized"] is False


async def test_execution_context_not_current_denial_is_audited_without_calling_adapter(
    tmp_path: Path,
) -> None:
    (tmp_path / "README.md").write_text("Forge", encoding="utf-8")
    service, work, reader = _service(tmp_path)
    work.tool_calls.current = False
    context = _context()
    request = ToolRequest(name=ToolName.REPOSITORY_READ_FILE, arguments={"path": "README.md"})

    result = await service.invoke(context, request)

    assert result.status is ToolCallStatus.DENIED
    assert result.error is not None
    assert result.error.code is ToolErrorCode.RESOURCE_MISMATCH
    assert result.error.message == "tool execution identity is not current"
    assert reader.calls == []
    assert work.committed is True

    record = work.tool_calls.records[0]
    assert record.authorized is False
    assert record.status is ToolCallStatus.DENIED
    assert len(work.events.events) == 1
    event = work.events.events[0]
    assert event.payload["resource_id"] == repository_resource_identity(PROJECT_ID)
    assert event.payload["status"] == "denied"
    assert event.payload["authorized"] is False


async def test_budget_exact_boundary_denial_is_audited_without_calling_adapter(
    tmp_path: Path,
) -> None:
    (tmp_path / "README.md").write_text("Forge", encoding="utf-8")
    policy = _project_policy(tmp_path, max_tool_calls=2)
    service, work, reader = _service(tmp_path, project_policy=policy)
    context = _context()
    request = ToolRequest(name=ToolName.REPOSITORY_READ_FILE, arguments={"path": "README.md"})

    # Set execution count to 1 (under limit of 2) -> allowed
    work.tool_calls.execution_count = 1
    result1 = await service.invoke(context, request)
    assert result1.status is ToolCallStatus.SUCCEEDED
    assert len(reader.calls) == 1

    # Set execution count to 2 (exact limit reached) -> denied
    work.tool_calls.execution_count = 2
    result2 = await service.invoke(context, request)
    assert result2.status is ToolCallStatus.DENIED
    assert result2.error is not None
    assert result2.error.code is ToolErrorCode.BUDGET_EXCEEDED
    assert result2.error.message == "tool-call budget is exhausted"
    assert len(reader.calls) == 1  # No extra adapter call!

    record = work.tool_calls.records[-1]
    assert record.authorized is False
    assert record.status is ToolCallStatus.DENIED
    assert len(work.events.events) == 2
    assert work.events.events[0].payload["resource_id"] == repository_resource_identity(PROJECT_ID)
    assert work.events.events[0].payload["status"] == "succeeded"
    assert work.events.events[0].payload["authorized"] is True
    assert work.events.events[1].payload["resource_id"] == repository_resource_identity(PROJECT_ID)
    assert work.events.events[1].payload["status"] == "denied"
    assert work.events.events[1].payload["authorized"] is False


async def test_capability_denial_for_unauthorized_role_is_audited_without_calling_adapter(
    tmp_path: Path,
) -> None:
    service, work, reader = _service(tmp_path)
    context = _context(role=AgentRole.PLANNER)
    request = ToolRequest(
        name=ToolName.REPOSITORY_WRITE_FILE,
        arguments={"path": "file.txt", "content": "hello"},
    )

    result = await service.invoke(context, request)

    assert result.status is ToolCallStatus.DENIED
    assert result.error is not None
    assert result.error.code is ToolErrorCode.AUTHORIZATION_DENIED
    assert reader.calls == []
    assert work.committed is True

    record = work.tool_calls.records[0]
    assert record.authorized is False
    assert record.status is ToolCallStatus.DENIED
    assert len(work.events.events) == 1
    event = work.events.events[0]
    assert event.payload["resource_id"] == repository_resource_identity(PROJECT_ID)
    assert event.payload["status"] == "denied"
    assert event.payload["authorized"] is False


# ---------------------------------------------------------------------------
# Acceptance Criterion 1: Invalid run identity fails closed without fabricating rows
# ---------------------------------------------------------------------------


async def test_invalid_run_identity_fails_closed_without_persisting_or_fabricating(
    tmp_path: Path,
) -> None:
    (tmp_path / "README.md").write_text("Forge", encoding="utf-8")
    service, work, reader = _service(tmp_path)
    work.runs.run = None  # No run exists for context.run_id

    context = _context(run_id=UUID("99999999-9999-4999-8999-999999999999"))
    request = ToolRequest(name=ToolName.REPOSITORY_READ_FILE, arguments={"path": "README.md"})

    with pytest.raises(ToolInvocationError):
        await service.invoke(context, request)

    # Must fail closed: no records inserted, no commit, no reader calls
    assert reader.calls == []
    assert len(work.tool_calls.records) == 0
    assert len(work.events.events) == 0
    assert work.committed is False


# ---------------------------------------------------------------------------
# Acceptance Criterion 2: Adapter exception returns authorized FAILED without leak
# ---------------------------------------------------------------------------


async def test_adapter_exception_returns_authorized_failed_without_leaking_error_text(
    tmp_path: Path,
) -> None:
    secret_leak = "FATAL: secret_api_token_abc123 failed connection to https://internal.dev"
    (tmp_path / "README.md").write_text("Forge", encoding="utf-8")
    service, work, reader = _service(tmp_path)
    reader.error = RuntimeError(secret_leak)

    context = _context()
    request = ToolRequest(name=ToolName.REPOSITORY_READ_FILE, arguments={"path": "README.md"})

    result = await service.invoke(context, request)

    # 1. Result must be FAILED with stable ADAPTER_ERROR
    assert result.status is ToolCallStatus.FAILED
    assert result.error is not None
    assert result.error.code is ToolErrorCode.ADAPTER_ERROR
    assert result.error.message == "controlled tool adapter failed"

    # 2. No leaked text in result, record, or events
    assert "secret_api_token" not in str(result)
    assert "secret_api_token" not in repr(result)
    assert "secret_api_token" not in result.error.message

    # 3. Audit record proves call was authorized but failed in adapter
    assert work.committed is True
    assert len(work.tool_calls.records) == 1
    record = work.tool_calls.records[0]
    assert record.authorized is True
    assert record.status is ToolCallStatus.FAILED
    assert record.result_metadata is not None
    assert record.result_metadata["error"] == {
        "code": "adapter_error",
        "message": "controlled tool adapter failed",
    }
    assert "secret_api_token" not in json.dumps(record.result_metadata)

    # 4. Event proves failure
    event = work.events.events[0]
    assert event.payload["status"] == "failed"
    assert event.payload["error_code"] == "adapter_error"
    assert event.payload["authorized"] is True
    assert event.payload["resource_id"] == repository_resource_identity(PROJECT_ID)


# ---------------------------------------------------------------------------
# Acceptance Criterion 2: Audit failures rollback and raise ToolInvocationError
# ---------------------------------------------------------------------------


async def test_tool_call_record_failure_rolls_back_and_raises_invocation_error(
    tmp_path: Path,
) -> None:
    (tmp_path / "README.md").write_text("Forge", encoding="utf-8")
    service, work, _ = _service(tmp_path)
    work.tool_calls.record_error = RuntimeError("database disk full")

    context = _context()
    request = ToolRequest(name=ToolName.REPOSITORY_READ_FILE, arguments={"path": "README.md"})

    with pytest.raises(ToolInvocationError):
        await service.invoke(context, request)

    assert work.rolled_back is True
    assert work.committed is False


async def test_event_append_failure_rolls_back_and_raises_invocation_error(
    tmp_path: Path,
) -> None:
    (tmp_path / "README.md").write_text("Forge", encoding="utf-8")
    service, work, _ = _service(tmp_path)
    work.events.append_error = RuntimeError("event stream broke")

    context = _context()
    request = ToolRequest(name=ToolName.REPOSITORY_READ_FILE, arguments={"path": "README.md"})

    with pytest.raises(ToolInvocationError):
        await service.invoke(context, request)

    assert work.rolled_back is True
    assert work.committed is False


async def test_commit_failure_rolls_back_and_raises_invocation_error(
    tmp_path: Path,
) -> None:
    (tmp_path / "README.md").write_text("Forge", encoding="utf-8")
    service, work, _ = _service(tmp_path)
    work.commit_error = RuntimeError("transaction serialization conflict")

    context = _context()
    request = ToolRequest(name=ToolName.REPOSITORY_READ_FILE, arguments={"path": "README.md"})

    with pytest.raises(ToolInvocationError):
        await service.invoke(context, request)

    assert work.rolled_back is True
    assert work.committed is False


# ---------------------------------------------------------------------------
# Acceptance Criterion 2: Terminal replay avoids extra adapter call and extra budget
# ---------------------------------------------------------------------------


class _TrackingWriter:
    def __init__(self, worktree_path: Path) -> None:
        self.worktree_path = worktree_path
        self.calls: list[tuple[str, str]] = []

    def is_bound_to(self, controlled_git: object, worktree: object, policy: object) -> bool:
        return True

    def write_file(self, path: str, content: str) -> FileWrite:
        self.calls.append((path, content))
        encoded = content.encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        target_file = self.worktree_path / path
        target_file.parent.mkdir(parents=True, exist_ok=True)
        target_file.write_text(content, encoding="utf-8")
        return FileWrite(
            path=path,
            output_digest=digest,
            byte_count=len(encoded),
            created=True,
        )

    def inspect_file(self, path: str, expected_digest: str) -> FileWrite | None:
        return None


class _MemoryArtifactStore:
    def __init__(self) -> None:
        self.stored: dict[str, bytes] = {}

    async def put_bytes(
        self,
        data: bytes,
        *,
        media_type: str,
        max_bytes: int | None = None,
        bounding_policy: str = "none",
    ) -> ArtifactDescriptor:
        digest = hashlib.sha256(data).hexdigest()
        self.stored[digest] = data
        return ArtifactDescriptor(
            digest=digest,
            media_type=media_type,
            byte_count=len(data),
            storage_path=Path(f"sha256/{digest[:2]}/{digest[2:]}.blob"),
        )

    async def verify(self, digest: str) -> bool:
        return digest in self.stored

    async def open_bytes(self, digest: str) -> bytes:
        return self.stored[digest]


async def test_write_terminal_replay_avoids_extra_adapter_call_and_extra_budget(
    tmp_path: Path,
) -> None:
    worktree_path = tmp_path / "worktree"
    worktree_path.mkdir()

    branch = "forge/dev-write"
    identity = WorktreeIdentity.for_run(PROJECT_ID, RUN_ID, branch, False)
    managed_wt = ManagedWorktree(identity=identity, path=worktree_path, base_sha=BASE_SHA)
    run = RunSnapshot(
        id=RUN_ID,
        project_id=PROJECT_ID,
        task_id=TASK_ID,
        state=RunState.IMPLEMENTING,
        policy_version=1,
        branch_name=branch,
        base_sha=BASE_SHA,
        worktree_path=str(worktree_path),
    )
    policy = _project_policy(tmp_path, max_tool_calls=1)
    project = _project_record(policy)
    work = _UnitOfWork(run, project)

    reader = _TrackingReader(
        RepositoryReader(
            worktree_path,
            secret_paths=(".env",),
            max_file_bytes=1024,
            force_python_search=True,
        )
    )
    writer = _TrackingWriter(worktree_path)
    artifact_store = _MemoryArtifactStore()
    git = _Git(tmp_path, managed_wt)

    class _MockOpRepo:
        def __init__(self) -> None:
            self.intents: dict[UUID, OperationIntent] = {}

        async def begin(self, **kwargs: Any) -> OperationIntent:
            now = datetime.now(UTC)
            expires = now + timedelta(seconds=kwargs.get("execution_lease_seconds", 30))
            intent = OperationIntent(
                id=UUID("66666666-6666-4666-8666-666666666666"),
                run_id=kwargs["run_id"],
                kind=kwargs["operation_type"],
                idempotency_key=kwargs["idempotency_key"],
                request_digest=kwargs["request_digest"],
                request_payload=kwargs["request_payload"],
                status=OperationStatus.PENDING,
                is_new=True,
                execution_owner=kwargs.get("execution_owner"),
                execution_lease_expires_at=expires,
                created_at=now,
                updated_at=now,
            )
            self.intents[intent.id] = intent
            return intent

        async def get(self, intent_id: UUID) -> OperationIntent:
            return self.intents[intent_id]

        async def renew_lease(
            self, intent_id: UUID, *, owner_id: str, lease_seconds: float
        ) -> bool:
            return True

        async def complete(
            self,
            intent_id: UUID,
            outcome: OperationOutcome,
            *,
            owner_id: str | None = None,
        ) -> OperationIntent:
            current = self.intents[intent_id]
            updated = replace(
                current,
                status=OperationStatus.SUCCEEDED,
                outcome=outcome.payload,
                outcome_schema_version=1,
                completed_at=datetime.now(UTC),
                execution_owner=None,
                execution_lease_expires_at=None,
            )
            self.intents[intent_id] = updated
            return updated

        async def fail(self, *_: Any, **__: Any) -> None:
            pass

    op_repo = _MockOpRepo()
    work.operations = op_repo  # type: ignore[attr-defined]
    executor = OperationExecutor(op_repo)  # type: ignore[arg-type]

    service = ControlledToolService(
        lambda: work,
        repository_reader=reader,
        repository_writer=writer,
        controlled_git=git,
        operation_executor=executor,
        artifact_store=artifact_store,
        worktree=managed_wt,
    )

    context = _context(
        role=AgentRole.DEVELOPER,
        worktree_id=identity.worktree_name,
        agent_execution_id=EXECUTION_ID,
        step_id=STEP_ID,
    )
    request = ToolRequest(
        name=ToolName.REPOSITORY_WRITE_FILE,
        arguments={"path": "hello.txt", "content": "world"},
    )

    # 1. First execution succeeds
    result1 = await service.invoke(context, request)
    assert result1.status is ToolCallStatus.SUCCEEDED
    assert len(writer.calls) == 1

    # 2. Budget is now 1 (limit is 1). A new non-replayed call would fail with BUDGET_EXCEEDED.
    work.tool_calls.execution_count = 1

    # 3. Terminal replay with identical request must return replayed result without calling writer
    result2 = await service.invoke(context, request)
    assert result2.status is ToolCallStatus.SUCCEEDED
    assert result2.tool_call_id == result1.tool_call_id
    assert len(writer.calls) == 1  # Writer was NOT called again!
    assert work.rolled_back is True  # Replay rolls back read-only UoW without creating new records
    assert len(work.events.events) == 1
    assert work.events.events[0].payload["resource_id"] == identity.worktree_name
    assert work.events.events[0].payload["status"] == "succeeded"
    assert work.events.events[0].payload["authorized"] is True


async def test_write_audits_canonical_digest_arguments_and_excludes_plaintext_canary_from_all_records(
    tmp_path: Path,
) -> None:
    """Verify write audits canonical digest-only arguments and excludes plaintext canary from records."""
    worktree_path = tmp_path / "worktree"
    worktree_path.mkdir()

    branch = "forge/dev-write-canary"
    identity = WorktreeIdentity.for_run(PROJECT_ID, RUN_ID, branch, False)
    managed_wt = ManagedWorktree(identity=identity, path=worktree_path, base_sha=BASE_SHA)
    run = RunSnapshot(
        id=RUN_ID,
        project_id=PROJECT_ID,
        task_id=TASK_ID,
        state=RunState.IMPLEMENTING,
        policy_version=1,
        branch_name=branch,
        base_sha=BASE_SHA,
        worktree_path=str(worktree_path),
    )
    policy = _project_policy(tmp_path)
    project = _project_record(policy)
    work = _UnitOfWork(run, project)

    reader = _TrackingReader(
        RepositoryReader(
            worktree_path,
            secret_paths=(".env",),
            max_file_bytes=1024,
            force_python_search=True,
        )
    )
    writer = _TrackingWriter(worktree_path)
    artifact_store = _MemoryArtifactStore()
    git = _Git(tmp_path, managed_wt)

    class _OpRepo:
        def __init__(self) -> None:
            self.intents: dict[UUID, OperationIntent] = {}

        async def begin(self, **kwargs: Any) -> OperationIntent:
            now = datetime.now(UTC)
            expires = now + timedelta(seconds=kwargs.get("execution_lease_seconds", 30))
            intent = OperationIntent(
                id=UUID("77777777-7777-4777-8777-777777777777"),
                run_id=kwargs["run_id"],
                kind=kwargs["operation_type"],
                idempotency_key=kwargs["idempotency_key"],
                request_digest=kwargs["request_digest"],
                request_payload=kwargs["request_payload"],
                status=OperationStatus.PENDING,
                is_new=True,
                execution_owner=kwargs.get("execution_owner"),
                execution_lease_expires_at=expires,
                created_at=now,
                updated_at=now,
            )
            self.intents[intent.id] = intent
            return intent

        async def get(self, intent_id: UUID) -> OperationIntent:
            return self.intents[intent_id]

        async def renew_lease(
            self, intent_id: UUID, *, owner_id: str, lease_seconds: float
        ) -> bool:
            return True

        async def complete(
            self,
            intent_id: UUID,
            outcome: OperationOutcome,
            *,
            owner_id: str | None = None,
        ) -> OperationIntent:
            current = self.intents[intent_id]
            updated = replace(
                current,
                status=OperationStatus.SUCCEEDED,
                outcome=outcome.payload,
                outcome_schema_version=1,
                completed_at=datetime.now(UTC),
                execution_owner=None,
                execution_lease_expires_at=None,
            )
            self.intents[intent_id] = updated
            return updated

        async def fail(self, *_: Any, **__: Any) -> None:
            pass

    op_repo = _OpRepo()
    work.operations = op_repo  # type: ignore[attr-defined]
    executor = OperationExecutor(op_repo)  # type: ignore[arg-type]

    service = ControlledToolService(
        lambda: work,
        repository_reader=reader,
        repository_writer=writer,
        controlled_git=git,
        operation_executor=executor,
        artifact_store=artifact_store,
        worktree=managed_wt,
    )

    context = _context(
        role=AgentRole.DEVELOPER,
        worktree_id=identity.worktree_name,
        agent_execution_id=EXECUTION_ID,
        step_id=STEP_ID,
    )

    canary_token = "CANARY_SECRET_ghp_writersecrettoken555544443333_write"
    canary_content = f"{canary_token}\n"
    canary_bytes = canary_content.encode("utf-8")
    canary_digest = hashlib.sha256(canary_bytes).hexdigest()
    target_path = "src/nested/written.py"

    # Positive control: prove canary_token detects leaked content in JSON where canary_content vacuously passes
    assert canary_content not in json.dumps({"content": canary_content})
    assert canary_token in json.dumps({"content": canary_content})

    request = ToolRequest(
        name=ToolName.REPOSITORY_WRITE_FILE,
        arguments={"path": target_path, "content": canary_content},
    )

    result = await service.invoke(context, request)
    assert result.status is ToolCallStatus.SUCCEEDED

    # File effect occurred on disk
    assert (worktree_path / target_path).read_text(encoding="utf-8") == canary_content

    # Exact canonical/digest-only normalized arguments
    expected_normalized_args = {
        "path": target_path,
        "content_digest": canary_digest,
        "content_byte_count": len(canary_bytes),
    }

    # ToolCall record
    assert len(work.tool_calls.records) == 1
    record = work.tool_calls.records[0]
    assert record.normalized_arguments == expected_normalized_args
    assert "content" not in record.normalized_arguments
    assert canary_token not in json.dumps(record.normalized_arguments)
    assert canary_token not in json.dumps(record.result_metadata)

    # OperationIntent payload
    intent = op_repo.intents[result.operation_intent_id]
    assert intent.request_payload["path"] == target_path
    assert intent.request_payload["content_digest"] == canary_digest
    assert intent.request_payload["content_byte_count"] == len(canary_bytes)
    assert "content" not in intent.request_payload
    assert canary_token not in json.dumps(dict(intent.request_payload))
    if intent.outcome is not None:
        assert canary_token not in json.dumps(dict(intent.outcome))

    # Causal event
    assert len(work.events.events) == 1
    event = work.events.events[0]
    assert event.payload["resource_id"] == identity.worktree_name
    assert canary_token not in json.dumps(dict(event.payload))

    # Evidence artifact
    assert len(artifact_store.stored) == 1
    artifact_bytes = next(iter(artifact_store.stored.values()))
    assert canary_token.encode("utf-8") not in artifact_bytes
    artifact_doc = json.loads(artifact_bytes.decode("utf-8"))
    assert canary_token not in json.dumps(artifact_doc)


async def test_service_configured_redactor_redacts_sensitive_canary_from_read_and_search_audit(
    tmp_path: Path,
) -> None:
    """Verify service-configured Redactor redacts sensitive canaries from read and search audit."""
    worktree_path = tmp_path / "worktree"
    worktree_path.mkdir()

    canary = "CANARY_SECRET_ghp_testcanarytoken999988887777_literal"
    (worktree_path / "notes.txt").write_text(f"api_key: {canary}\n", encoding="utf-8")

    branch = "forge/dev-read-redact"
    identity = WorktreeIdentity.for_run(PROJECT_ID, RUN_ID, branch, False)
    managed_wt = ManagedWorktree(identity=identity, path=worktree_path, base_sha=BASE_SHA)
    run = RunSnapshot(
        id=RUN_ID,
        project_id=PROJECT_ID,
        task_id=TASK_ID,
        state=RunState.IMPLEMENTING,
        policy_version=1,
        branch_name=branch,
        base_sha=BASE_SHA,
        worktree_path=str(worktree_path),
    )
    policy = _project_policy(tmp_path)
    project = _project_record(policy)
    work = _UnitOfWork(run, project)

    reader = _TrackingReader(
        RepositoryReader(
            worktree_path,
            secret_paths=(".env",),
            max_file_bytes=1024,
            force_python_search=True,
        )
    )
    git = _Git(tmp_path, managed_wt)

    # Configure service with explicit Redactor registered with the synthetic canary
    redactor = Redactor(secrets=(canary,))
    service = ControlledToolService(
        lambda: work,
        repository_reader=reader,
        controlled_git=git,
        worktree=managed_wt,
        redactor=redactor,
    )
    context = _context(
        role=AgentRole.DEVELOPER,
        worktree_id=identity.worktree_name,
        agent_execution_id=EXECUTION_ID,
        step_id=STEP_ID,
    )

    # 1. Invoke REPOSITORY_READ_FILE
    read_request = ToolRequest(
        name=ToolName.REPOSITORY_READ_FILE,
        arguments={"path": "notes.txt"},
    )
    read_result = await service.invoke(context, read_request)
    assert read_result.status is ToolCallStatus.SUCCEEDED
    assert canary not in json.dumps(thaw_payload(read_result.metadata))
    assert "[REDACTED]" in str(read_result.metadata.get("content"))

    # Audited record in tool_calls also has canary redacted
    record = work.tool_calls.records[0]
    assert canary not in json.dumps(thaw_payload(record.result_metadata))
    assert "[REDACTED]" in str(record.result_metadata.get("content"))

    # Causal RunEvent does not contain the canary
    event = work.events.events[0]
    assert canary not in json.dumps(thaw_payload(event.payload))

    # 2. Invoke REPOSITORY_SEARCH
    search_request = ToolRequest(
        name=ToolName.REPOSITORY_SEARCH,
        arguments={"literal": "api_key", "path": "."},
    )
    search_result = await service.invoke(context, search_request)
    assert search_result.status is ToolCallStatus.SUCCEEDED
    assert canary not in json.dumps(thaw_payload(search_result.metadata))

    record2 = work.tool_calls.records[1]
    assert canary not in json.dumps(thaw_payload(record2.result_metadata))
    event2 = work.events.events[1]
    assert canary not in json.dumps(thaw_payload(event2.payload))


async def test_directory_reads_normalize_empty_and_dot_paths_to_canonical_root(
    tmp_path: Path,
) -> None:
    """Verify directory read tools normalize empty and dot paths to canonical root."""
    worktree_path = tmp_path / "worktree"
    worktree_path.mkdir()
    (worktree_path / "hello.txt").write_text("hello\n", encoding="utf-8")
    (worktree_path / "AGENTS.md").write_text("instructions\n", encoding="utf-8")

    branch = "forge/dev-norm-test"
    identity = WorktreeIdentity.for_run(PROJECT_ID, RUN_ID, branch, False)
    managed_wt = ManagedWorktree(identity=identity, path=worktree_path, base_sha=BASE_SHA)
    run = RunSnapshot(
        id=RUN_ID,
        project_id=PROJECT_ID,
        task_id=TASK_ID,
        state=RunState.IMPLEMENTING,
        policy_version=1,
        branch_name=branch,
        base_sha=BASE_SHA,
        worktree_path=str(worktree_path),
    )
    policy = _project_policy(tmp_path)
    project = _project_record(policy)
    work = _UnitOfWork(run, project)

    reader = _TrackingReader(
        RepositoryReader(
            worktree_path,
            secret_paths=(".env",),
            max_file_bytes=1024,
            force_python_search=True,
        )
    )
    git = _Git(tmp_path, managed_wt)
    service = ControlledToolService(
        lambda: work,
        repository_reader=reader,
        controlled_git=git,
        worktree=managed_wt,
    )
    context = _context(
        role=AgentRole.DEVELOPER,
        worktree_id=identity.worktree_name,
        agent_execution_id=EXECUTION_ID,
        step_id=STEP_ID,
    )

    # 1. LIST_FILES with path="" normalizes to "."
    res1 = await service.invoke(
        context,
        ToolRequest(name=ToolName.REPOSITORY_LIST_FILES, arguments={"path": ""}),
    )
    assert res1.status is ToolCallStatus.SUCCEEDED
    assert work.tool_calls.records[0].normalized_arguments["path"] == "."

    # 2. SEARCH with path="" normalizes to "."
    res2 = await service.invoke(
        context,
        ToolRequest(name=ToolName.REPOSITORY_SEARCH, arguments={"literal": "hello", "path": ""}),
    )
    assert res2.status is ToolCallStatus.SUCCEEDED
    assert work.tool_calls.records[1].normalized_arguments["path"] == "."

    # 3. READ_INSTRUCTIONS with target_path="" normalizes to "."
    res3 = await service.invoke(
        context,
        ToolRequest(name=ToolName.REPOSITORY_READ_INSTRUCTIONS, arguments={"target_path": ""}),
    )
    assert res3.status is ToolCallStatus.SUCCEEDED
    assert work.tool_calls.records[2].normalized_arguments["target_path"] == "."
