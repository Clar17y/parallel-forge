from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Self
from uuid import UUID

import pytest
from forge.application.ports.projects import ProjectPolicyRecord, ProjectRecord
from forge.application.ports.tools import ToolCallRecord
from forge.application.ports.worktrees import ManagedWorktree
from forge.application.services.tools import ControlledToolService
from forge.domain.actor import AgentRole
from forge.domain.approval import ApprovalGate
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
from forge.tools.repository import RepositoryReader

PROJECT_ID = UUID("11111111-1111-4111-8111-111111111111")
RUN_ID = UUID("22222222-2222-4222-8222-222222222222")
TASK_ID = UUID("33333333-3333-4333-8333-333333333333")
EXECUTION_ID = UUID("44444444-4444-4444-8444-444444444444")
STEP_ID = UUID("55555555-5555-4555-8555-555555555555")
OTHER_PROJECT_ID = UUID("66666666-6666-4666-8666-666666666666")


@dataclass
class _Runs:
    run: RunSnapshot

    async def get_for_update(self, run_id: UUID) -> RunSnapshot:
        assert run_id == self.run.id
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
        self.records: list[object] = []
        self.current = True
        self.execution_count = 0

    async def validate_execution_context(self, *_: object) -> bool:
        return self.current

    async def count_for_execution(self, _: UUID) -> int:
        return self.execution_count

    async def record(self, record: object) -> object:
        self.records.append(record)
        return record


class _Events:
    def __init__(self) -> None:
        self.events: list[object] = []

    async def append(self, event: object) -> object:
        self.events.append(event)
        return event


class _UnitOfWork:
    def __init__(self, run: RunSnapshot, project: ProjectRecord) -> None:
        self.runs = _Runs(run)
        self.projects = _Projects(project)
        self.tool_calls = _ToolCalls()
        self.events = _Events()
        self.committed = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        return None


class _TrackingReader:
    def __init__(self, reader: RepositoryReader) -> None:
        self._reader = reader
        self.calls = 0
        self.exclusion_result: object | None = None
        self.exclusion_error: Exception | None = None

    @property
    def root(self):  # type: ignore[no-untyped-def]
        return self._reader.root

    def excludes_paths(self, paths: tuple[str, ...]) -> object:
        if self.exclusion_error is not None:
            raise self.exclusion_error
        if self.exclusion_result is not None:
            return self.exclusion_result
        return self._reader.excludes_paths(paths)

    def list_files(self, path: str = "."):  # type: ignore[no-untyped-def]
        self.calls += 1
        return self._reader.list_files(path)

    def read_file(self, path: str):  # type: ignore[no-untyped-def]
        self.calls += 1
        return self._reader.read_file(path)

    def search(self, literal: str, path: str = "."):  # type: ignore[no-untyped-def]
        self.calls += 1
        return self._reader.search(literal, path)

    def read_instructions(self, target_path: str = "."):  # type: ignore[no-untyped-def]
        self.calls += 1
        return self._reader.read_instructions(target_path)


class _Git:
    def __init__(self, repository: Path, worktree: ManagedWorktree) -> None:
        self.repository_path = repository
        self._worktree = worktree

    def expected_worktree(self, identity: WorktreeIdentity, base_sha: str) -> ManagedWorktree:
        assert identity == self._worktree.identity
        assert base_sha == self._worktree.base_sha
        return self._worktree


def _project(
    repository: Path, *, allowed_environment_files: tuple[str, ...] = (), max_tool_calls: int = 100
) -> ProjectRecord:
    policy = ProjectPolicy(
        id=PROJECT_ID,
        version=1,
        repository_path=str(repository),
        github_repository="forge/test",
        default_branch="main",
        secret_paths=(".env",),
        allowed_environment_files=allowed_environment_files,
        planner_model=AgentModelPolicy(max_tool_calls=max_tool_calls),
    )
    return ProjectRecord(
        id=PROJECT_ID,
        name="test",
        canonical_path=str(repository),
        canonical_path_key=str(repository),
        github_repository="forge/test",
        default_branch="main",
        instructions_path=None,
        current_policy_version=1,
        policy=ProjectPolicyRecord(
            project_id=PROJECT_ID,
            version=1,
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
    }
    values.update(overrides)
    return ToolAuthorizationContext(**values)  # type: ignore[arg-type]


def _service(
    tmp_path: Path,
    run: RunSnapshot | None = None,
    *,
    controlled_git: _Git | None = None,
    managed_worktree: ManagedWorktree | None = None,
    project_root: Path | None = None,
    allowed_environment_files: tuple[str, ...] = (),
    reader_secret_paths: tuple[str, ...] = (".env",),
    max_tool_calls: int = 100,
) -> tuple[ControlledToolService, _UnitOfWork, _TrackingReader]:
    selected_run = run or RunSnapshot(
        id=RUN_ID,
        project_id=PROJECT_ID,
        task_id=TASK_ID,
        state=RunState.PLANNING,
        policy_version=1,
    )
    reader = _TrackingReader(
        RepositoryReader(
            tmp_path,
            secret_paths=reader_secret_paths,
            max_file_bytes=32,
            force_python_search=True,
        )
    )
    work = _UnitOfWork(
        selected_run,
        _project(
            project_root or tmp_path,
            allowed_environment_files=allowed_environment_files,
            max_tool_calls=max_tool_calls,
        ),
    )
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


async def test_planner_reads_the_locked_canonical_repository_before_worktree_creation(
    tmp_path: Path,
) -> None:
    (tmp_path / "README.md").write_text("Forge planner source\n", encoding="utf-8")
    service, work, _ = _service(tmp_path)

    result = await service.invoke(
        _context(),
        ToolRequest(name=ToolName.REPOSITORY_READ_FILE, arguments={"path": "README.md"}),
    )

    assert result.error is None
    assert result.status is ToolCallStatus.SUCCEEDED
    assert result.metadata["content"].rstrip("\r\n") == "Forge planner source"
    assert work.committed is True
    assert len(work.tool_calls.records) == 1
    assert work.events.events[0].payload["resource_id"] == repository_resource_identity(PROJECT_ID)


async def test_planner_read_with_an_existing_worktree_keeps_worktree_binding(
    tmp_path: Path,
) -> None:
    canonical = tmp_path / "canonical"
    managed = tmp_path / "managed"
    canonical.mkdir()
    managed.mkdir()
    (canonical / "README.md").write_text("Forge canonical source", encoding="utf-8")
    (managed / "README.md").write_text("Forge managed source", encoding="utf-8")
    base_sha = "a" * 40
    identity = WorktreeIdentity.for_run(PROJECT_ID, RUN_ID, "forge/planner", False)
    worktree = ManagedWorktree(identity=identity, path=managed, base_sha=base_sha)
    run = RunSnapshot(
        id=RUN_ID,
        project_id=PROJECT_ID,
        task_id=TASK_ID,
        state=RunState.PLANNING,
        policy_version=1,
        branch_name="forge/planner",
        base_sha=base_sha,
        worktree_path=str(managed),
    )
    service, _, _ = _service(
        managed,
        run,
        controlled_git=_Git(canonical, worktree),
        managed_worktree=worktree,
        project_root=canonical,
    )

    result = await service.invoke(
        _context(worktree_id=identity.worktree_name),
        ToolRequest(name=ToolName.REPOSITORY_READ_FILE, arguments={"path": "README.md"}),
    )

    assert result.status is ToolCallStatus.SUCCEEDED
    assert result.metadata["content"] == "Forge managed source"


async def test_planner_read_rejects_a_reader_bound_to_another_repository(tmp_path: Path) -> None:
    registered = tmp_path / "registered"
    other = tmp_path / "other"
    registered.mkdir()
    other.mkdir()
    (other / "README.md").write_text("wrong repository", encoding="utf-8")
    service, _, reader = _service(other, project_root=registered)

    result = await service.invoke(
        _context(),
        ToolRequest(name=ToolName.REPOSITORY_READ_FILE, arguments={"path": "README.md"}),
    )

    assert result.status is ToolCallStatus.DENIED
    assert result.error is not None
    assert result.error.code is ToolErrorCode.RESOURCE_MISMATCH
    assert reader.calls == 0


async def test_canonical_reader_requires_every_policy_secret_exclusion(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("Forge", encoding="utf-8")
    service, _, reader = _service(
        tmp_path,
        allowed_environment_files=("config/runtime.env",),
    )

    result = await service.invoke(
        _context(),
        ToolRequest(name=ToolName.REPOSITORY_READ_FILE, arguments={"path": "README.md"}),
    )

    assert result.status is ToolCallStatus.DENIED
    assert result.error is not None
    assert result.error.code is ToolErrorCode.RESOURCE_MISMATCH
    assert reader.calls == 0


@pytest.mark.parametrize(
    "tool_request",
    (
        ToolRequest(name=ToolName.REPOSITORY_LIST_FILES, arguments={"path": "."}),
        ToolRequest(name=ToolName.REPOSITORY_READ_FILE, arguments={"path": "README.md"}),
        ToolRequest(
            name=ToolName.REPOSITORY_SEARCH,
            arguments={"literal": "SECRET_CANARY", "path": "."},
        ),
        ToolRequest(
            name=ToolName.REPOSITORY_READ_INSTRUCTIONS, arguments={"target_path": "config"}
        ),
    ),
)
async def test_canonical_reader_with_policy_exclusions_hides_configured_secrets(
    tmp_path: Path, tool_request: ToolRequest
) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (tmp_path / "README.md").write_text("Forge", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("untrusted root instructions", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET_CANARY", encoding="utf-8")
    (config / "runtime.env").write_text("SECRET_CANARY", encoding="utf-8")
    (config / "AGENTS.md").write_text("SECRET_CANARY", encoding="utf-8")
    service, _, _ = _service(
        tmp_path,
        allowed_environment_files=("config/runtime.env", "config/AGENTS.md"),
        reader_secret_paths=(".env", "config/runtime.env", "config/AGENTS.md"),
    )

    result = await service.invoke(_context(), tool_request)

    assert result.status is ToolCallStatus.SUCCEEDED
    if tool_request.name is ToolName.REPOSITORY_LIST_FILES:
        paths = {entry["path"] for entry in result.metadata["entries"]}
        assert ".env" not in paths
        assert "config/runtime.env" not in paths
        assert "config/AGENTS.md" not in paths
    if tool_request.name is ToolName.REPOSITORY_SEARCH:
        assert tuple(result.metadata["matches"]) == ()
    if tool_request.name is ToolName.REPOSITORY_READ_INSTRUCTIONS:
        assert "config/AGENTS.md" not in {
            document["path"] for document in result.metadata["documents"]
        }


async def test_canonical_direct_configured_secret_read_fails_at_adapter(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("secret", encoding="utf-8")
    service, _, reader = _service(tmp_path)

    result = await service.invoke(
        _context(),
        ToolRequest(name=ToolName.REPOSITORY_READ_FILE, arguments={"path": ".env"}),
    )

    assert result.status is ToolCallStatus.FAILED
    assert result.error is not None
    assert result.error.code is ToolErrorCode.ADAPTER_ERROR
    assert reader.calls == 1


async def test_canonical_direct_allowed_environment_read_fails_at_adapter(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "runtime.env").write_text("SECRET_CANARY", encoding="utf-8")
    service, _, reader = _service(
        tmp_path,
        allowed_environment_files=("config/runtime.env",),
        reader_secret_paths=(".env", "config/runtime.env"),
    )

    result = await service.invoke(
        _context(),
        ToolRequest(name=ToolName.REPOSITORY_READ_FILE, arguments={"path": "config/runtime.env"}),
    )

    assert result.status is ToolCallStatus.FAILED
    assert result.error is not None
    assert result.error.code is ToolErrorCode.ADAPTER_ERROR
    assert reader.calls == 1


@pytest.mark.parametrize("proof", (False, "true", object()))
async def test_canonical_reader_rejects_non_boolean_exclusion_proof(
    tmp_path: Path, proof: object
) -> None:
    (tmp_path / "README.md").write_text("Forge", encoding="utf-8")
    service, _, reader = _service(tmp_path)
    reader.exclusion_result = proof

    result = await service.invoke(
        _context(),
        ToolRequest(name=ToolName.REPOSITORY_READ_FILE, arguments={"path": "README.md"}),
    )

    assert result.status is ToolCallStatus.DENIED
    assert result.error is not None
    assert result.error.code is ToolErrorCode.RESOURCE_MISMATCH
    assert reader.calls == 0


async def test_canonical_reader_rejects_exclusion_proof_error(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("Forge", encoding="utf-8")
    service, _, reader = _service(tmp_path)
    reader.exclusion_error = RuntimeError("untrusted adapter failure")

    result = await service.invoke(
        _context(),
        ToolRequest(name=ToolName.REPOSITORY_READ_FILE, arguments={"path": "README.md"}),
    )

    assert result.status is ToolCallStatus.DENIED
    assert result.error is not None
    assert result.error.code is ToolErrorCode.RESOURCE_MISMATCH
    assert reader.calls == 0


async def test_canonical_reader_rejects_missing_exclusion_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "README.md").write_text("Forge", encoding="utf-8")
    monkeypatch.delattr(_TrackingReader, "excludes_paths")
    service, _, reader = _service(tmp_path)

    result = await service.invoke(
        _context(),
        ToolRequest(name=ToolName.REPOSITORY_READ_FILE, arguments={"path": "README.md"}),
    )

    assert result.status is ToolCallStatus.DENIED
    assert result.error is not None
    assert result.error.code is ToolErrorCode.RESOURCE_MISMATCH
    assert reader.calls == 0


async def test_canonical_budget_exhaustion_does_not_call_reader(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("Forge", encoding="utf-8")
    service, work, reader = _service(tmp_path, max_tool_calls=1)
    work.tool_calls.execution_count = 1

    result = await service.invoke(
        _context(),
        ToolRequest(name=ToolName.REPOSITORY_READ_FILE, arguments={"path": "README.md"}),
    )

    assert result.status is ToolCallStatus.DENIED
    assert result.error is not None
    assert result.error.code is ToolErrorCode.BUDGET_EXCEEDED
    assert reader.calls == 0


@pytest.mark.parametrize("value", (UUID(int=0), "not-a-uuid", object()))
def test_repository_resource_identity_rejects_invalid_project_identity(value: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        repository_resource_identity(value)  # type: ignore[arg-type]


async def test_canonical_planner_access_ends_when_planning_ends(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("Forge", encoding="utf-8")
    run = RunSnapshot(
        id=RUN_ID,
        project_id=PROJECT_ID,
        task_id=TASK_ID,
        state=RunState.AWAITING_PLAN_APPROVAL,
        policy_version=1,
        pending_gate=ApprovalGate.PLAN,
        pending_evidence_digest="a" * 64,
    )
    service, _, reader = _service(tmp_path, run)

    result = await service.invoke(
        _context(),
        ToolRequest(name=ToolName.REPOSITORY_READ_FILE, arguments={"path": "README.md"}),
    )

    assert result.status is ToolCallStatus.DENIED
    assert result.error is not None
    assert result.error.code is ToolErrorCode.RESOURCE_MISMATCH
    assert reader.calls == 0


@pytest.mark.parametrize(
    ("role", "state"),
    ((AgentRole.DEVELOPER, RunState.IMPLEMENTING), (AgentRole.REVIEWER, RunState.REVIEWING)),
)
async def test_non_planner_without_worktree_cannot_use_canonical_reader(
    tmp_path: Path, role: AgentRole, state: RunState
) -> None:
    (tmp_path / "README.md").write_text("Forge", encoding="utf-8")
    run = RunSnapshot(
        id=RUN_ID,
        project_id=PROJECT_ID,
        task_id=TASK_ID,
        state=state,
        policy_version=1,
    )
    service, work, reader = _service(tmp_path, run)

    result = await service.invoke(
        _context(role=role),
        ToolRequest(name=ToolName.REPOSITORY_READ_FILE, arguments={"path": "README.md"}),
    )

    assert result.status is ToolCallStatus.DENIED
    assert result.error is not None
    assert result.error.code is ToolErrorCode.RESOURCE_MISMATCH
    assert reader.calls == 0
    assert isinstance(work.tool_calls.records[0], ToolCallRecord)
    assert work.tool_calls.records[0].authorized is False
    assert work.events.events[0].payload["resource_id"] == repository_resource_identity(PROJECT_ID)


@pytest.mark.parametrize(
    "tool_request",
    (
        ToolRequest(name=ToolName.REPOSITORY_LIST_FILES, arguments={"path": "."}),
        ToolRequest(name=ToolName.REPOSITORY_READ_FILE, arguments={"path": "README.md"}),
        ToolRequest(name=ToolName.REPOSITORY_SEARCH, arguments={"literal": "Forge", "path": "."}),
        ToolRequest(name=ToolName.REPOSITORY_READ_INSTRUCTIONS, arguments={"target_path": "."}),
    ),
)
async def test_planner_repository_reads_are_bounded_untrusted_and_audited(
    tmp_path: Path, tool_request: ToolRequest
) -> None:
    (tmp_path / "README.md").write_text("Forge " + "x" * 80, encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("untrusted repository instructions", encoding="utf-8")
    (tmp_path / ".env").write_text("PRIVATE=not-readable", encoding="utf-8")
    service, work, _ = _service(tmp_path)

    result = await service.invoke(_context(), tool_request)

    assert result.status is ToolCallStatus.SUCCEEDED
    assert isinstance(work.tool_calls.records[0], ToolCallRecord)
    record = work.tool_calls.records[0]
    assert record.authorized is True
    assert record.role is AgentRole.PLANNER
    assert record.run_id == RUN_ID
    assert record.step_id == STEP_ID
    assert record.policy_version == 1
    if tool_request.name is ToolName.REPOSITORY_READ_FILE:
        assert result.metadata["truncated"] is True
    if tool_request.name is ToolName.REPOSITORY_READ_INSTRUCTIONS:
        assert result.metadata["documents"][0]["untrusted_repository_content"] is True
    if tool_request.name is ToolName.REPOSITORY_LIST_FILES:
        assert ".env" not in {entry["path"] for entry in result.metadata["entries"]}


@pytest.mark.parametrize(
    ("tool_request", "context_overrides", "run_overrides", "execution_current", "expected_code"),
    (
        (
            ToolRequest(name=ToolName.REPOSITORY_READ_FILE, arguments={"path": "README.md"}),
            {"worktree_id": repository_resource_identity(OTHER_PROJECT_ID)},
            {},
            True,
            ToolErrorCode.RESOURCE_MISMATCH,
        ),
        (
            ToolRequest(name=ToolName.REPOSITORY_READ_FILE, arguments={"path": "README.md"}),
            {"policy_version": 2},
            {},
            True,
            ToolErrorCode.POLICY_MISMATCH,
        ),
        (
            ToolRequest(name=ToolName.REPOSITORY_READ_FILE, arguments={"path": "README.md"}),
            {},
            {"state": RunState.COMPLETED},
            True,
            ToolErrorCode.RUN_NOT_ACTIVE,
        ),
        (
            ToolRequest(name=ToolName.REPOSITORY_READ_FILE, arguments={"path": "../outside"}),
            {},
            {},
            True,
            ToolErrorCode.INVALID_REQUEST,
        ),
        (
            ToolRequest(name=ToolName.REPOSITORY_READ_FILE, arguments={"path": "README.md"}),
            {},
            {},
            False,
            ToolErrorCode.RESOURCE_MISMATCH,
        ),
    ),
)
async def test_planner_read_denials_do_not_call_a_reader(
    tmp_path: Path,
    tool_request: ToolRequest,
    context_overrides: dict[str, object],
    run_overrides: dict[str, object],
    execution_current: bool,
    expected_code: ToolErrorCode,
) -> None:
    (tmp_path / "README.md").write_text("Forge", encoding="utf-8")
    base_run = RunSnapshot(
        id=RUN_ID,
        project_id=PROJECT_ID,
        task_id=TASK_ID,
        state=RunState.PLANNING,
        policy_version=1,
    )
    service, work, reader = _service(tmp_path, replace(base_run, **run_overrides))
    work.tool_calls.current = execution_current

    result = await service.invoke(_context(**context_overrides), tool_request)

    assert result.status is ToolCallStatus.DENIED
    assert reader.calls == 0
    assert result.error is not None
    assert result.error.code is expected_code


@pytest.mark.parametrize(
    "tool_request",
    (
        ToolRequest(name=ToolName.REPOSITORY_WRITE_FILE, arguments={"path": "x", "content": "x"}),
        ToolRequest(name=ToolName.GIT_STATUS, arguments={}),
        ToolRequest(name=ToolName.BUILD_RUN_NAMED_CHECK, arguments={"command_name": "unit"}),
    ),
)
async def test_planner_never_gains_write_git_or_build_capabilities(
    tmp_path: Path, tool_request: ToolRequest
) -> None:
    service, _, reader = _service(tmp_path)

    result = await service.invoke(_context(), tool_request)

    assert result.status is ToolCallStatus.DENIED
    assert result.error is not None
    assert result.error.code is ToolErrorCode.AUTHORIZATION_DENIED
    assert reader.calls == 0
