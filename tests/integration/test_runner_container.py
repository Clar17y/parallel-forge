from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest
from forge.application.ports.runner import RunCommandRequest
from forge.application.ports.worktrees import DatabaseBinding
from forge.artifacts.filesystem import FilesystemArtifactStore
from forge.domain.policy import CommandSpec, ProjectPolicy, RunnerMode, StepKind
from forge.domain.resource import ResourceState, WorktreeIdentity
from forge.tools.docker import DockerRunner
from forge.tools.environment import EnvironmentStager
from forge.tools.git import ControlledGit
from forge.tools.paths import CanonicalRoot
from forge.tools.worktree_runner import WorktreeRunnerFactory

pytestmark = pytest.mark.docker


def _docker_or_skip() -> str:
    docker = shutil.which("docker")
    if docker is None:
        if os.environ.get("CI"):
            pytest.fail("Docker CLI is required in CI for the runner smoke")
        pytest.skip("Docker CLI is unavailable outside CI")
    probe = subprocess.run(
        [docker, "info"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=15,
    )
    if probe.returncode != 0:
        if os.environ.get("CI"):
            pytest.fail("Docker daemon is required in CI for the runner smoke")
        pytest.skip("Docker daemon is unavailable outside CI")
    return docker


def test_runner_image_is_pinned_nonroot_and_has_required_tool_versions(tmp_path: Path) -> None:
    docker = _docker_or_skip()
    repository = Path(__file__).resolve().parents[2]
    image_id = subprocess.check_output(
        [
            docker,
            "build",
            "--platform",
            "linux/amd64",
            "-f",
            "Dockerfile.runner",
            "-q",
            str(repository),
        ],
        text=True,
        timeout=600,
    ).strip()
    assert image_id.startswith("sha256:")
    smoke_command = " ".join(  # noqa: FLY002 - keep the shell probe readable and bounded
        (
            "python --version && node --version && npm --version && git --version",
            "&& rg --version | head -1 && cc --version | head -1 && id -u",
            "&& ! command -v docker",
        )
    )
    output = subprocess.check_output(
        [
            docker,
            "run",
            "--rm",
            "--network=none",
            image_id,
            "sh",
            "-c",
            smoke_command,
        ],
        text=True,
        timeout=30,
    )
    lines = output.splitlines()
    assert lines[0].startswith("Python 3.14")
    assert lines[1].startswith("v24.")
    assert lines[2]
    assert lines[3].startswith("git version")
    assert lines[4].startswith("ripgrep ")
    assert lines[5]
    assert lines[6] == "10001"

    worktree = tmp_path / "repo"
    worktree.mkdir()
    command = CommandSpec(
        kind=StepKind.TEST,
        name="python-version",
        argv=("python", "--version"),
        timeout_seconds=30,
    )
    policy = ProjectPolicy(
        id=uuid4(),
        version=1,
        repository_path=str(repository),
        github_repository="local/runner-smoke",
        default_branch="main",
        runner_mode=RunnerMode.DOCKER,
        commands=(command,),
    )
    artifact_store = FilesystemArtifactStore(tmp_path / "artifacts")
    runner = DockerRunner(
        policy=policy,
        root=CanonicalRoot(worktree),
        image_digest=image_id,
        artifact_store=artifact_store,
    )
    result = asyncio.run(
        runner.run(
            RunCommandRequest(command_name=command.name, kind=command.kind),
        )
    )
    stdout = json.loads(asyncio.run(artifact_store.open_bytes(result.stdout_digest)))
    assert result.exit_code == 0
    assert result.runner_mode is RunnerMode.DOCKER
    assert result.unsandboxed is False
    assert result.image_digest == image_id
    assert stdout["text"].startswith("Python 3.14")


def test_bound_runner_reads_e2a_staged_file_as_fixed_container_user(tmp_path: Path) -> None:
    docker = _docker_or_skip()
    project_root = Path(__file__).resolve().parents[2]
    repository = tmp_path / "repository"
    repository.mkdir()
    for arguments in (
        ("init", "-b", "main"),
        ("config", "user.name", "Forge Test"),
        ("config", "user.email", "forge@example.test"),
    ):
        result = subprocess.run(
            [(shutil.which("git") or "git"), "-C", str(repository), *arguments],
            capture_output=True,
            check=False,
            shell=False,
            text=True,
        )
        assert result.returncode == 0, result.stderr
    (repository / "README.md").write_text("forge\n", encoding="utf-8")
    (repository / ".gitignore").write_text(".worktrees/\nconfig/*.env\n", encoding="utf-8")
    (repository / "config").mkdir()
    (repository / "config" / ".keep").write_text("\n", encoding="utf-8")
    (repository / "tests").mkdir()
    (repository / "tests" / "e2b_reader.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "assert Path.cwd() == Path('/workspace')\n"
        "assert os.getuid() == 10001\n"
        "assert Path('/workspace/.git').is_file()\n"
        "assert Path('config/local.env').read_text(encoding='utf-8').strip() == 'FORGE_SECRET=secret-value'\n"
        "print('FORGE_E2B_OK')\n",
        encoding="utf-8",
    )
    git = shutil.which("git") or "git"
    for arguments in (("add", "."), ("commit", "-m", "initial")):
        result = subprocess.run(
            [git, "-C", str(repository), *arguments],
            capture_output=True,
            check=False,
            shell=False,
            text=True,
        )
        assert result.returncode == 0, result.stderr
    base_sha = subprocess.check_output(
        [git, "-C", str(repository), "rev-parse", "HEAD"], text=True
    ).strip()

    project_id = uuid4()
    run_id = uuid4()
    identity = WorktreeIdentity.for_run(project_id, run_id, "feature/e2b-smoke", False)
    root = CanonicalRoot(repository)
    controlled = ControlledGit(
        root,
        default_branch="main",
        state_root=tmp_path / "forge-state",
        git_executable=git,
    )
    worktree = controlled.create_worktree(identity, base_sha)
    (repository / "config" / "local.env").write_text(
        "FORGE_SECRET=secret-value\n", encoding="utf-8"
    )
    policy = ProjectPolicy(
        id=project_id,
        version=1,
        repository_path=str(repository.resolve()),
        github_repository="local/e2b-smoke",
        default_branch="main",
        runner_mode=RunnerMode.DOCKER,
        allowed_environment_files=("config/local.env",),
        commands=(
            CommandSpec(
                kind=StepKind.TEST,
                name="e2b-reader",
                argv=("python", "tests/e2b_reader.py"),
                timeout_seconds=60,
            ),
        ),
    )
    stager = EnvironmentStager(controlled)
    plan = stager.build_plan(
        worktree,
        policy,
        DatabaseBinding(state=ResourceState.DISABLED),
    )
    stager.publish(worktree, policy, plan)

    image_id = subprocess.check_output(
        [
            docker,
            "build",
            "--platform",
            "linux/amd64",
            "-f",
            str(project_root / "Dockerfile.runner"),
            "-q",
            str(project_root),
        ],
        text=True,
        timeout=600,
    ).strip()
    artifacts = FilesystemArtifactStore(tmp_path / "artifacts")
    runner = WorktreeRunnerFactory(
        controlled,
        image_digest=image_id,
        artifact_store=artifacts,
    ).create(worktree, policy)
    terminal = asyncio.run(
        runner.run_terminal(RunCommandRequest(command_name="e2b-reader", kind=StepKind.TEST))
    )
    stdout_bytes = asyncio.run(artifacts.open_bytes(terminal.result.stdout_digest))
    stderr_bytes = asyncio.run(artifacts.open_bytes(terminal.result.stderr_digest))
    stdout = json.loads(stdout_bytes)
    stderr = json.loads(stderr_bytes)
    assert terminal.result.exit_code == 0
    assert terminal.result.runner_mode is RunnerMode.DOCKER
    assert terminal.result.unsandboxed is False
    assert stdout["text"].strip() == "FORGE_E2B_OK"
    assert stderr["text"] == ""
    assert "secret-value" not in repr(terminal)
    assert b"secret-value" not in stdout_bytes
