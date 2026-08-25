"""Process contracts for the standalone worktree CLI and host wrappers."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _environment() -> dict[str, str]:
    environment = dict(os.environ)
    scripts = str(Path(sys.executable).parent)
    environment["PATH"] = scripts + os.pathsep + environment.get("PATH", "")
    return environment


def test_python_cli_exposes_worktree_commands() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "forge.cli.main", "worktree", "--help"],
        cwd=ROOT,
        env=_environment(),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0
    assert "setup" in result.stdout
    assert "teardown" in result.stdout


def test_declining_teardown_is_success_without_configuration_access() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "forge.cli.main",
            "worktree",
            "teardown",
            "--branch",
            "feature/decline",
        ],
        cwd=ROOT,
        env=_environment(),
        input="n\n",
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0
    assert "no resources were changed" in result.stdout
    assert result.stderr == ""


@pytest.mark.skipif(os.name != "nt", reason="PowerShell host wrapper")
@pytest.mark.parametrize("script", ("setup-worktree.ps1", "teardown-worktree.ps1"))
def test_powershell_wrapper_forwards_help_and_exit_code(script: str) -> None:
    powershell = next(
        (
            candidate
            for candidate in ("pwsh.exe", "pwsh", "powershell.exe", "powershell")
            if __import__("shutil").which(candidate)
        ),
        None,
    )
    if powershell is None:
        pytest.skip("PowerShell unavailable")
    result = subprocess.run(
        [powershell, "-NoProfile", "-File", str(ROOT / "scripts" / script), "--help"],
        cwd=ROOT,
        env=_environment(),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0
    assert "--branch" in result.stdout


def test_bash_wrappers_are_thin_and_strict() -> None:
    for script in ("setup-worktree.sh", "teardown-worktree.sh"):
        contents = (ROOT / "scripts" / script).read_text(encoding="utf-8")
        assert "set -euo pipefail" in contents
        assert "exec python -m forge.cli.main worktree" in contents
        assert '"$@"' in contents
        assert "DATABASE_URL" not in contents


def test_yes_failure_is_nonzero_redacted_and_secret_free() -> None:
    environment = _environment()
    sentinel = "SENTINEL_ADMIN_SECRET_DO_NOT_PRINT"
    environment["FORGE_DATABASE_URL"] = sentinel
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "forge.cli.main",
            "worktree",
            "teardown",
            "--branch",
            "feature/failure",
            "--yes",
        ],
        cwd=ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode != 0
    assert result.stderr.strip() == "Forge worktree operation failed."
    assert sentinel not in result.stdout + result.stderr


@pytest.mark.skipif(os.name != "nt", reason="PowerShell host wrapper")
@pytest.mark.parametrize(
    ("script", "subcommand", "extra"),
    (
        ("setup-worktree.ps1", "setup", "--no-bootstrap"),
        ("teardown-worktree.ps1", "teardown", "--yes"),
    ),
)
def test_powershell_wrapper_forwards_lifecycle_arguments_and_failure_exit(
    tmp_path: Path,
    script: str,
    subcommand: str,
    extra: str,
) -> None:
    powershell = next(
        (
            candidate
            for candidate in ("pwsh.exe", "pwsh", "powershell.exe", "powershell")
            if __import__("shutil").which(candidate)
        ),
        None,
    )
    if powershell is None:
        pytest.skip("PowerShell unavailable")
    fake_bin = tmp_path / "fake python with spaces"
    fake_bin.mkdir()
    capture = tmp_path / "captured arguments.txt"
    (fake_bin / "python.cmd").write_text(
        '@echo off\r\n> "%FORGE_CAPTURE%" echo %*\r\nexit /b %FORGE_FAKE_EXIT%\r\n',
        encoding="utf-8",
    )
    environment = dict(os.environ)
    environment["PATH"] = str(fake_bin) + os.pathsep + environment.get("PATH", "")
    environment["FORGE_CAPTURE"] = str(capture)
    environment["FORGE_FAKE_EXIT"] = "23"
    spaced_cwd = tmp_path / "repository with spaces"
    spaced_cwd.mkdir()

    result = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-File",
            str(ROOT / "scripts" / script),
            "--branch",
            "feature/space value",
            extra,
        ],
        cwd=spaced_cwd,
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 23
    forwarded = capture.read_text(encoding="utf-8").strip()
    assert forwarded.startswith(f"-m forge.cli.main worktree {subcommand} --branch ")
    assert '"feature/space value"' in forwarded
    assert forwarded.endswith(extra)


@pytest.mark.skipif(os.name == "nt", reason="Bash host wrapper")
@pytest.mark.parametrize(
    ("script", "subcommand", "extra"),
    (
        ("setup-worktree.sh", "setup", "--no-bootstrap"),
        ("teardown-worktree.sh", "teardown", "--yes"),
    ),
)
def test_bash_wrapper_forwards_lifecycle_arguments_and_failure_exit(
    tmp_path: Path,
    script: str,
    subcommand: str,
    extra: str,
) -> None:
    fake_bin = tmp_path / "fake python with spaces"
    fake_bin.mkdir()
    capture = tmp_path / "captured arguments.txt"
    fake_python = fake_bin / "python"
    fake_python.write_text(
        '#!/usr/bin/env bash\nprintf \'%s\\n\' "$@" > "$FORGE_CAPTURE"\nexit "$FORGE_FAKE_EXIT"\n',
        encoding="utf-8",
    )
    fake_python.chmod(0o700)
    environment = dict(os.environ)
    environment["PATH"] = str(fake_bin) + os.pathsep + environment.get("PATH", "")
    environment["FORGE_CAPTURE"] = str(capture)
    environment["FORGE_FAKE_EXIT"] = "23"
    spaced_cwd = tmp_path / "repository with spaces"
    spaced_cwd.mkdir()

    result = subprocess.run(
        [
            "bash",
            str(ROOT / "scripts" / script),
            "--branch",
            "feature/space value",
            extra,
        ],
        cwd=spaced_cwd,
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 23
    assert capture.read_text(encoding="utf-8").splitlines() == [
        "-m",
        "forge.cli.main",
        "worktree",
        subcommand,
        "--branch",
        "feature/space value",
        extra,
    ]
