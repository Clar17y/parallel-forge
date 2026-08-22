from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from uuid import UUID

import pytest
from forge.application.ports.repository import ProcessResult
from forge.application.ports.worktrees import ManagedWorktree
from forge.domain.resource import WorktreeIdentity
from forge.tools.git import ControlledGit, ControlledGitError
from forge.tools.paths import CanonicalRoot

PROJECT_ID = UUID("11111111-1111-1111-1111-111111111111")
RUN_ID = UUID("22222222-2222-2222-2222-222222222222")


class CapturingRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], str, dict[str, str]]] = []

    def run_argv(
        self,
        argv: tuple[str, ...] | list[str],
        *,
        cwd: str,
        environment: dict[str, str],
        timeout_seconds: float | None = None,
    ) -> ProcessResult:
        del timeout_seconds
        command = tuple(argv)
        self.calls.append((command, cwd, dict(environment)))
        if "config" in command:
            stdout = "core.repositoryformatversion\x00"
        elif "branch" in command:
            stdout = "feat\n"
        else:
            stdout = "## feat\x00"
        return ProcessResult(
            return_code=0,
            stdout=stdout,
            stderr="",
            timed_out=False,
            stdout_original_byte_count=len(stdout.encode()),
            stderr_original_byte_count=0,
            stdout_truncated=False,
            stderr_truncated=False,
        )


class BranchRunner(CapturingRunner):
    def __init__(self, branch: str) -> None:
        super().__init__()
        self.branch = branch

    def run_argv(
        self,
        argv: tuple[str, ...] | list[str],
        *,
        cwd: str,
        environment: dict[str, str],
        timeout_seconds: float | None = None,
    ) -> ProcessResult:
        result = super().run_argv(
            argv,
            cwd=cwd,
            environment=environment,
            timeout_seconds=timeout_seconds,
        )
        if "branch" in tuple(argv):
            return ProcessResult(
                return_code=0,
                stdout=f"{self.branch}\n",
                stderr="",
                timed_out=False,
                stdout_original_byte_count=len(self.branch.encode()) + 1,
                stderr_original_byte_count=0,
                stdout_truncated=False,
                stderr_truncated=False,
            )
        return result


class TruncatedHeadRunner(CapturingRunner):
    def run_argv(
        self,
        argv: tuple[str, ...] | list[str],
        *,
        cwd: str,
        environment: dict[str, str],
        timeout_seconds: float | None = None,
    ) -> ProcessResult:
        result = super().run_argv(
            argv,
            cwd=cwd,
            environment=environment,
            timeout_seconds=timeout_seconds,
        )
        if "rev-parse" in tuple(argv):
            return ProcessResult(
                return_code=0,
                stdout="a" * 40,
                stderr="",
                timed_out=False,
                stdout_original_byte_count=41,
                stderr_original_byte_count=0,
                stdout_truncated=True,
                stderr_truncated=False,
            )
        return result


def _git(repository: Path, *arguments: str) -> None:
    result = subprocess.run(
        [shutil.which("git") or "git", "-C", str(repository), *arguments],
        check=False,
        capture_output=True,
        shell=False,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def _managed_repository(tmp_path: Path) -> tuple[Path, WorktreeIdentity, ManagedWorktree]:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "user.name", "Forge Test")
    _git(repository, "config", "user.email", "forge@example.test")
    (repository / "README.md").write_text("forge\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-m", "initial")
    (repository / ".worktrees").mkdir()
    identity = WorktreeIdentity.for_run(PROJECT_ID, RUN_ID, branch="feat", database_enabled=False)
    path = repository / ".worktrees" / identity.worktree_name
    _git(repository, "worktree", "add", "-b", identity.branch, str(path), "HEAD")
    base_sha = subprocess.run(
        [shutil.which("git") or "git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        shell=False,
        text=True,
    ).stdout.strip()
    return repository, identity, ManagedWorktree(identity=identity, path=path, base_sha=base_sha)


def test_managed_worktree_is_immutable_and_requires_lowercase_base_sha(tmp_path: Path) -> None:
    repository, identity, handle = _managed_repository(tmp_path)
    assert handle.identity == identity
    assert handle.path == repository / ".worktrees" / identity.worktree_name
    with pytest.raises((AttributeError, TypeError)):
        handle.path = repository  # type: ignore[misc]
    with pytest.raises(ValueError):
        ManagedWorktree(identity=identity, path=handle.path, base_sha="A" * 40)


def test_status_uses_exact_forge_prefix_and_sanitized_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, _identity, handle = _managed_repository(tmp_path)
    state_root = tmp_path / "state"
    runner = CapturingRunner()
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "--bad-config")
    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh attacker")
    monkeypatch.setenv("SSH_ASKPASS", "attacker-askpass")
    monkeypatch.setenv("HTTP_PROXY", "http://attacker.invalid")
    monkeypatch.setenv("EDITOR", "attacker-editor")
    monkeypatch.setenv("FORGE_SECRET", "do-not-copy")

    controlled = ControlledGit(
        CanonicalRoot(repository),
        default_branch="main",
        state_root=state_root,
        runner=runner,
    )
    result = controlled.status(handle)

    assert result.text == "## feat\x00"
    assert len(runner.calls) == 3
    git_executable = runner.calls[0][0][0]
    assert Path(git_executable).is_absolute()
    for argv, cwd, environment in runner.calls:
        assert argv[0] == git_executable
        assert argv[1:3] == ("-C", str(handle.path))
        assert "--no-pager" in argv
        assert cwd == str(handle.path)
        assert environment["GIT_TERMINAL_PROMPT"] == "0"
        assert environment["GIT_CONFIG_NOSYSTEM"] == "1"
        assert environment["GIT_ASKPASS"] == ""
        assert environment["GIT_PAGER"] == ""
        assert environment["GIT_CONFIG_GLOBAL"]
        assert environment["GIT_CONFIG_GLOBAL"] != str(repository)
        assert "GIT_CONFIG_PARAMETERS" not in environment
        assert "GIT_SSH_COMMAND" not in environment
        assert "SSH_ASKPASS" not in environment
        assert "HTTP_PROXY" not in environment
        assert "EDITOR" not in environment
        assert "FORGE_SECRET" not in environment


def test_status_and_diff_use_deterministic_safety_flags(tmp_path: Path) -> None:
    repository, _identity, handle = _managed_repository(tmp_path)
    runner = CapturingRunner()
    controlled = ControlledGit(
        CanonicalRoot(repository),
        default_branch="main",
        state_root=tmp_path / "state",
        runner=runner,
    )

    controlled.status(handle)
    controlled.diff(handle)

    status_call = next(argv for argv, _cwd, _env in runner.calls if "status" in argv)
    assert status_call[-6:] == (
        "status",
        "--porcelain=v1",
        "--branch",
        "--untracked-files=all",
        "-z",
        "--",
    )
    diff_call = next(argv for argv, _cwd, _env in runner.calls if "diff" in argv)
    assert diff_call[-8:] == (
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "--binary",
        "--full-index",
        "--no-color",
        "HEAD",
        "--",
    )


def test_unsafe_local_filter_config_is_refused_before_status(tmp_path: Path) -> None:
    repository, _identity, handle = _managed_repository(tmp_path)
    marker = tmp_path / "filter-marker"
    command = f"\"{sys.executable}\" -c \"open(r'{marker}', 'w').write('executed')\""
    _git(repository, "config", "filter.evil.clean", command)

    controlled = ControlledGit(
        CanonicalRoot(repository),
        default_branch="main",
        state_root=tmp_path / "state",
    )
    with pytest.raises(ControlledGitError):
        controlled.status(handle)
    assert not marker.exists()


def test_nonempty_trusted_hooks_are_refused(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    hooks = state_root / "hooks"
    hooks.mkdir(parents=True)
    (hooks / "marker").write_text("no", encoding="utf-8")
    repository, _identity, _handle = _managed_repository(tmp_path)

    with pytest.raises(ControlledGitError):
        ControlledGit(
            CanonicalRoot(repository),
            default_branch="main",
            state_root=state_root,
            runner=CapturingRunner(),
        )
    assert (hooks / "marker").read_text(encoding="utf-8") == "no"


def test_handle_path_must_be_exact_registered_worktree(tmp_path: Path) -> None:
    repository, identity, handle = _managed_repository(tmp_path)
    controlled = ControlledGit(
        CanonicalRoot(repository),
        default_branch="main",
        state_root=tmp_path / "state",
        runner=CapturingRunner(),
    )

    outside = ManagedWorktree(
        identity=identity, path=tmp_path / "outside", base_sha=handle.base_sha
    )
    sibling = ManagedWorktree(
        identity=identity,
        path=handle.path.with_name(f"{identity.worktree_name}-sibling"),
        base_sha=handle.base_sha,
    )
    for invalid in (outside, sibling):
        with pytest.raises(ControlledGitError):
            controlled.status(invalid)

    unregistered_path = repository / ".worktrees" / identity.worktree_name
    shutil.rmtree(unregistered_path)
    unregistered_path.mkdir()
    (unregistered_path / ".git").mkdir()
    with pytest.raises(ControlledGitError):
        controlled.status(handle)


def test_handle_path_rejects_symlink_or_reparse_target(tmp_path: Path) -> None:
    repository, _identity, handle = _managed_repository(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    shutil.rmtree(handle.path)
    try:
        handle.path.symlink_to(outside, target_is_directory=True)
    except OSError, NotImplementedError:
        pytest.skip("symlinks are not available on this host")
    controlled = ControlledGit(
        CanonicalRoot(repository),
        default_branch="main",
        state_root=tmp_path / "state",
        runner=CapturingRunner(),
    )
    with pytest.raises(ControlledGitError):
        controlled.status(handle)


def test_wrong_recorded_branch_fails_before_requested_operation(tmp_path: Path) -> None:
    repository, _identity, handle = _managed_repository(tmp_path)
    runner = BranchRunner("other")
    controlled = ControlledGit(
        CanonicalRoot(repository),
        default_branch="main",
        state_root=tmp_path / "state",
        runner=runner,
    )
    with pytest.raises(ControlledGitError):
        controlled.status(handle)
    assert not any("status" in argv for argv, _cwd, _env in runner.calls)


def test_identity_helpers_return_lowercase_sha_and_ancestry(tmp_path: Path) -> None:
    repository, _identity, handle = _managed_repository(tmp_path)
    controlled = ControlledGit(
        CanonicalRoot(repository),
        default_branch="main",
        state_root=tmp_path / "state",
    )

    assert controlled.current_branch(handle) == "feat"
    assert controlled.head_sha(handle) == handle.base_sha
    assert controlled.branch_exists(handle) is True
    assert controlled.is_ancestor(handle) is True


def test_identity_sha_decisions_fail_on_truncated_or_malformed_output(tmp_path: Path) -> None:
    repository, _identity, handle = _managed_repository(tmp_path)
    controlled = ControlledGit(
        CanonicalRoot(repository),
        default_branch="main",
        state_root=tmp_path / "state",
        runner=TruncatedHeadRunner(),
    )
    with pytest.raises(ControlledGitError):
        controlled.head_sha(handle)
