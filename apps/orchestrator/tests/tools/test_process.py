from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from forge.application.ports.repository import ProcessExecutionError
from forge.tools import paths as paths_module
from forge.tools.paths import CanonicalRoot, RepositoryAccessDenied
from forge.tools.process import ProcessRunner


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "repository"
    root.mkdir()
    (root / "work").mkdir()
    return root


def _runner(root: Path, **kwargs: object) -> ProcessRunner:
    return ProcessRunner(CanonicalRoot(root), **kwargs)


def _python(code: str, *arguments: str) -> tuple[str, ...]:
    return (sys.executable, "-c", code, *arguments)


def _make_directory_link(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except OSError, NotImplementedError:
        pass
    if os.name == "nt":
        result = subprocess.run(
            ["cmd.exe", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            check=False,
            shell=False,
        )
        if result.returncode == 0 and link.is_dir():
            return
    pytest.skip("the current host cannot create a directory link or junction")


def _make_managed_worktree_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    git = shutil.which("git") or "git"
    root = tmp_path / "repository"
    root.mkdir()
    for arguments in (
        ("init", "-b", "main"),
        ("config", "user.name", "Forge Test"),
        ("config", "user.email", "forge@example.test"),
    ):
        result = subprocess.run(
            [git, "-C", str(root), *arguments],
            capture_output=True,
            check=False,
            shell=False,
        )
        assert result.returncode == 0, result.stderr.decode(errors="replace")
    (root / "README.md").write_text("forge\n", encoding="utf-8")
    for arguments in (("add", "README.md"), ("commit", "-m", "initial")):
        result = subprocess.run(
            [git, "-C", str(root), *arguments],
            capture_output=True,
            check=False,
            shell=False,
        )
        assert result.returncode == 0, result.stderr.decode(errors="replace")
    target = root / ".worktrees" / "target"
    target.parent.mkdir()
    result = subprocess.run(
        [git, "-C", str(root), "worktree", "add", "-b", "feature", str(target), "HEAD"],
        capture_output=True,
        check=False,
        shell=False,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    registration = next((root / ".git" / "worktrees").iterdir())
    opaque = registration.with_name("opaque-registration")
    marker = target / ".git"
    marker.chmod(0o600)
    marker.unlink()
    marker.write_bytes(f"gitdir: {opaque}\n".encode())
    registration.rename(opaque)
    return root, target, opaque


def test_run_rejects_empty_nontext_or_nul_argv(tmp_path: Path) -> None:
    root = _root(tmp_path)
    runner = _runner(root)

    for argv in ((), ("",), (sys.executable, "\x00"), (sys.executable, 7)):  # type: ignore[tuple-item]
        with pytest.raises(ProcessExecutionError):
            runner.run_argv(argv, cwd=".", environment={})  # type: ignore[arg-type]


def test_run_does_not_interpret_metacharacters_as_shell_syntax(tmp_path: Path) -> None:
    root = _root(tmp_path)
    result = _runner(root).run_argv(
        _python("import sys; print(sys.argv[1])", "literal & echo injected"),
        cwd=".",
        environment={},
    )

    assert result.return_code == 0
    assert result.stdout.splitlines() == ["literal & echo injected"]


def test_run_uses_exact_contained_cwd_and_explicit_environment(tmp_path: Path) -> None:
    root = _root(tmp_path)
    result = _runner(root).run_argv(
        _python(
            "import os; print(os.getcwd()); print(os.environ.get('FORGE_VISIBLE')); print(os.environ.get('FORGE_PARENT'))"
        ),
        cwd="work",
        environment={"FORGE_VISIBLE": "yes"},
    )

    assert result.return_code == 0
    lines = result.stdout.splitlines()
    assert lines[0] == str((root / "work").resolve())
    assert lines[1:] == ["yes", "None"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor launch")
def test_posix_launch_inherits_the_retained_target_descriptor(tmp_path: Path) -> None:
    root, _target, _registration = _make_managed_worktree_fixture(tmp_path)
    canonical = CanonicalRoot(root)
    with canonical._create_directory(".", "created") as access:
        retained_fd = f"/proc/self/fd/{access.capability}"
        result = ProcessRunner(canonical).run_argv(
            _python("import os, sys; print(os.path.exists(sys.argv[1]))", retained_fd),
            cwd=str(access.path),
            environment={},
        )

    assert result.return_code == 0
    assert result.stdout.strip() == "True"


def test_run_bounds_stdout_and_stderr_independently_with_truthful_counts(tmp_path: Path) -> None:
    root = _root(tmp_path)
    result = _runner(root, stdout_max_bytes=64, stderr_max_bytes=32).run_argv(
        _python("import os; os.write(1, b'O' * 5000); os.write(2, b'E' * 4000)"),
        cwd=".",
        environment={},
    )

    assert result.return_code == 0
    assert len(result.stdout.encode()) == 64
    assert len(result.stderr.encode()) == 32
    assert result.stdout_original_byte_count == 5000
    assert result.stderr_original_byte_count == 4000
    assert result.stdout_truncated is True
    assert result.stderr_truncated is True


def test_run_decodes_invalid_utf8_with_replacement(tmp_path: Path) -> None:
    result = _runner(_root(tmp_path)).run_argv(
        _python("import os; os.write(1, b'good\\xff')"),
        cwd=".",
        environment={},
    )

    assert result.return_code == 0
    assert result.stdout == "good\ufffd"
    assert result.stdout_original_byte_count == 5


def test_run_timeout_kills_and_reaps_child(tmp_path: Path) -> None:
    started = time.monotonic()
    result = _runner(_root(tmp_path)).run_argv(
        _python("import time; print('started', flush=True); time.sleep(10)"),
        cwd=".",
        environment={},
        timeout_seconds=0.2,
    )

    assert time.monotonic() - started < 3
    assert result.timed_out is True
    assert result.return_code is not None
    assert result.stdout.splitlines() == ["started"]


def test_run_returns_bounded_nonzero_result(tmp_path: Path) -> None:
    result = _runner(_root(tmp_path)).run_argv(
        _python("import sys; sys.stderr.write('failure'); sys.exit(7)"),
        cwd=".",
        environment={},
    )

    assert result.return_code == 7
    assert result.timed_out is False
    assert result.stderr == "failure"


def test_run_rejects_spawn_failure_without_echoing_inputs(tmp_path: Path) -> None:
    root = _root(tmp_path)
    secret = "private-secret-value"
    with pytest.raises(ProcessExecutionError) as error:
        _runner(root).run_argv(
            (str(root / "does-not-exist"), secret),
            cwd=".",
            environment={"FORGE_SECRET": secret},
        )

    assert str(error.value) == "repository process execution failed"
    assert secret not in str(error.value)


@pytest.mark.parametrize("cwd", ("../outside", "/outside", "C:/outside", "//server/share"))
def test_run_rejects_uncontained_cwd(tmp_path: Path, cwd: str) -> None:
    root = _root(tmp_path)

    with pytest.raises((RepositoryAccessDenied, ProcessExecutionError)):
        _runner(root).run_argv(_python("print('unexpected')"), cwd=cwd, environment={})


def test_run_rejects_linked_cwd(tmp_path: Path) -> None:
    root = _root(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    _make_directory_link(root / "linked", outside)

    with pytest.raises(RepositoryAccessDenied):
        _runner(root).run_argv(_python("print('unexpected')"), cwd="linked", environment={})


@pytest.mark.parametrize("proof", ("marker", "registration"))
def test_run_fails_closed_when_managed_worktree_proof_changes_before_launch(
    tmp_path: Path, proof: str
) -> None:
    root_path, target, registration = _make_managed_worktree_fixture(tmp_path)
    root = CanonicalRoot(root_path)
    runner = ProcessRunner(root)

    with root._open_managed_worktree(target.name, registration.name):
        if proof == "marker":
            proof_path = target / ".git"
            replacement = b"gitdir: /foreign/registration\n"
        else:
            proof_path = registration / "gitdir"
            replacement = f"{target / 'foreign.git'}\n".encode()
        if os.name == "nt":
            with pytest.raises(OSError):
                proof_path.write_bytes(replacement)
            return
        original = proof_path.read_bytes()
        proof_path.write_bytes(replacement)
        try:
            with pytest.raises(RepositoryAccessDenied):
                runner.run_argv(_python("print('unexpected')"), cwd=str(target), environment={})
        finally:
            proof_path.write_bytes(original)


def test_directory_capability_rejects_forgery_mutation_and_reuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root_path = _root(tmp_path)
    (root_path / ".git" / "worktrees").mkdir(parents=True)
    root = CanonicalRoot(root_path)
    outside = tmp_path / "outside"
    outside.mkdir()

    with pytest.raises(TypeError):
        paths_module._DirectoryAccess(
            seal=object(),
            path=outside,
            capability=0,
            root_path=root.path,
            root_identity=root.identity,
            identity=root.identity,
            normalized="work",
            owner=object(),
        )

    with root._create_directory(".", "created") as access:
        result = ProcessRunner(root).run_argv(
            _python("import os; print(os.getcwd())"),
            cwd=str(access.path),
            environment={},
        )
        assert result.return_code == 0
        assert Path(result.stdout.strip()).resolve() == access.path.resolve()

        foreign_root = CanonicalRoot(root_path)
        with pytest.raises(RepositoryAccessDenied):
            foreign_root._launch_path_for_access("created", access, require_fd=True)
        with pytest.raises(AttributeError):
            access.path = outside  # type: ignore[misc]
        with pytest.raises(AttributeError):
            access._live = False  # type: ignore[misc]

        if os.name == "nt":
            api = root._windows
            assert api is not None
            original_identity = api.identity

            def substituted_identity(handle: int) -> tuple[int, ...]:
                if handle == access.capability:
                    return (0, 0, 0)
                return tuple(original_identity(handle))

            monkeypatch.setattr(api, "identity", substituted_identity)
        else:
            outside_descriptor = os.open(outside, os.O_RDONLY)
            os.dup2(outside_descriptor, access.capability)
            os.close(outside_descriptor)
        with pytest.raises(RepositoryAccessDenied):
            root._verify_directory_access("created", access)

    with pytest.raises(RepositoryAccessDenied):
        root._launch_path_for_access("created", access, require_fd=True)


def test_run_rejects_root_replacement(tmp_path: Path) -> None:
    root = _root(tmp_path)
    runner = _runner(root)
    moved = tmp_path / "moved"
    root.rename(moved)
    root.mkdir()

    with pytest.raises(RepositoryAccessDenied):
        runner.run_argv(_python("print('unexpected')"), cwd=".", environment={})


@pytest.mark.parametrize(
    "kwargs",
    (
        {"stdout_max_bytes": 0},
        {"stderr_max_bytes": 0},
        {"timeout_seconds": 0},
        {"stdout_max_bytes": -1},
        {"stderr_max_bytes": -1},
        {"timeout_seconds": -1},
    ),
)
def test_run_rejects_nonpositive_bounds(tmp_path: Path, kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        _runner(_root(tmp_path), **kwargs)
