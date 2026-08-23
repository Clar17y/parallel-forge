from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest
from forge.application.ports.repository import ProcessResult
from forge.application.ports.worktrees import GitCommit, ManagedWorktree
from forge.domain.resource import WorktreeIdentity
from forge.tools import git as git_module
from forge.tools.git import ControlledGit, ControlledGitError
from forge.tools.paths import CanonicalRoot
from forge.tools.process import ProcessRunner

PROJECT_ID = UUID("11111111-1111-1111-1111-111111111111")
RUN_ID = UUID("22222222-2222-2222-2222-222222222222")
TRUSTED_GIT = Path(shutil.which("git") or "git").resolve()


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


class RecordingRunner:
    def __init__(self, repository: Path) -> None:
        self._root = CanonicalRoot(repository)
        self._delegate = ProcessRunner(self._root)
        self.calls: list[tuple[tuple[str, ...], str, dict[str, str]]] = []
        self.completed_calls: list[tuple[tuple[str, ...], str, dict[str, str]]] = []

    def run_argv(
        self,
        argv: tuple[str, ...] | list[str],
        *,
        cwd: str,
        environment: dict[str, str],
        timeout_seconds: float | None = None,
    ) -> ProcessResult:
        command = tuple(argv)
        call = (command, cwd, dict(environment))
        self.calls.append(call)
        result = self._delegate.run_argv(
            command,
            cwd=cwd,
            environment=environment,
            timeout_seconds=timeout_seconds,
        )
        self.completed_calls.append(call)
        return result


class FailingCommitRunner(RecordingRunner):
    def run_argv(
        self,
        argv: tuple[str, ...] | list[str],
        *,
        cwd: str,
        environment: dict[str, str],
        timeout_seconds: float | None = None,
    ) -> ProcessResult:
        command = tuple(argv)
        if command[-6:-5] == ("commit",):
            self.calls.append((command, cwd, dict(environment)))
            return ProcessResult(
                return_code=1,
                stdout="",
                stderr="commit rejected",
                timed_out=False,
                stdout_original_byte_count=0,
                stderr_original_byte_count=len("commit rejected"),
                stdout_truncated=False,
                stderr_truncated=False,
            )
        return super().run_argv(
            command,
            cwd=cwd,
            environment=environment,
            timeout_seconds=timeout_seconds,
        )


class CommitProofSwapRunner(RecordingRunner):
    def __init__(
        self, repository: Path, worktree: Path, mode: str, trigger: tuple[str, ...]
    ) -> None:
        super().__init__(repository)
        self._worktree = worktree
        self._mode = mode
        self._trigger = trigger
        self.swapped = False

    def run_argv(
        self,
        argv: tuple[str, ...] | list[str],
        *,
        cwd: str,
        environment: dict[str, str],
        timeout_seconds: float | None = None,
    ) -> ProcessResult:
        command = tuple(argv)
        if not self.swapped and command[-len(self._trigger) :] == tuple(self._trigger):
            self.swapped = True
            marker = self._worktree / ".git"
            registration = _registration_for(self._worktree)
            if self._mode == "marker":
                marker.write_text("gitdir: /foreign/registration\n", encoding="utf-8")
            else:
                (registration / "gitdir").write_text(
                    f"{self._worktree / 'foreign.git'}\n", encoding="utf-8"
                )
        return super().run_argv(
            command,
            cwd=cwd,
            environment=environment,
            timeout_seconds=timeout_seconds,
        )


class NamespaceSwapRunner(RecordingRunner):
    def __init__(self, repository: Path, target: Path, outside: Path) -> None:
        super().__init__(repository)
        self._target = target
        self._outside = outside
        self.swap_attempted = False
        self.swap_succeeded = False
        self.redirect_created = False

    def run_argv(
        self,
        argv: tuple[str, ...] | list[str],
        *,
        cwd: str,
        environment: dict[str, str],
        timeout_seconds: float | None = None,
    ) -> ProcessResult:
        command = tuple(argv)
        if command[-6:-5] == ("worktree",) and not self.swap_attempted:
            self.swap_attempted = True
            try:
                self._target.rename(self._target.with_name(f"{self._target.name}-moved"))
                self.swap_succeeded = True
                self._outside.mkdir()
                try:
                    self._target.symlink_to(self._outside, target_is_directory=True)
                    self.redirect_created = True
                except OSError, NotImplementedError:
                    if os.name == "nt":
                        result = subprocess.run(
                            [
                                "cmd.exe",
                                "/c",
                                "mklink",
                                "/J",
                                str(self._target),
                                str(self._outside),
                            ],
                            capture_output=True,
                            check=False,
                            shell=False,
                        )
                        self.redirect_created = result.returncode == 0 and self._target.is_dir()
            except OSError, NotImplementedError:
                pass
        return super().run_argv(
            command,
            cwd=cwd,
            environment=environment,
            timeout_seconds=timeout_seconds,
        )


def _git(repository: Path, *arguments: str) -> None:
    result = subprocess.run(
        [shutil.which("git") or "git", "-C", str(repository), *arguments],
        check=False,
        capture_output=True,
        shell=False,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def _git_show(repository: Path, commit: str) -> str:
    result = subprocess.run(
        [str(TRUSTED_GIT), "-C", str(repository), "show", "-s", "--format=%s", commit],
        check=True,
        capture_output=True,
        shell=False,
        text=True,
    )
    return result.stdout


def _managed_repository(tmp_path: Path) -> tuple[Path, WorktreeIdentity, ManagedWorktree]:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "user.name", "Forge Test")
    _git(repository, "config", "user.email", "forge@example.test")
    _git(repository, "config", "core.autocrlf", "false")
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


def _source_repository(tmp_path: Path, *, ignored: bool = True) -> tuple[Path, str]:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "user.name", "Forge Test")
    _git(repository, "config", "user.email", "forge@example.test")
    (repository / "README.md").write_text("forge\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-m", "initial")
    if ignored:
        (repository / ".gitignore").write_text(".worktrees/\n", encoding="utf-8")
        _git(repository, "add", ".gitignore")
        _git(repository, "commit", "-m", "ignore managed worktrees")
    base_sha = subprocess.run(
        [str(TRUSTED_GIT), "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        shell=False,
        text=True,
    ).stdout.strip()
    return repository, base_sha


def _controlled(repository: Path, state_root: Path, runner: object | None = None) -> ControlledGit:
    root = getattr(runner, "_root", None)
    if not isinstance(root, CanonicalRoot):
        root = CanonicalRoot(repository)
    assert root.path == repository.resolve()
    return ControlledGit(
        root,
        default_branch="main",
        state_root=state_root,
        git_executable=TRUSTED_GIT,
        runner=runner,  # type: ignore[arg-type]
    )


def _registration_for(worktree: Path) -> Path:
    marker = worktree / ".git"
    raw_marker = marker.read_text(encoding="utf-8")
    assert raw_marker.startswith("gitdir: ")
    raw_metadata = raw_marker.removesuffix("\n")[8:]
    metadata = Path(raw_metadata)
    if not metadata.is_absolute():
        metadata = worktree / metadata
    return metadata.resolve()


def _rename_registration(worktree: Path, basename: str) -> Path:
    marker = worktree / ".git"
    metadata = _registration_for(worktree)
    renamed = metadata.with_name(basename)
    marker.chmod(stat.S_IRUSR | stat.S_IWUSR)
    marker.unlink()
    marker.write_bytes(f"gitdir: {renamed}\n".encode())
    metadata.rename(renamed)
    return renamed


def _add_registration_candidate(metadata_root: Path, name: str, target: Path) -> Path:
    metadata = metadata_root / name
    metadata.mkdir()
    (metadata / "gitdir").write_bytes(f"{target}\n".encode())
    return metadata


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
        git_executable=TRUSTED_GIT,
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
        git_executable=TRUSTED_GIT,
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


@pytest.mark.parametrize("basename", ("-", "registered-under-another-name"))
def test_registered_worktree_metadata_basename_is_not_identity_name(
    tmp_path: Path, basename: str
) -> None:
    repository, _identity, handle = _managed_repository(tmp_path)
    _rename_registration(handle.path, basename)
    controlled = ControlledGit(
        CanonicalRoot(repository),
        default_branch="main",
        state_root=tmp_path / "state",
        git_executable=TRUSTED_GIT,
        runner=CapturingRunner(),
    )

    controlled.status(handle)


def test_unrelated_valid_registration_does_not_hide_exact_target_match(tmp_path: Path) -> None:
    repository, _identity, handle = _managed_repository(tmp_path)
    metadata_root = repository / ".git" / "worktrees"
    unrelated_target = tmp_path / "unrelated" / ".git"
    unrelated_target.mkdir(parents=True)
    _add_registration_candidate(metadata_root, "unrelated-registration", unrelated_target)
    controlled = ControlledGit(
        CanonicalRoot(repository),
        default_branch="main",
        state_root=tmp_path / "state",
        git_executable=TRUSTED_GIT,
        runner=CapturingRunner(),
    )

    controlled.status(handle)


def test_duplicate_exact_target_registrations_fail_closed(tmp_path: Path) -> None:
    repository, _identity, handle = _managed_repository(tmp_path)
    metadata = _rename_registration(handle.path, "first-registration")
    duplicate = metadata.with_name("second-registration")
    shutil.copytree(metadata, duplicate)
    controlled = ControlledGit(
        CanonicalRoot(repository),
        default_branch="main",
        state_root=tmp_path / "state",
        git_executable=TRUSTED_GIT,
        runner=CapturingRunner(),
    )

    with pytest.raises(ControlledGitError):
        controlled.status(handle)


@pytest.mark.parametrize(
    "contents",
    (b"/missing/target", b"/first\n/second\n", b"\xff\n", b"x" * 4097),
)
def test_malformed_registration_candidate_fails_closed(tmp_path: Path, contents: bytes) -> None:
    repository, _identity, handle = _managed_repository(tmp_path)
    metadata_root = repository / ".git" / "worktrees"
    malformed = metadata_root / "malformed-registration"
    malformed.mkdir()
    (malformed / "gitdir").write_bytes(contents)
    controlled = ControlledGit(
        CanonicalRoot(repository),
        default_branch="main",
        state_root=tmp_path / "state",
        git_executable=TRUSTED_GIT,
        runner=CapturingRunner(),
    )

    with pytest.raises(ControlledGitError):
        controlled.status(handle)


def test_linked_registration_candidate_fails_closed(tmp_path: Path) -> None:
    repository, _identity, handle = _managed_repository(tmp_path)
    metadata_root = repository / ".git" / "worktrees"
    outside = tmp_path / "outside-metadata"
    outside.mkdir()
    (outside / "gitdir").write_text(f"{tmp_path / 'unrelated' / '.git'}\n", encoding="utf-8")
    linked = metadata_root / "linked-registration"
    try:
        linked.symlink_to(outside, target_is_directory=True)
    except OSError, NotImplementedError:
        pytest.skip("symlinks are not available on this host")
    controlled = ControlledGit(
        CanonicalRoot(repository),
        default_branch="main",
        state_root=tmp_path / "state",
        git_executable=TRUSTED_GIT,
        runner=CapturingRunner(),
    )

    with pytest.raises(ControlledGitError):
        controlled.status(handle)


def test_registration_entry_cap_fails_closed(tmp_path: Path) -> None:
    repository, _identity, handle = _managed_repository(tmp_path)
    metadata_root = repository / ".git" / "worktrees"
    for index in range(git_module._MAX_METADATA_ENTRIES + 1):
        target = tmp_path / f"unrelated-{index}" / ".git"
        target.mkdir(parents=True)
        _add_registration_candidate(metadata_root, f"unrelated-{index}", target)
    controlled = ControlledGit(
        CanonicalRoot(repository),
        default_branch="main",
        state_root=tmp_path / "state",
        git_executable=TRUSTED_GIT,
        runner=CapturingRunner(),
    )

    with pytest.raises(ControlledGitError):
        controlled.status(handle)


def test_unsafe_local_filter_config_is_refused_before_status(tmp_path: Path) -> None:
    repository, _identity, handle = _managed_repository(tmp_path)
    marker = tmp_path / "filter-marker"
    command = f"\"{sys.executable}\" -c \"open(r'{marker}', 'w').write('executed')\""
    _git(repository, "config", "filter.evil.clean", command)

    controlled = ControlledGit(
        CanonicalRoot(repository),
        default_branch="main",
        state_root=tmp_path / "state",
        git_executable=TRUSTED_GIT,
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
            git_executable=TRUSTED_GIT,
            runner=CapturingRunner(),
        )
    assert (hooks / "marker").read_text(encoding="utf-8") == "no"


def test_handle_path_must_be_exact_registered_worktree(tmp_path: Path) -> None:
    repository, identity, handle = _managed_repository(tmp_path)
    controlled = ControlledGit(
        CanonicalRoot(repository),
        default_branch="main",
        state_root=tmp_path / "state",
        git_executable=TRUSTED_GIT,
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
        git_executable=TRUSTED_GIT,
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
        git_executable=TRUSTED_GIT,
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
        git_executable=TRUSTED_GIT,
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
        git_executable=TRUSTED_GIT,
        runner=TruncatedHeadRunner(),
    )
    with pytest.raises(ControlledGitError):
        controlled.head_sha(handle)


def test_git_commit_result_is_immutable_and_lowercase_sha_bound(tmp_path: Path) -> None:
    result = GitCommit(previous_sha="a" * 40, new_sha="b" * 40)

    assert result.previous_sha == "a" * 40
    assert result.new_sha == "b" * 40
    with pytest.raises((AttributeError, TypeError)):
        result.new_sha = "c" * 40  # type: ignore[misc]
    with pytest.raises(ValueError):
        GitCommit(previous_sha="A" * 40, new_sha="b" * 40)


@pytest.mark.parametrize(
    "message",
    (
        "",
        "   ",
        "line\nfeed",
        "carriage\rreturn",
        "tab\ttext",
        "escape\x1btext",
        "bidi\u202eoverride",
        "zero\u200dwidth",
        "x" * 4097,
    ),
)
def test_commit_rejects_unbounded_or_control_message_before_git(
    tmp_path: Path, message: str
) -> None:
    repository, _identity, handle = _managed_repository(tmp_path)
    runner = RecordingRunner(repository)
    controlled = _controlled(repository, tmp_path / "state", runner)

    with pytest.raises(ControlledGitError):
        controlled.commit(handle, message)

    assert not any(argv[-3:] == ("add", "-A", "--") for argv, _cwd, _env in runner.calls)


def test_commit_stages_new_modified_and_deleted_files_and_verifies_ancestry(
    tmp_path: Path,
) -> None:
    repository, _identity, handle = _managed_repository(tmp_path)
    (handle.path / "README.md").write_text("updated\n", encoding="utf-8")
    (handle.path / "new.txt").write_text("new\n", encoding="utf-8")
    (handle.path / "README.md").replace(handle.path / "renamed.txt")

    controlled = _controlled(repository, tmp_path / "state")
    result = controlled.commit(handle, "  capture all changes  ")

    assert result.previous_sha == handle.base_sha
    assert result.new_sha != result.previous_sha
    assert len(result.new_sha) == 40
    assert result.new_sha == result.new_sha.lower()
    assert controlled.head_sha(handle) == result.new_sha
    assert (handle.path / "renamed.txt").read_text(encoding="utf-8") == "updated\n"
    assert not (handle.path / "README.md").exists()
    assert (handle.path / "new.txt").read_text(encoding="utf-8") == "new\n"
    assert _git_show(repository, result.new_sha).endswith("capture all changes\n")


def test_commit_refuses_no_change_without_creating_a_commit(tmp_path: Path) -> None:
    repository, _identity, handle = _managed_repository(tmp_path)
    runner = RecordingRunner(repository)
    controlled = _controlled(repository, tmp_path / "state", runner)

    with pytest.raises(ControlledGitError):
        controlled.commit(handle, "nothing to commit")

    assert controlled.head_sha(handle) == handle.base_sha
    assert any(argv[-3:] == ("add", "-A", "--") for argv, _cwd, _env in runner.calls)
    assert not any(argv[-6:-5] == ("commit",) for argv, _cwd, _env in runner.calls)


def test_commit_uses_exact_add_and_commit_argv_and_isolated_identity_controls(
    tmp_path: Path,
) -> None:
    repository, _identity, handle = _managed_repository(tmp_path)
    (handle.path / "change.txt").write_text("change\n", encoding="utf-8")
    runner = RecordingRunner(repository)
    controlled = _controlled(repository, tmp_path / "state", runner)

    controlled.commit(handle, "one argument")

    add_call, add_environment = next(
        (argv, environment)
        for argv, _cwd, environment in runner.calls
        if argv[-3:] == ("add", "-A", "--")
    )
    commit_call, commit_environment = next(
        (argv, environment)
        for argv, _cwd, environment in runner.calls
        if argv[-6:] == ("commit", "--no-verify", "--no-gpg-sign", "-m", "one argument", "--")
    )
    assert add_call[-3:] == ("add", "-A", "--")
    assert commit_call[-6:] == (
        "commit",
        "--no-verify",
        "--no-gpg-sign",
        "-m",
        "one argument",
        "--",
    )
    assert commit_call.count("one argument") == 1
    assert "commit.gpgSign=false" in commit_call
    assert "user.name=Forge" in commit_call
    assert "user.email=forge@example.test" in commit_call
    assert commit_environment["GIT_CONFIG_NOSYSTEM"] == "1"
    assert commit_environment["GIT_TERMINAL_PROMPT"] == "0"
    assert commit_environment["GIT_ASKPASS"] == ""
    assert commit_environment["GIT_EDITOR"] == ""
    assert commit_environment["GIT_PAGER"] == ""
    assert commit_environment["GIT_DIR"] != commit_environment["GIT_COMMON_DIR"]
    if os.name == "nt":
        assert commit_environment["GIT_WORK_TREE"] == str(handle.path)
        assert commit_environment["GIT_DIR"] == str(_registration_for(handle.path))
        assert commit_environment["GIT_COMMON_DIR"] == str(repository / ".git")
    else:
        assert commit_environment["GIT_DIR"].startswith("/proc/self/fd/")
        assert commit_environment["GIT_COMMON_DIR"].startswith("/proc/self/fd/")
        assert commit_environment["GIT_WORK_TREE"].startswith("/proc/self/fd/")
        assert (
            len(
                {
                    commit_environment["GIT_DIR"],
                    commit_environment["GIT_COMMON_DIR"],
                    commit_environment["GIT_WORK_TREE"],
                }
            )
            == 3
        )
    assert add_environment["GIT_CONFIG_GLOBAL"] == commit_environment["GIT_CONFIG_GLOBAL"]


def test_commit_accepts_opaque_registration_basename(tmp_path: Path) -> None:
    repository, _identity, handle = _managed_repository(tmp_path)
    _rename_registration(handle.path, "opaque-registration")
    (handle.path / "change.txt").write_text("change\n", encoding="utf-8")

    result = _controlled(repository, tmp_path / "state").commit(handle, "opaque proof")

    assert result.new_sha != result.previous_sha


@pytest.mark.parametrize("mode", ("marker", "registration"))
@pytest.mark.parametrize(
    "trigger",
    (("add", "-A", "--"), ("commit", "--no-verify", "--no-gpg-sign", "-m", "proof swap", "--")),
)
def test_commit_fails_closed_when_bound_proof_is_substituted_before_process(
    tmp_path: Path, mode: str, trigger: tuple[str, ...]
) -> None:
    repository, _identity, handle = _managed_repository(tmp_path)
    (handle.path / "change.txt").write_text("change\n", encoding="utf-8")
    runner = CommitProofSwapRunner(repository, handle.path, mode, trigger)
    controlled = _controlled(repository, tmp_path / "state", runner)
    proof_path = (
        handle.path / ".git" if mode == "marker" else _registration_for(handle.path) / "gitdir"
    )
    original_proof = proof_path.read_bytes()

    try:
        with pytest.raises(ControlledGitError):
            controlled.commit(handle, "proof swap")
    finally:
        if os.name == "nt":
            assert proof_path.read_bytes() == original_proof
        else:
            proof_path.write_bytes(original_proof)

    assert runner.swapped is True
    assert not any(
        argv[-6:] == ("commit", "--no-verify", "--no-gpg-sign", "-m", "proof swap", "--")
        for argv, _cwd, _environment in runner.completed_calls
    )
    assert controlled.head_sha(handle) == handle.base_sha
    if trigger[0] == "commit":
        staged = subprocess.run(
            [str(TRUSTED_GIT), "-C", str(handle.path), "diff", "--cached", "--name-only"],
            check=True,
            capture_output=True,
            shell=False,
            text=True,
        ).stdout.splitlines()
        assert staged == ["change.txt"]


def test_commit_does_not_run_repository_hook(tmp_path: Path) -> None:
    repository, _identity, handle = _managed_repository(tmp_path)
    marker = tmp_path / "hook-marker"
    hook = repository / ".git" / "hooks" / "pre-commit"
    hook.write_text(
        f"#!/bin/sh\nprintf executed > '{marker}'\nexit 1\n",
        encoding="utf-8",
    )
    hook.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    (handle.path / "change.txt").write_text("change\n", encoding="utf-8")

    controlled = _controlled(repository, tmp_path / "state")
    controlled.commit(handle, "hook bypass")

    assert not marker.exists()


@pytest.mark.parametrize("filter_key", ("clean", "smudge", "process"))
def test_commit_refuses_configured_filter_before_staging(tmp_path: Path, filter_key: str) -> None:
    repository, _identity, handle = _managed_repository(tmp_path)
    marker = tmp_path / f"{filter_key}-marker"
    command = f"\"{sys.executable}\" -c \"open(r'{marker}', 'w').write('executed')\""
    _git(repository, "config", f"filter.evil.{filter_key}", command)
    (handle.path / ".gitattributes").write_text("*.txt filter=evil\n", encoding="utf-8")
    (handle.path / "change.txt").write_text("change\n", encoding="utf-8")
    runner = RecordingRunner(repository)
    controlled = _controlled(repository, tmp_path / "state", runner)

    with pytest.raises(ControlledGitError):
        controlled.commit(handle, "unsafe filter")

    assert not marker.exists()
    assert not any(argv[-3:] == ("add", "-A", "--") for argv, _cwd, _env in runner.calls)


def test_commit_refuses_wrong_branch_default_branch_and_diverged_base(
    tmp_path: Path,
) -> None:
    repository, _identity, handle = _managed_repository(tmp_path)
    controlled = _controlled(repository, tmp_path / "state")
    _git(handle.path, "switch", "-c", "other")
    (handle.path / "change.txt").write_text("change\n", encoding="utf-8")
    with pytest.raises(ControlledGitError):
        controlled.commit(handle, "wrong branch")

    default_root = tmp_path / "default"
    default_root.mkdir()
    repository, _identity, handle = _managed_repository(default_root)
    default_identity = replace(handle.identity, branch="main")
    default_handle = replace(handle, identity=default_identity)
    with pytest.raises(ControlledGitError):
        _controlled(repository, tmp_path / "default-state").commit(default_handle, "default")

    diverged_root = tmp_path / "diverged"
    diverged_root.mkdir()
    repository, _identity, handle = _managed_repository(diverged_root)
    _git(repository, "commit", "--allow-empty", "-m", "diverging main")
    diverged_sha = subprocess.run(
        [str(TRUSTED_GIT), "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        shell=False,
        text=True,
    ).stdout.strip()
    diverged_handle = replace(handle, base_sha=diverged_sha)
    with pytest.raises(ControlledGitError):
        _controlled(repository, tmp_path / "diverged-state").commit(
            diverged_handle, "diverged base"
        )


def test_commit_failure_preserves_staged_index_for_reconciliation(tmp_path: Path) -> None:
    repository, _identity, handle = _managed_repository(tmp_path)
    (handle.path / "change.txt").write_text("change\n", encoding="utf-8")
    runner = FailingCommitRunner(repository)
    controlled = _controlled(repository, tmp_path / "state", runner)

    with pytest.raises(ControlledGitError):
        controlled.commit(handle, "preserve staged state")

    staged = subprocess.run(
        [str(TRUSTED_GIT), "-C", str(handle.path), "diff", "--cached", "--name-only"],
        check=True,
        capture_output=True,
        shell=False,
        text=True,
    ).stdout.splitlines()
    assert staged == ["change.txt"]
    assert controlled.head_sha(handle) == handle.base_sha


def test_controlled_git_has_no_dangerous_public_commit_neighbors() -> None:
    for method in (
        "add",
        "amend",
        "branch_delete",
        "checkout",
        "clean",
        "push",
        "rebase",
        "remote",
        "reset",
    ):
        assert not hasattr(ControlledGit, method)


@pytest.mark.parametrize(
    "forged_name",
    ("/absolute", "nested/name", r"nested\name", r"C:\drive", ".", "..", "../escape"),
)
def test_forged_worktree_names_are_rejected_before_git_invocation(
    tmp_path: Path, forged_name: str
) -> None:
    repository, _identity, handle = _managed_repository(tmp_path)
    runner = CapturingRunner()
    _controlled = ControlledGit(
        CanonicalRoot(repository),
        default_branch="main",
        state_root=tmp_path / "state",
        git_executable=TRUSTED_GIT,
        runner=runner,
    )

    with pytest.raises(ValueError):
        replace(handle.identity, worktree_name=forged_name)
    assert runner.calls == []


def test_is_ancestor_does_not_accept_a_caller_selected_ref(tmp_path: Path) -> None:
    repository, _identity, handle = _managed_repository(tmp_path)
    controlled = ControlledGit(
        CanonicalRoot(repository),
        default_branch="main",
        state_root=tmp_path / "state",
        git_executable=TRUSTED_GIT,
    )

    with pytest.raises(TypeError):
        controlled.is_ancestor(handle, handle.base_sha)  # type: ignore[call-arg]


def test_absolute_git_executable_is_mandatory(tmp_path: Path) -> None:
    repository, _identity, _handle = _managed_repository(tmp_path)
    with pytest.raises(TypeError):
        ControlledGit(
            CanonicalRoot(repository),
            default_branch="main",
            state_root=tmp_path / "state",
            runner=CapturingRunner(),
        )


def test_inherited_path_cannot_replace_explicit_trusted_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, _identity, handle = _managed_repository(tmp_path)
    rogue_marker = tmp_path / "rogue-marker"
    rogue_directory = tmp_path / "rogue-bin"
    rogue_directory.mkdir()
    (rogue_directory / "git.exe").write_text(f"rogue marker: {rogue_marker}", encoding="utf-8")
    monkeypatch.setenv("PATH", f"{rogue_directory}{os.pathsep}{os.environ.get('PATH', '')}")
    runner = CapturingRunner()
    controlled = ControlledGit(
        CanonicalRoot(repository),
        default_branch="main",
        state_root=tmp_path / "state",
        git_executable=TRUSTED_GIT,
        runner=runner,
    )

    controlled.status(handle)

    assert runner.calls
    assert all(argv[0] == str(TRUSTED_GIT) for argv, _cwd, _environment in runner.calls)
    assert not rogue_marker.exists()


def test_create_worktree_uses_exact_add_argv_and_verifies_identity(tmp_path: Path) -> None:
    repository, base_sha = _source_repository(tmp_path)
    identity = WorktreeIdentity.for_run(
        PROJECT_ID, RUN_ID, branch="feature/new", database_enabled=False
    )
    runner = RecordingRunner(repository)
    controlled = _controlled(repository, tmp_path / "state", runner)

    handle = controlled.create_worktree(identity, base_sha)

    expected_path = repository / ".worktrees" / identity.worktree_name
    assert handle.identity == identity
    assert handle.path == expected_path
    assert handle.base_sha == base_sha
    assert controlled.current_branch(handle) == identity.branch
    assert controlled.head_sha(handle) == base_sha
    add_call = next(
        argv for argv, _cwd, _environment in runner.calls if argv[-6:-5] == ("worktree",)
    )
    assert add_call[-6:] == (
        "worktree",
        "add",
        "-b",
        identity.branch,
        ".",
        base_sha,
    )
    assert "-C" not in add_call
    add_cwd, add_environment = next(
        (cwd, environment) for argv, cwd, environment in runner.calls if argv == add_call
    )
    assert add_cwd == str(expected_path)
    assert add_environment["GIT_DIR"]
    assert add_environment["GIT_COMMON_DIR"] == add_environment["GIT_DIR"]
    if os.name == "nt":
        assert add_environment["GIT_DIR"] == str(repository / ".git")
    else:
        assert add_environment["GIT_DIR"].startswith("/proc/self/fd/")


@pytest.mark.parametrize("swap_scope", ("root", "leaf"))
def test_create_keeps_git_on_retained_leaf_when_namespace_is_swapped(
    tmp_path: Path, swap_scope: str
) -> None:
    repository, base_sha = _source_repository(tmp_path)
    identity = WorktreeIdentity.for_run(
        PROJECT_ID, RUN_ID, branch="feature/new", database_enabled=False
    )
    expected_path = repository / ".worktrees" / identity.worktree_name
    target = expected_path.parent if swap_scope == "root" else expected_path
    outside = tmp_path / f"outside-{swap_scope}"
    runner = NamespaceSwapRunner(repository, target, outside)
    controlled = _controlled(repository, tmp_path / "state", runner)

    if os.name == "nt":
        handle = controlled.create_worktree(identity, base_sha)
        assert handle.path.is_dir()
        assert runner.swap_succeeded is False
    else:
        with pytest.raises(ControlledGitError):
            controlled.create_worktree(identity, base_sha)
        assert runner.swap_succeeded is True

    assert runner.swap_attempted is True
    if runner.swap_succeeded:
        assert runner.redirect_created is True
    outside_target = outside / identity.worktree_name if swap_scope == "root" else outside
    assert not (outside_target / ".git").exists()


@pytest.mark.skipif(
    os.name != "nt",
    reason="POSIX same-UID metadata renames are outside the advisory-lock boundary",
)
def test_create_binds_git_metadata_to_retained_repository_directory(tmp_path: Path) -> None:
    repository, base_sha = _source_repository(tmp_path)
    identity = WorktreeIdentity.for_run(
        PROJECT_ID, RUN_ID, branch="feature/new", database_enabled=False
    )
    metadata_root = repository / ".git" / "worktrees"
    outside = tmp_path / "outside-metadata"
    runner = NamespaceSwapRunner(repository, metadata_root, outside)
    controlled = _controlled(repository, tmp_path / "state", runner)

    handle = controlled.create_worktree(identity, base_sha)
    assert handle.path.is_dir()

    assert runner.swap_attempted is True
    assert runner.swap_succeeded is False
    assert runner.redirect_created is False
    assert not outside.exists() or not any(outside.iterdir())


def test_create_refuses_nonignored_root_without_creating_it(tmp_path: Path) -> None:
    repository, base_sha = _source_repository(tmp_path, ignored=False)
    identity = WorktreeIdentity.for_run(
        PROJECT_ID, RUN_ID, branch="feature/new", database_enabled=False
    )
    controlled = _controlled(repository, tmp_path / "state")

    with pytest.raises(ControlledGitError):
        controlled.create_worktree(identity, base_sha)
    assert not (repository / ".worktrees").exists()


@pytest.mark.parametrize("branch", ("main", "bad..branch", "bad?branch"))
def test_create_refuses_default_or_invalid_branch_before_target_creation(
    tmp_path: Path, branch: str
) -> None:
    repository, base_sha = _source_repository(tmp_path)
    identity = WorktreeIdentity.for_run(
        PROJECT_ID, RUN_ID, branch="feature/new", database_enabled=False
    )
    object.__setattr__(identity, "branch", branch)
    controlled = _controlled(repository, tmp_path / "state")

    with pytest.raises(ControlledGitError):
        controlled.create_worktree(identity, base_sha)
    assert not (repository / ".worktrees").exists()


def test_create_preflight_matches_registration_by_exact_target(tmp_path: Path) -> None:
    repository, base_sha = _source_repository(tmp_path)
    identity = WorktreeIdentity.for_run(
        PROJECT_ID, RUN_ID, branch="feature/new", database_enabled=False
    )
    metadata_root = repository / ".git" / "worktrees"
    metadata_root.mkdir()
    expected_target = repository / ".worktrees" / identity.worktree_name / ".git"
    _add_registration_candidate(metadata_root, "-", expected_target)
    runner = RecordingRunner(repository)
    controlled = _controlled(repository, tmp_path / "state", runner)

    with pytest.raises(ControlledGitError):
        controlled.create_worktree(identity, base_sha)

    assert not any("worktree" in argv and "add" in argv for argv, _cwd, _env in runner.calls)


@pytest.mark.parametrize("basename", ("-", "non-identity-registration"))
def test_remove_uses_exact_target_registration_lookup(tmp_path: Path, basename: str) -> None:
    repository, _identity, handle = _managed_repository(tmp_path)
    _rename_registration(handle.path, basename)
    controlled = ControlledGit(
        CanonicalRoot(repository),
        default_branch="main",
        state_root=tmp_path / "state",
        git_executable=TRUSTED_GIT,
    )

    controlled.remove_worktree(handle)

    assert not handle.path.exists()
    metadata_root = repository / ".git" / "worktrees"
    assert not metadata_root.exists() or not any(metadata_root.iterdir())


def test_create_refuses_existing_target_and_branch_collision(tmp_path: Path) -> None:
    repository, base_sha = _source_repository(tmp_path)
    identity = WorktreeIdentity.for_run(
        PROJECT_ID, RUN_ID, branch="feature/new", database_enabled=False
    )
    target = repository / ".worktrees" / identity.worktree_name
    target.parent.mkdir()
    target.mkdir()
    controlled = _controlled(repository, tmp_path / "state")
    with pytest.raises(ControlledGitError):
        controlled.create_worktree(identity, base_sha)
    assert target.is_dir()

    shutil.rmtree(target)
    _git(repository, "branch", identity.branch, base_sha)
    with pytest.raises(ControlledGitError):
        controlled.create_worktree(identity, base_sha)
    assert not target.exists()


def test_create_refuses_unsafe_local_filter_before_add(tmp_path: Path) -> None:
    repository, base_sha = _source_repository(tmp_path)
    identity = WorktreeIdentity.for_run(
        PROJECT_ID, RUN_ID, branch="feature/new", database_enabled=False
    )
    marker = tmp_path / "filter-marker"
    command = f"\"{sys.executable}\" -c \"open(r'{marker}', 'w').write('executed')\""
    _git(repository, "config", "filter.evil.clean", command)
    controlled = _controlled(repository, tmp_path / "state")

    with pytest.raises(ControlledGitError):
        controlled.create_worktree(identity, base_sha)
    assert not marker.exists()
    assert not (repository / ".worktrees" / identity.worktree_name).exists()


def test_remove_is_exact_quarantine_idempotent_and_keeps_branch(tmp_path: Path) -> None:
    repository, base_sha = _source_repository(tmp_path)
    identity = WorktreeIdentity.for_run(
        PROJECT_ID, RUN_ID, branch="feature/new", database_enabled=False
    )
    runner = RecordingRunner(repository)
    controlled = _controlled(repository, tmp_path / "state", runner)
    handle = controlled.create_worktree(identity, base_sha)
    _rename_registration(handle.path, "non-identity-registration")

    controlled.remove_worktree(handle)
    controlled.remove_worktree(handle)

    assert not handle.path.exists()
    assert (
        subprocess.run(
            [
                str(TRUSTED_GIT),
                "-C",
                str(repository),
                "show-ref",
                "--verify",
                "--quiet",
                f"refs/heads/{identity.branch}",
            ],
            check=False,
            capture_output=True,
            shell=False,
        ).returncode
        == 0
    )
    assert not any(
        "worktree" in argv and ("remove" in argv or "prune" in argv)
        for argv, _cwd, _environment in runner.calls
    )
    metadata_root = repository / ".git" / "worktrees"
    assert not metadata_root.exists() or not any(metadata_root.iterdir())
    registration_quarantine = repository / ".git" / ".forge-worktree-quarantine"
    assert not registration_quarantine.exists() or not any(registration_quarantine.iterdir())
    target_quarantine = repository / ".worktrees" / ".forge-quarantine"
    assert not target_quarantine.exists() or not any(target_quarantine.iterdir())


def test_remove_stale_registration_is_exact_and_preserves_unrelated_metadata(
    tmp_path: Path,
) -> None:
    repository, identity, handle = _managed_repository(tmp_path)
    metadata = _registration_for(handle.path)
    unrelated_target = repository / ".worktrees" / "unrelated-worktree" / ".git"
    unrelated = _add_registration_candidate(
        repository / ".git" / "worktrees", "unrelated-registration", unrelated_target
    )
    shutil.rmtree(handle.path)
    runner = RecordingRunner(repository)
    controlled = _controlled(repository, tmp_path / "state", runner)

    controlled.remove_worktree(handle)

    assert not metadata.exists()
    assert unrelated.is_dir()
    assert not any(
        "worktree" in argv and ("remove" in argv or "prune" in argv)
        for argv, _cwd, _environment in runner.calls
    )
    assert (
        subprocess.run(
            [
                str(TRUSTED_GIT),
                "-C",
                str(repository),
                "show-ref",
                "--verify",
                "--quiet",
                f"refs/heads/{identity.branch}",
            ],
            check=False,
            capture_output=True,
            shell=False,
        ).returncode
        == 0
    )


def test_remove_fully_absent_is_idempotent_without_git_or_metadata_mutation(
    tmp_path: Path,
) -> None:
    repository, _identity, handle = _managed_repository(tmp_path)
    metadata = _registration_for(handle.path)
    shutil.rmtree(handle.path)
    shutil.rmtree(metadata)
    runner = RecordingRunner(repository)
    controlled = _controlled(repository, tmp_path / "state", runner)

    controlled.remove_worktree(handle)
    controlled.remove_worktree(handle)

    assert not handle.path.exists()
    assert not metadata.exists()
    metadata_root = repository / ".git" / "worktrees"
    assert not metadata_root.exists() or not any(metadata_root.iterdir())
    registration_quarantine = repository / ".git" / ".forge-worktree-quarantine"
    assert not registration_quarantine.exists() or not any(registration_quarantine.iterdir())
    target_quarantine = repository / ".worktrees" / ".forge-quarantine"
    assert not target_quarantine.exists() or not any(target_quarantine.iterdir())
    assert not any(
        "worktree" in argv and ("remove" in argv or "prune" in argv)
        for argv, _cwd, _environment in runner.calls
    )


def test_remove_live_validation_uses_target_git_only_before_quarantine_bind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, _identity, handle = _managed_repository(tmp_path)
    runner = RecordingRunner(repository)
    runner.bound = False
    runner.target_git_after_bind = False
    target = handle.path.resolve()
    original_run = runner.run_argv

    def run_argv(
        argv: tuple[str, ...] | list[str],
        *,
        cwd: str,
        environment: dict[str, str],
        timeout_seconds: float | None = None,
    ) -> ProcessResult:
        if runner.bound and Path(cwd).resolve() == target:
            runner.target_git_after_bind = True
        return original_run(
            argv,
            cwd=cwd,
            environment=environment,
            timeout_seconds=timeout_seconds,
        )

    monkeypatch.setattr(runner, "run_argv", run_argv)
    controlled = _controlled(repository, tmp_path / "state", runner)
    original_bind = controlled._repository._bind_worktree_quarantine

    def bind(access: object) -> None:
        original_bind(access)  # type: ignore[arg-type]
        runner.bound = True

    monkeypatch.setattr(controlled._repository, "_bind_worktree_quarantine", bind)

    controlled.remove_worktree(handle)

    target_git_calls = [
        (argv, cwd) for argv, cwd, _environment in runner.calls if Path(cwd).resolve() == target
    ]
    assert target_git_calls
    assert runner.target_git_after_bind is False


def test_remove_live_rejects_registration_gitdir_substitution_before_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, identity, handle = _managed_repository(tmp_path)
    runner = RecordingRunner(repository)
    controlled = _controlled(repository, tmp_path / "state", runner)
    registration = _registration_for(handle.path)
    alternate = repository / ".worktrees" / "alternate-target"
    alternate.mkdir()
    (alternate / ".git").write_text("alternate\n", encoding="utf-8")
    branch_ref = f"refs/heads/{identity.branch}"

    original_bind = controlled._repository._bind_worktree_quarantine

    def substitute_before_bind(access: object) -> None:
        (registration / "gitdir").write_bytes(f"{alternate / '.git'}\n".encode())
        original_bind(access)  # type: ignore[arg-type]

    monkeypatch.setattr(controlled._repository, "_bind_worktree_quarantine", substitute_before_bind)

    with pytest.raises(ControlledGitError):
        controlled.remove_worktree(handle)

    assert handle.path.is_dir()
    assert registration.is_dir()
    assert (repository / ".git" / branch_ref).is_file()


def test_remove_refuses_missing_branch_before_any_quarantine_mutation(tmp_path: Path) -> None:
    repository, identity, handle = _managed_repository(tmp_path)
    registration = _registration_for(handle.path)
    _git(repository, "update-ref", "-d", f"refs/heads/{identity.branch}")
    controlled = _controlled(repository, tmp_path / "state")

    with pytest.raises(ControlledGitError):
        controlled.remove_worktree(handle)

    assert handle.path.is_dir()
    assert registration.is_dir()


def test_remove_refuses_unsafe_local_filter_without_running_or_mutating_target(
    tmp_path: Path,
) -> None:
    repository, _identity, handle = _managed_repository(tmp_path)
    marker = tmp_path / "filter-marker"
    command = f"\"{sys.executable}\" -c \"open(r'{marker}', 'w').write('executed')\""
    _git(repository, "config", "filter.evil.clean", command)
    registration = _registration_for(handle.path)
    controlled = _controlled(repository, tmp_path / "state")

    with pytest.raises(ControlledGitError):
        controlled.remove_worktree(handle)

    assert not marker.exists()
    assert handle.path.is_dir()
    assert registration.is_dir()


def test_remove_refuses_malformed_registration_without_mutation(tmp_path: Path) -> None:
    repository, _identity, handle = _managed_repository(tmp_path)
    registration = _registration_for(handle.path)
    (registration / "gitdir").write_bytes(b"/missing/target\n/second\n")
    controlled = _controlled(repository, tmp_path / "state")

    with pytest.raises(ControlledGitError):
        controlled.remove_worktree(handle)

    assert handle.path.is_dir()
    assert registration.is_dir()


def test_remove_refuses_duplicate_registration_proof_without_mutation(tmp_path: Path) -> None:
    repository, _identity, handle = _managed_repository(tmp_path)
    registration = _registration_for(handle.path)
    duplicate = registration.with_name("duplicate-registration")
    shutil.copytree(registration, duplicate)
    controlled = _controlled(repository, tmp_path / "state")

    with pytest.raises(ControlledGitError):
        controlled.remove_worktree(handle)

    assert handle.path.is_dir()
    assert registration.is_dir()
    assert duplicate.is_dir()


def test_remove_refuses_linked_registration_without_mutation(tmp_path: Path) -> None:
    repository, _identity, handle = _managed_repository(tmp_path)
    registration = _registration_for(handle.path)
    moved = registration.with_name("registration-target")
    registration.rename(moved)
    try:
        registration.symlink_to(moved, target_is_directory=True)
    except OSError, NotImplementedError:
        pytest.skip("symlinks are not available on this host")
    controlled = _controlled(repository, tmp_path / "state")

    with pytest.raises(ControlledGitError):
        controlled.remove_worktree(handle)

    assert handle.path.is_dir()
    assert moved.is_dir()


def test_remove_refuses_linked_target_without_mutation(tmp_path: Path) -> None:
    repository, _identity, handle = _managed_repository(tmp_path)
    outside = tmp_path / "outside-target"
    outside.mkdir()
    (outside / "outside-marker").write_text("untouched\n", encoding="utf-8")
    registration = _registration_for(handle.path)
    shutil.rmtree(handle.path)
    try:
        handle.path.symlink_to(outside, target_is_directory=True)
    except OSError, NotImplementedError:
        pytest.skip("symlinks are not available on this host")
    controlled = _controlled(repository, tmp_path / "state")

    with pytest.raises(ControlledGitError):
        controlled.remove_worktree(handle)

    assert handle.path.is_symlink()
    assert (outside / "outside-marker").read_text(encoding="utf-8") == "untouched\n"
    assert registration.is_dir()


@pytest.mark.parametrize("collision", ("target", "registration"))
def test_remove_refuses_exact_quarantine_collision_without_mutation(
    tmp_path: Path, collision: str
) -> None:
    repository, _identity, handle = _managed_repository(tmp_path)
    registration = _registration_for(handle.path)
    if collision == "target":
        destination = repository / ".worktrees" / ".forge-quarantine" / handle.path.name
    else:
        destination = repository / ".git" / ".forge-worktree-quarantine" / registration.name
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.mkdir()
    controlled = _controlled(repository, tmp_path / "state")

    with pytest.raises(ControlledGitError):
        controlled.remove_worktree(handle)

    assert handle.path.is_dir()
    assert registration.is_dir()
    assert destination.is_dir()


def test_remove_refuses_target_matched_registration_quarantine_evidence(
    tmp_path: Path,
) -> None:
    repository, _identity, handle = _managed_repository(tmp_path)
    registration = _registration_for(handle.path)
    evidence = repository / ".git" / ".forge-worktree-quarantine" / "retained-proof"
    evidence.mkdir(parents=True)
    (evidence / "gitdir").write_text(f"{handle.path / '.git'}\n", encoding="utf-8")
    controlled = _controlled(repository, tmp_path / "state")

    with pytest.raises(ControlledGitError):
        controlled.remove_worktree(handle)

    assert handle.path.is_dir()
    assert registration.is_dir()
    assert evidence.is_dir()


def test_remove_refuses_duplicate_registration_quarantine_proofs(tmp_path: Path) -> None:
    repository, _identity, handle = _managed_repository(tmp_path)
    registration = _registration_for(handle.path)
    quarantine_root = repository / ".git" / ".forge-worktree-quarantine"
    for name in ("retained-one", "retained-two"):
        evidence = quarantine_root / name
        evidence.mkdir(parents=True)
        (evidence / "gitdir").write_text(f"{handle.path / '.git'}\n", encoding="utf-8")
    controlled = _controlled(repository, tmp_path / "state")

    with pytest.raises(ControlledGitError):
        controlled.remove_worktree(handle)

    assert handle.path.is_dir()
    assert registration.is_dir()
    assert all((quarantine_root / name).is_dir() for name in ("retained-one", "retained-two"))


def test_remove_refuses_malformed_registration_quarantine_evidence(tmp_path: Path) -> None:
    repository, _identity, handle = _managed_repository(tmp_path)
    registration = _registration_for(handle.path)
    malformed = repository / ".git" / ".forge-worktree-quarantine" / "malformed-proof"
    malformed.mkdir(parents=True)
    (malformed / "unexpected").write_text("no gitdir proof\n", encoding="utf-8")
    controlled = _controlled(repository, tmp_path / "state")

    with pytest.raises(ControlledGitError):
        controlled.remove_worktree(handle)

    assert handle.path.is_dir()
    assert registration.is_dir()
    assert malformed.is_dir()


def test_remove_leaves_unrelated_well_formed_registration_quarantine_evidence(
    tmp_path: Path,
) -> None:
    repository, _identity, handle = _managed_repository(tmp_path)
    unrelated_target = repository / ".worktrees" / "foreign-worktree" / ".git"
    registration = _registration_for(handle.path)
    root = CanonicalRoot(repository)
    with root._prepare_worktree_quarantine(handle.path.name, registration.name):
        pass
    quarantine_root = repository / ".git" / ".forge-worktree-quarantine"
    evidence = _add_registration_candidate(
        quarantine_root,
        "foreign-proof",
        unrelated_target,
    )
    controlled = _controlled(repository, tmp_path / "state")

    controlled.remove_worktree(handle)

    assert not handle.path.exists()
    assert evidence.is_dir()
    assert (evidence / "gitdir").is_file()


def test_remove_refuses_linked_registration_quarantine_evidence(tmp_path: Path) -> None:
    repository, _identity, handle = _managed_repository(tmp_path)
    outside = tmp_path / "outside-proof"
    outside.mkdir()
    (outside / "gitdir").write_text(f"{tmp_path / 'foreign' / '.git'}\n", encoding="utf-8")
    evidence = repository / ".git" / ".forge-worktree-quarantine" / "linked-proof"
    try:
        evidence.symlink_to(outside, target_is_directory=True)
    except OSError, NotImplementedError:
        pytest.skip("symlinks are not available on this host")
    controlled = _controlled(repository, tmp_path / "state")

    with pytest.raises(ControlledGitError):
        controlled.remove_worktree(handle)

    assert handle.path.is_dir()
    assert evidence.is_symlink()


@pytest.mark.parametrize(
    "failure_stage",
    (
        "after-target-move",
        "mid-target-deletion",
        "after-target-deletion",
        "after-registration-move",
        "mid-registration-deletion",
        "final-verification",
    ),
)
def test_remove_failure_states_preserve_exact_evidence_and_retry_truthfully(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    repository, identity, handle = _managed_repository(tmp_path)
    registration = _registration_for(handle.path)
    target_quarantine = repository / ".worktrees" / ".forge-quarantine" / handle.path.name
    registration_quarantine = repository / ".git" / ".forge-worktree-quarantine" / registration.name
    controlled = _controlled(repository, tmp_path / "state")
    root = controlled._repository

    if failure_stage == "after-target-move":
        original = root._quarantine_target

        def fail_after_target_move(access: object) -> None:
            original(access)  # type: ignore[arg-type]
            raise RuntimeError("injected target move failure")

        monkeypatch.setattr(root, "_quarantine_target", fail_after_target_move)
    elif failure_stage == "mid-target-deletion":
        original = root._delete_target_quarantine

        def fail_mid_target_delete(access: object) -> None:
            target_quarantine.joinpath("injected-child").write_text("retained\n", encoding="utf-8")
            raise RuntimeError("injected target delete failure")

        monkeypatch.setattr(root, "_delete_target_quarantine", fail_mid_target_delete)
    elif failure_stage == "after-target-deletion":
        original = root._quarantine_registration

        def fail_before_registration_move(access: object) -> None:
            raise RuntimeError("injected registration move failure")

        monkeypatch.setattr(root, "_quarantine_registration", fail_before_registration_move)
    elif failure_stage == "after-registration-move":
        original = root._delete_registration_quarantine

        def fail_before_registration_delete(access: object) -> None:
            raise RuntimeError("injected registration delete failure")

        monkeypatch.setattr(
            root, "_delete_registration_quarantine", fail_before_registration_delete
        )
    elif failure_stage == "mid-registration-deletion":
        original = root._delete_registration_quarantine

        def fail_mid_registration_delete(access: object) -> None:
            registration_quarantine.joinpath("injected-child").write_text(
                "retained\n", encoding="utf-8"
            )
            raise RuntimeError("injected registration delete failure")

        monkeypatch.setattr(root, "_delete_registration_quarantine", fail_mid_registration_delete)
    else:
        original = controlled._verify_removal_absent

        def fail_final_verification(
            exact_identity: WorktreeIdentity,
            exact_path: Path,
            basename: str | None,
        ) -> None:
            original(exact_identity, exact_path, basename)
            raise RuntimeError("injected final verification failure")

        monkeypatch.setattr(controlled, "_verify_removal_absent", fail_final_verification)

    with pytest.raises(ControlledGitError):
        controlled.remove_worktree(handle)

    assert (
        subprocess.run(
            [
                str(TRUSTED_GIT),
                "-C",
                str(repository),
                "show-ref",
                "--verify",
                "--quiet",
                f"refs/heads/{identity.branch}",
            ],
            check=False,
            capture_output=True,
            shell=False,
        ).returncode
        == 0
    )
    if failure_stage in {"after-target-move", "mid-target-deletion"}:
        assert not handle.path.exists()
        assert target_quarantine.is_dir()
        assert registration.is_dir()
        assert not registration_quarantine.exists()
        with pytest.raises(ControlledGitError):
            controlled.remove_worktree(handle)
    elif failure_stage == "after-target-deletion":
        assert not handle.path.exists()
        assert not target_quarantine.exists()
        assert registration.is_dir()
        assert not registration_quarantine.exists()
        monkeypatch.setattr(root, "_quarantine_registration", original)
        controlled.remove_worktree(handle)
        assert not registration.exists()
    elif failure_stage in {"after-registration-move", "mid-registration-deletion"}:
        assert not handle.path.exists()
        assert not target_quarantine.exists()
        assert not registration.exists()
        assert registration_quarantine.is_dir()
        with pytest.raises(ControlledGitError):
            controlled.remove_worktree(handle)
    else:
        assert not handle.path.exists()
        assert not target_quarantine.exists()
        assert not registration.exists()
        assert not registration_quarantine.exists()
        monkeypatch.setattr(controlled, "_verify_removal_absent", original)
        controlled.remove_worktree(handle)


def test_remove_refuses_unregistered_or_wrong_branch_without_deletion(tmp_path: Path) -> None:
    repository, base_sha = _source_repository(tmp_path)
    identity = WorktreeIdentity.for_run(
        PROJECT_ID, RUN_ID, branch="feature/new", database_enabled=False
    )
    controlled = _controlled(repository, tmp_path / "state")
    target = repository / ".worktrees" / identity.worktree_name
    target.parent.mkdir()
    target.mkdir()
    forged = ManagedWorktree(identity=identity, path=target, base_sha=base_sha)
    with pytest.raises(ControlledGitError):
        controlled.remove_worktree(forged)
    assert target.is_dir()

    shutil.rmtree(target)
    handle = controlled.create_worktree(identity, base_sha)
    _git(handle.path, "switch", "-c", "other")
    with pytest.raises(ControlledGitError):
        controlled.remove_worktree(handle)
    assert handle.path.is_dir()


def test_remove_locked_worktree_fails_closed(tmp_path: Path) -> None:
    repository, base_sha = _source_repository(tmp_path)
    identity = WorktreeIdentity.for_run(
        PROJECT_ID, RUN_ID, branch="feature/new", database_enabled=False
    )
    controlled = _controlled(repository, tmp_path / "state")
    handle = controlled.create_worktree(identity, base_sha)
    _git(repository, "worktree", "lock", str(handle.path))

    with pytest.raises(ControlledGitError):
        controlled.remove_worktree(handle)
    assert handle.path.is_dir()


def test_prune_only_removes_stale_registration_and_not_live_worktree(tmp_path: Path) -> None:
    repository, base_sha = _source_repository(tmp_path)
    identity = WorktreeIdentity.for_run(
        PROJECT_ID, RUN_ID, branch="feature/new", database_enabled=False
    )
    runner = RecordingRunner(repository)
    controlled = _controlled(repository, tmp_path / "state", runner)
    handle = controlled.create_worktree(identity, base_sha)
    controlled.prune()
    assert handle.path.is_dir()

    shutil.rmtree(handle.path)
    controlled.prune()
    assert not handle.path.exists()
    assert not any((repository / ".git" / "worktrees").glob(identity.worktree_name))
    prune_calls = [argv for argv, _cwd, _environment in runner.calls if argv[-2:-1] == ("prune",)]
    assert prune_calls
    assert prune_calls[-1][-2:] == ("prune", "--expire=now")
