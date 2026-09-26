"""A1 repair/restart through the real Docker runner with scripted providers."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import test_subscription_counter_acceptance as counter
from forge.domain.policy import RunnerMode
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401


@pytest.fixture(scope="module")
def counter_runner_image():
    docker = shutil.which("docker")
    if docker is None:
        if os.environ.get("CI"):
            pytest.fail("Docker CLI is required in CI for counter acceptance")
        pytest.skip("Docker CLI is unavailable outside CI")
    probe = subprocess.run([docker, "info"], capture_output=True, timeout=15, check=False)
    if probe.returncode:
        if os.environ.get("CI"):
            pytest.fail("Docker daemon is required in CI for counter acceptance")
        pytest.skip("Docker daemon is unavailable outside CI")
    root = Path(__file__).resolve().parents[4]
    built = subprocess.run(
        [docker, "build", "--platform", "linux/amd64", "-f", "Dockerfile.runner", "-q", "."],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert built.returncode == 0, built.stderr
    return built.stdout.strip()


@pytest.mark.integration
@pytest.mark.docker
async def test_a1_docker_counter_repair_and_restart(
    counter_runner_image, session_factory, tmp_path
):
    case = await counter.prepared_counter_case(
        session_factory,
        tmp_path,
        runner_mode=RunnerMode.DOCKER,
        runner_image=counter_runner_image,
    )
    manifest = await counter.verify_counter_repair(case, session_factory, tmp_path)
    results = assert_docker_execution(manifest, counter_runner_image)
    assert len(results) == 2
    assert sorted(result["exit_code"] for result in results) == [0, 1]
    assert all(result["command_name"] == "unit" for result in results)


def assert_docker_execution(manifest, runner_image):
    """Check policy and actual command evidence for any Docker counter variant."""
    evidence = json.loads(manifest.read_text(encoding="utf-8"))["evidence"]
    assert evidence["runner"] == {"mode": "docker", "trusted_project": False}
    assert "trusted-host runner" not in evidence["limitations"]
    results = [
        json.loads((manifest.parent / digest).read_text(encoding="utf-8"))
        for digest, artifact in evidence["artifacts"].items()
        if artifact["media_type"] == "application/vnd.forge.command-result+json"
    ]
    for result in results:
        assert result["runner_mode"] == "docker"
        assert result["image_digest"] == runner_image
        assert result["unsandboxed"] is False
        assert result["network_enabled"] is False
        assert result["timed_out"] is False
        assert result["duration_ms"] >= 0
        assert result["stdout_truncated"] is result["stderr_truncated"] is False
    return results
