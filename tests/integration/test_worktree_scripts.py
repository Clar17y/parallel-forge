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
