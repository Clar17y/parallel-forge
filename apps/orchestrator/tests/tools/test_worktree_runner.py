from __future__ import annotations

import asyncio
import shutil
import subprocess
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from forge.application.ports.runner import (
    CommandResult,
    CommandTerminalResult,
    RunCommandRequest,
    WorktreeRunnerFactoryPort,
    WorktreeRunnerPort,
)
from forge.application.ports.worktrees import ManagedWorktree
from forge.domain.policy import CommandSpec, ProjectPolicy, RunnerMode, StepKind
from forge.domain.resource import WorktreeIdentity
from forge.tools.git import ControlledGit
from forge.tools.paths import CanonicalRoot
from forge.tools.runner import RunnerExecutionError
from forge.tools.worktree_runner import WorktreeRunnerFactory


def test_terminal_result_is_immutable_and_validated() -> None:
    result = CommandResult(
        command_name="named-test",
        kind=StepKind.TEST,
        command_digest="a" * 64,
        policy_version=1,
        exit_code=0,
        timed_out=False,
        started_at=datetime.now(UTC),
        duration_ms=1,
        stdout_digest="b" * 64,
        stderr_digest="c" * 64,
        runner_mode=RunnerMode.DOCKER,
        image_digest="sha256:" + "d" * 64,
        network_enabled=False,
        stdout_original_byte_count=0,
        stderr_original_byte_count=0,
        stdout_truncated=False,
        stderr_truncated=False,
        unsandboxed=False,
    )
    terminal = CommandTerminalResult(result=result, caller_cancelled=True)

    assert terminal.result is result
    assert terminal.caller_cancelled is True
    assert "literal-secret" not in repr(terminal)
    try:
        terminal.caller_cancelled = False  # type: ignore[misc]
    except AttributeError:
        pass
    else:
        raise AssertionError("terminal result must be immutable")


def test_factory_contract_exposes_compatibility_and_terminal_methods(
    managed_case: tuple[ControlledGit, ManagedWorktree, ProjectPolicy, _Process],
) -> None:
    controlled, worktree, policy, _ = managed_case
    factory: WorktreeRunnerFactoryPort = WorktreeRunnerFactory(
        controlled,
        process_runner=_Process(),
        artifact_store=_Artifacts(),
        audit=_Audit(),
    )
    bound: WorktreeRunnerPort = factory.create(worktree, policy)

    assert isinstance(bound, WorktreeRunnerPort)
    assert callable(bound.run)
    assert callable(bound.run_terminal)


@dataclass
class _ProcessResult:
    return_code: int | None = 0
    stdout: str = "ok\n"
    stderr: str = ""
    timed_out: bool = False
    stdout_original_byte_count: int = 3
    stderr_original_byte_count: int = 0
    stdout_truncated: bool = False
    stderr_truncated: bool = False


class _Process:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []

    def run_argv(self, argv: tuple[str, ...], **kwargs: Any) -> _ProcessResult:
        self.calls.append((argv, kwargs))
        return _ProcessResult()


class _DockerProcess(_Process):
    def run_argv(self, argv: tuple[str, ...], **kwargs: Any) -> _ProcessResult:
        self.calls.append((argv, kwargs))
        if argv[:2] == ("docker", "inspect"):
            return _ProcessResult(
                return_code=1,
                stdout="",
                stderr=f"Error: No such object: {argv[-1]}\n",
                stdout_original_byte_count=0,
            )
        return _ProcessResult()


class _DockerTimeoutProcess(_Process):
    def __init__(self, *, foreign_after_launch: bool = False) -> None:
        super().__init__()
        self.foreign_after_launch = foreign_after_launch
        self.launched = False
        self.removed = False
        self.container_id = "a" * 64
        self.owner_token = ""

    def run_argv(self, argv: tuple[str, ...], **kwargs: Any) -> _ProcessResult:
        self.calls.append((argv, kwargs))
        if argv[:2] == ("docker", "inspect"):
            if not self.launched or self.removed:
                return _ProcessResult(
                    return_code=1,
                    stdout="",
                    stderr=f"Error: No such object: {argv[-1]}\n",
                    stdout_original_byte_count=0,
                )
            label = "foreign-token" if self.foreign_after_launch else self.owner_token
            return _ProcessResult(
                stdout=f"{self.container_id}\t{label}\n",
                stderr="",
                stdout_original_byte_count=66,
            )
        if argv[:3] == ("docker", "rm", "-f"):
            self.removed = True
            return _ProcessResult(stdout="", stderr="", stdout_original_byte_count=0)
        self.launched = True
        label_index = argv.index("--label") + 1
        self.owner_token = argv[label_index].split("=", 1)[1]
        return _ProcessResult(
            return_code=-9, timed_out=True, stdout="", stderr="", stdout_original_byte_count=0
        )


class _BlockingProcess(_Process):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()

    def run_argv(self, argv: tuple[str, ...], **kwargs: Any) -> _ProcessResult:
        self.calls.append((argv, kwargs))
        self.started.set()
        self.release.wait(timeout=5)
        self.finished.set()
        return _ProcessResult()


class _Artifacts:
    def __init__(self) -> None:
        self.values: list[bytes] = []

    async def put_bytes(self, data: bytes, **kwargs: Any) -> Any:
        from forge.domain.artifact import ArtifactDescriptor

        self.values.append(data)
        digest = f"{len(self.values):064x}"
        return ArtifactDescriptor(
            digest=digest,
            media_type=kwargs["media_type"],
            byte_count=len(data),
            storage_path=Path("sha256") / digest[:2] / f"{digest[2:]}.blob",
        )


class _Audit:
    async def record(self, event_type: str, *, priority: str, payload: dict[str, object]) -> None:
        del event_type, priority, payload


def _git(repository: Path, *arguments: str) -> None:
    result = subprocess.run(
        [shutil.which("git") or "git", "-C", str(repository), *arguments],
        capture_output=True,
        check=False,
        shell=False,
        text=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.fixture
def managed_case(tmp_path: Path) -> tuple[ControlledGit, ManagedWorktree, ProjectPolicy, _Process]:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "user.name", "Forge Test")
    _git(repository, "config", "user.email", "forge@example.test")
    (repository / "README.md").write_text("forge\n", encoding="utf-8")
    (repository / ".gitignore").write_text(".worktrees/\n", encoding="utf-8")
    _git(repository, "add", "README.md", ".gitignore")
    _git(repository, "commit", "-m", "initial")
    project_id = uuid4()
    run_id = uuid4()
    identity = WorktreeIdentity.for_run(project_id, run_id, "feature/bound", False)
    target = repository / ".worktrees" / identity.worktree_name
    _git(repository, "worktree", "add", "-b", identity.branch, str(target), "HEAD")
    base_sha = subprocess.check_output(
        [shutil.which("git") or "git", "-C", str(repository), "rev-parse", "HEAD"],
        text=True,
    ).strip()
    worktree = ManagedWorktree(
        identity=identity,
        path=target,
        base_sha=base_sha,
    )
    root = CanonicalRoot(repository)
    controlled = ControlledGit(
        root,
        default_branch="main",
        state_root=tmp_path / "forge-state",
        git_executable=shutil.which("git") or "git",
    )
    command = CommandSpec(
        kind=StepKind.TEST,
        name="bound-test",
        argv=("python", "--version"),
        timeout_seconds=10,
    )
    policy = ProjectPolicy(
        id=project_id,
        version=1,
        repository_path=str(repository.resolve()),
        github_repository="local/bound",
        default_branch="main",
        runner_mode=RunnerMode.TRUSTED_HOST,
        trusted_project=True,
        commands=(command,),
    )
    process = _Process()
    return controlled, worktree, policy, process


def test_bound_runner_uses_exact_managed_worktree_cwd(
    managed_case: tuple[ControlledGit, ManagedWorktree, ProjectPolicy, _Process],
) -> None:
    controlled, worktree, policy, process = managed_case
    runner = WorktreeRunnerFactory(
        controlled,
        process_runner=process,
        artifact_store=_Artifacts(),
        audit=_Audit(),
    ).create(worktree, policy)

    terminal = asyncio.run(
        runner.run_terminal(RunCommandRequest(command_name="bound-test", kind=StepKind.TEST))
    )

    assert terminal.result.exit_code == 0
    assert process.calls[0][1]["cwd"] == worktree.path
    assert str(worktree.path) not in repr(runner)


def test_bound_docker_runner_uses_one_capability_mount_and_ownership_label(
    managed_case: tuple[ControlledGit, ManagedWorktree, ProjectPolicy, _Process],
) -> None:
    controlled, worktree, policy, _ = managed_case
    docker_policy = policy.model_copy(update={"runner_mode": RunnerMode.DOCKER})
    process = _DockerProcess()
    runner = WorktreeRunnerFactory(
        controlled,
        image_digest="sha256:" + "e" * 64,
        process_runner=process,
        artifact_store=_Artifacts(),
    ).create(worktree, docker_policy)

    terminal = asyncio.run(
        runner.run_terminal(RunCommandRequest(command_name="bound-test", kind=StepKind.TEST))
    )

    launch = process.calls[1][0]
    assert terminal.result.exit_code == 0
    assert launch.count("--mount") == 1
    mount = launch[launch.index("--mount") + 1]
    assert "dst=/workspace" in mount
    assert "--workdir=/workspace" in launch
    assert launch.count("--label") == 1
    assert sum(value.startswith("forge.owner-token=") for value in launch) == 1
    if shutil.which("docker") and Path("/proc").is_dir():
        assert "/proc/" in mount
        assert str(worktree.path) not in mount
    else:
        assert str(worktree.path) in mount
    assert "forge.owner-token=" not in repr(terminal)


def test_bound_docker_cleanup_refuses_foreign_replacement(
    managed_case: tuple[ControlledGit, ManagedWorktree, ProjectPolicy, _Process],
) -> None:
    controlled, worktree, policy, _ = managed_case
    docker_policy = policy.model_copy(update={"runner_mode": RunnerMode.DOCKER})
    process = _DockerTimeoutProcess(foreign_after_launch=True)
    runner = WorktreeRunnerFactory(
        controlled,
        image_digest="sha256:" + "f" * 64,
        process_runner=process,
        artifact_store=_Artifacts(),
    ).create(worktree, docker_policy)

    with pytest.raises(RunnerExecutionError, match="^runner execution failed$"):
        asyncio.run(
            runner.run_terminal(RunCommandRequest(command_name="bound-test", kind=StepKind.TEST))
        )

    assert not any(call[0][:3] == ("docker", "rm", "-f") for call in process.calls)


@pytest.mark.asyncio
async def test_bound_terminal_defers_cancellation_until_process_and_capability_release(
    managed_case: tuple[ControlledGit, ManagedWorktree, ProjectPolicy, _Process],
) -> None:
    controlled, worktree, policy, _ = managed_case
    process = _BlockingProcess()
    runner = WorktreeRunnerFactory(
        controlled,
        process_runner=process,
        artifact_store=_Artifacts(),
        audit=_Audit(),
    ).create(worktree, policy)
    task = asyncio.create_task(
        runner.run_terminal(RunCommandRequest(command_name="bound-test", kind=StepKind.TEST))
    )
    assert await asyncio.to_thread(process.started.wait, 1)
    status_task = asyncio.create_task(asyncio.to_thread(controlled.status, worktree))
    task.cancel()
    task.cancel()
    await asyncio.sleep(0.05)
    assert not task.done()
    assert not status_task.done()

    process.release.set()
    terminal = await task
    await status_task
    assert process.finished.is_set()
    assert terminal.caller_cancelled is True
