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
from forge.artifacts.filesystem import FilesystemArtifactStore
from forge.domain.policy import CommandSpec, ProjectPolicy, RunnerMode, StepKind
from forge.tools.docker import DockerRunner
from forge.tools.paths import CanonicalRoot

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
