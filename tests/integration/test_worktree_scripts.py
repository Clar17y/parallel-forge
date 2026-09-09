"""Process contracts for the standalone worktree CLI and host wrappers."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[2]
pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


def _environment() -> dict[str, str]:
    environment = dict(os.environ)
    scripts = str(Path(sys.executable).parent)
    environment["PATH"] = scripts + os.pathsep + environment.get("PATH", "")
    return environment


@pytest.mark.integration
def test_host_wrapper_creates_and_removes_real_managed_worktree(
    tmp_path: Path,
    migrated_database_url: str,
) -> None:
    """Exercise the native wrapper, CLI, database policy and real Git together."""
    from forge.domain.policy import ProjectPolicy, RunnerMode
    from forge.persistence.database import create_engine, create_session_factory
    from forge.persistence.unit_of_work import PostgresUnitOfWork
    from forge.tools.worktree_manifest import WorktreeManifestStore

    repository = tmp_path / "repository with spaces"
    repository.mkdir()
    data_root = tmp_path / "isolated data"
    environment = _environment()
    environment.update(FORGE_DATABASE_URL=migrated_database_url, FORGE_DATA_ROOT=str(data_root))

    def invoke(argv: list[str]) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            argv,
            cwd=repository,
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return result

    invoke(["git", "init", "--initial-branch=main"])
    (repository / ".gitignore").write_text(".worktrees/\n", encoding="utf-8")
    invoke(["git", "add", ".gitignore"])
    invoke(
        [
            "git",
            "-c",
            "user.name=Forge Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-m",
            "fixture",
        ]
    )
    policy = ProjectPolicy(
        id=uuid4(),
        version=1,
        repository_path=str(repository.resolve()),
        github_repository="fixture/wrapper",
        default_branch="main",
        runner_mode=RunnerMode.TRUSTED_HOST,
        trusted_project=True,
    )

    async def register() -> None:
        engine = create_engine(migrated_database_url)
        document = policy.model_dump(mode="json")
        digest = hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest()
        try:
            async with PostgresUnitOfWork(create_session_factory(engine)) as work:
                await work.projects.create(
                    project_id=policy.id,
                    name="wrapper fixture",
                    canonical_path=policy.repository_path,
                    canonical_path_key=os.path.normcase(policy.repository_path),
                    github_repository=policy.github_repository,
                    default_branch="main",
                    policy_digest=digest,
                    policy_document=document,
                )
                await work.commit()
        finally:
            await engine.dispose()

    asyncio.run(register())
    if os.name == "nt":
        shell = shutil.which("pwsh") or shutil.which("powershell")
        assert shell is not None
        prefix, suffix = [shell, "-NoProfile", "-File"], "ps1"
    else:
        prefix, suffix = ["bash"], "sh"
    branch = "forge/wrapper-smoke"
    invoke(
        [
            *prefix,
            str(ROOT / "scripts" / f"setup-worktree.{suffix}"),
            "--branch",
            branch,
            "--no-bootstrap",
        ]
    )
    manifest = WorktreeManifestStore(data_root).load(policy.id, branch)
    worktree = Path(manifest.worktree_path)
    assert worktree.is_dir()
    assert manifest.database_state.value == "DISABLED"
    assert branch in invoke(["git", "worktree", "list", "--porcelain"]).stdout
    invoke(
        [
            *prefix,
            str(ROOT / "scripts" / f"teardown-worktree.{suffix}"),
            "--branch",
            branch,
            "--yes",
        ]
    )
    assert not worktree.exists()
    invoke(["git", "show-ref", "--verify", f"refs/heads/{branch}"])


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
