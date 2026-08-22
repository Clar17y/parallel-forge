from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from forge.application.ports.repository import ProcessExecutionError
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
