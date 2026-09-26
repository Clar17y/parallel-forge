from __future__ import annotations

import asyncio
import contextlib
import os
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
from forge.tools.paths import CanonicalRoot, RepositoryAccessDenied
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


@pytest.mark.parametrize("committed", [False, True])
def test_bound_runner_uses_exact_managed_worktree_cwd(
    managed_case: tuple[ControlledGit, ManagedWorktree, ProjectPolicy, _Process],
    committed: bool,
) -> None:
    controlled, worktree, policy, process = managed_case
    if committed:
        (worktree.path / "README.md").write_text("changed\n", encoding="utf-8")
        controlled.commit(worktree, "Change README")
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


def test_writer_can_repair_a_committed_candidate_without_relaxing_environment_capability(
    managed_case,
) -> None:
    from forge.tools.git import ControlledGitError
    from forge.tools.repository_writer import WorktreeRepositoryWriter

    controlled, worktree, policy, _ = managed_case
    (worktree.path / "README.md").write_text("first change\n", encoding="utf-8")
    controlled.commit(worktree, "First change")
    writer = WorktreeRepositoryWriter(controlled, worktree, policy)
    result = writer.write_file("README.md", "repaired\n")
    assert writer.inspect_file("README.md", result.output_digest) is not None
    assert (worktree.path / "README.md").read_text() == "repaired\n"
    with pytest.raises(ControlledGitError), controlled.open_worktree_capability(worktree, policy):
        pytest.fail("environment capability must still require the base commit")


def test_operational_capability_rejects_head_changes_during_its_lifetime(managed_case) -> None:
    from forge.tools.git import ControlledGitError

    controlled, worktree, policy, _ = managed_case
    with (
        pytest.raises(ControlledGitError),
        controlled.open_worktree_capability(
            worktree, policy, allow_committed_changes=True
        ) as capability,
    ):
        _git(worktree.path, "commit", "--allow-empty", "-m", "External candidate change")
        capability.revalidate()


def test_writer_retries_contended_worktree_admission_before_writing(managed_case, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    from forge.tools.git import ControlledGitError
    from forge.tools.repository_writer import WorktreeRepositoryWriter

    controlled, worktree, policy, _ = managed_case
    writer = WorktreeRepositoryWriter(controlled, worktree, policy)
    busy_seen = threading.Event()
    original_open = ControlledGit.open_worktree_capability

    @contextlib.contextmanager
    def observed_open(*args, **kwargs):
        try:
            with original_open(*args, **kwargs) as capability:
                yield capability
        except ControlledGitError:
            busy_seen.set()
            raise

    with ThreadPoolExecutor(max_workers=1) as executor:
        with controlled.open_worktree_capability(worktree, policy):
            monkeypatch.setattr(ControlledGit, "open_worktree_capability", observed_open)
            pending = executor.submit(writer.write_file, "README.md", "after contention\n")
            assert busy_seen.wait(10), "second writer must encounter the held OS lock"
            assert (worktree.path / "README.md").read_text() != "after contention\n"
        result = pending.result(timeout=10)
    assert result.path == "README.md"
    assert (worktree.path / "README.md").read_text() == "after contention\n"


@pytest.mark.parametrize("phase", ["admission", "body", "release"])
def test_writer_does_not_retry_safety_denials_or_admitted_effects(managed_case, monkeypatch, phase):
    from types import SimpleNamespace

    from forge.tools import repository_writer
    from forge.tools.git import ControlledGitBusy, ControlledGitError
    from forge.tools.repository_writer import RepositoryWriteError, WorktreeRepositoryWriter

    controlled, worktree, policy, _ = managed_case
    calls = []

    def failed_write(*args, **kwargs):
        calls.append("write")
        if phase == "body":
            raise ControlledGitBusy()
        return None, "a" * 64, 1, "README.md"

    @contextlib.contextmanager
    def denied_open(*args, **kwargs):
        calls.append("open")
        if phase == "admission":
            raise ControlledGitError()
        yield SimpleNamespace(write_repository_file=failed_write)
        if phase == "release":
            raise ControlledGitBusy()

    monkeypatch.setattr(ControlledGit, "open_worktree_capability", denied_open)
    monkeypatch.setattr(
        repository_writer,
        "time",
        SimpleNamespace(monotonic=lambda: 0, sleep=lambda _: pytest.fail("unsafe retry")),
    )
    with pytest.raises(RepositoryWriteError):
        WorktreeRepositoryWriter(controlled, worktree, policy).write_file("README.md", "x")
    assert calls == (["open"] if phase == "admission" else ["open", "write"])


def test_writer_lock_admission_deadline_is_bounded_without_real_waits(managed_case, monkeypatch):
    from types import SimpleNamespace

    from forge.tools import repository_writer
    from forge.tools.git import ControlledGitBusy
    from forge.tools.repository_writer import RepositoryWriteError, WorktreeRepositoryWriter

    controlled, worktree, policy, _ = managed_case
    now = 0.0
    attempts = []

    def advance(seconds):
        nonlocal now
        assert 0 < seconds <= 0.05
        now += seconds

    @contextlib.contextmanager
    def busy_open(*args, **kwargs):
        attempts.append(now)
        raise ControlledGitBusy()
        yield  # pragma: no cover - retain the context-manager protocol

    monkeypatch.setattr(ControlledGit, "open_worktree_capability", busy_open)
    monkeypatch.setattr(
        repository_writer, "time", SimpleNamespace(monotonic=lambda: now, sleep=advance)
    )
    with pytest.raises(RepositoryWriteError):
        WorktreeRepositoryWriter(controlled, worktree, policy).write_file("README.md", "x")
    assert now == 5.0
    assert 2 <= len(attempts) <= 102


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
    assert ("--workdir=/workspace" if os.name == "nt" else "--workdir=/") in launch
    assert launch.count("--label") == 1
    assert sum(value.startswith("forge.owner-token=") for value in launch) == 1
    assert str(worktree.path) in mount
    if os.name != "nt":
        metadata = worktree.path.stat()
        image_index = launch.index("sha256:" + "e" * 64)
        assert (
            launch[image_index + 1] == Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        )
        assert launch[image_index + 2 : image_index + 4] == (
            str(metadata.st_dev),
            str(metadata.st_ino),
        )
        assert launch[launch.index("--entrypoint") + 1] == "/usr/local/bin/forge-mount-guard"
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controlled, worktree, policy, _ = managed_case
    process = _BlockingProcess()
    capability_released = threading.Event()
    open_capability = controlled.open_worktree_capability

    @contextlib.contextmanager
    def observed_capability(*args: Any, **kwargs: Any) -> Any:
        try:
            with open_capability(*args, **kwargs) as capability:
                yield capability
        finally:
            capability_released.set()

    monkeypatch.setattr(controlled, "open_worktree_capability", observed_capability)
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
    task.cancel()
    task.cancel()
    await asyncio.sleep(0.05)
    assert not task.done()
    assert not capability_released.is_set()

    process.release.set()
    terminal = await task
    assert process.finished.is_set()
    assert capability_released.is_set()
    assert terminal.caller_cancelled is True


@pytest.mark.asyncio
async def test_bound_docker_cancellation_survives_access_lease_restoration_failure(
    managed_case: tuple[ControlledGit, ManagedWorktree, ProjectPolicy, _Process],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lease cleanup must not replace a cancellation with an ACL availability error."""

    controlled, worktree, policy, _ = managed_case
    docker_policy = policy.model_copy(update={"runner_mode": RunnerMode.DOCKER})
    bound = WorktreeRunnerFactory(
        controlled,
        image_digest="sha256:" + "e" * 64,
        process_runner=_DockerProcess(),
        artifact_store=_Artifacts(),
    ).create(worktree, docker_policy)
    delegate = bound._delegate
    entered = asyncio.Event()

    @contextlib.contextmanager
    def failing_lease(*args: Any, **kwargs: Any) -> Any:
        yield
        raise RepositoryAccessDenied("Docker ACL restoration failed")

    async def cancelled_terminal(*args: Any, **kwargs: Any) -> CommandTerminalResult:
        entered.set()
        try:
            await asyncio.Future[None]()
        except asyncio.CancelledError:
            return CommandTerminalResult(
                result=CommandResult(
                    command_name="bound-test",
                    kind=StepKind.TEST,
                    command_digest="a" * 64,
                    policy_version=1,
                    exit_code=0,
                    timed_out=False,
                    started_at=datetime.now(UTC),
                    duration_ms=0,
                    stdout_digest="b" * 64,
                    stderr_digest="c" * 64,
                    runner_mode=RunnerMode.DOCKER,
                    image_digest="sha256:" + "e" * 64,
                    network_enabled=False,
                    stdout_original_byte_count=0,
                    stderr_original_byte_count=0,
                    stdout_truncated=False,
                    stderr_truncated=False,
                    unsandboxed=False,
                ),
                caller_cancelled=True,
            )

    monkeypatch.setattr(delegate, "managed_access_lease", failing_lease)
    monkeypatch.setattr(delegate, "_run_terminal_at", cancelled_terminal)
    task = asyncio.create_task(
        bound.run_terminal(RunCommandRequest(command_name="bound-test", kind=StepKind.TEST))
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError) as cancellation:
        await task
    assert task.cancelled()
    assert "Docker ACL restoration failed" in str(cancellation.value.__cause__)
