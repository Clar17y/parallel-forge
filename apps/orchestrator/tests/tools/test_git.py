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
from forge.application.ports.worktrees import ManagedWorktree
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
        self._delegate = ProcessRunner(CanonicalRoot(repository))
        self.calls: list[tuple[tuple[str, ...], str, dict[str, str]]] = []

    def run_argv(
        self,
        argv: tuple[str, ...] | list[str],
        *,
        cwd: str,
        environment: dict[str, str],
        timeout_seconds: float | None = None,
    ) -> ProcessResult:
        command = tuple(argv)
        self.calls.append((command, cwd, dict(environment)))
        return self._delegate.run_argv(
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
    return ControlledGit(
        CanonicalRoot(repository),
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
        str(expected_path),
        base_sha,
    )


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


def test_remove_is_exact_force_idempotent_and_keeps_branch(tmp_path: Path) -> None:
    repository, base_sha = _source_repository(tmp_path)
    identity = WorktreeIdentity.for_run(
        PROJECT_ID, RUN_ID, branch="feature/new", database_enabled=False
    )
    runner = RecordingRunner(repository)
    controlled = _controlled(repository, tmp_path / "state", runner)
    handle = controlled.create_worktree(identity, base_sha)

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
    remove_calls = [argv for argv, _cwd, _environment in runner.calls if argv[-4:-3] == ("remove",)]
    assert remove_calls
    assert remove_calls[0][-4:] == ("remove", "--force", "--", str(handle.path))


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
