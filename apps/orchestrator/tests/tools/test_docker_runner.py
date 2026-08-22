from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest
from forge.application.ports.runner import RunCommandRequest
from forge.domain.artifact import ArtifactDescriptor
from forge.domain.policy import CommandSpec, ProjectPolicy, RunnerMode, StepKind
from forge.domain.validation import UnknownNamedCommand
from forge.tools.docker import DockerRunner, RunnerExecutionError
from forge.tools.paths import CanonicalRoot


def _policy(command: CommandSpec) -> ProjectPolicy:
    return ProjectPolicy(
        id=uuid4(),
        version=3,
        repository_path="D:/Code/Parallel",
        github_repository="Clar17y/Parallel",
        default_branch="main",
        runner_mode=RunnerMode.DOCKER,
        commands=(command,),
    )


def _command(**overrides: object) -> CommandSpec:
    values: dict[str, object] = {
        "kind": StepKind.TEST,
        "name": "named-test",
        "argv": ("python", "--version"),
        "timeout_seconds": 30,
        "environment_keys": ("DATABASE_URL", "FORGE_INPUT"),
    }
    values.update(overrides)
    return CommandSpec(**values)


@dataclass
class _ProcessResult:
    return_code: int | None = 0
    stdout: str = "ok\n"
    stderr: str = ""
    timed_out: bool = False
    stdout_original_byte_count: int = 3
    stderr_original_byte_count: int = 0
    stdout_truncated: bool = False
    stderr_truncated: bool = False


class _FakeProcess:
    def __init__(
        self, result: _ProcessResult | None = None, error: BaseException | None = None
    ) -> None:
        self.calls: list[tuple[tuple[str, ...], dict[str, object]]] = []
        self.result = result or _ProcessResult()
        self.error = error

    def run_argv(self, argv: tuple[str, ...], **kwargs: object) -> _ProcessResult:
        self.calls.append((argv, kwargs))
        if self.error is not None:
            error, self.error = self.error, None
            raise error
        if argv[:3] == ("docker", "rm", "-f"):
            return _ProcessResult(stdout="", stderr="", stdout_original_byte_count=0)
        return self.result


class _CleanupFailureProcess(_FakeProcess):
    def run_argv(self, argv: tuple[str, ...], **kwargs: object) -> _ProcessResult:
        self.calls.append((argv, kwargs))
        if argv[:3] == ("docker", "rm", "-f"):
            return _ProcessResult(return_code=1, stdout="", stderr="cleanup failed")
        return self.result


class _BlockingProcess(_FakeProcess):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def run_argv(self, argv: tuple[str, ...], **kwargs: object) -> _ProcessResult:
        self.calls.append((argv, kwargs))
        if argv[:3] == ("docker", "rm", "-f"):
            return _ProcessResult(stdout="", stderr="", stdout_original_byte_count=0)
        self.started.set()
        self.release.wait(timeout=5)
        return self.result


class _FakeArtifacts:
    def __init__(self) -> None:
        self.values: list[bytes] = []

    async def put_bytes(self, data: bytes, **kwargs: object) -> ArtifactDescriptor:
        del kwargs
        self.values.append(data)
        digest = f"{len(self.values):064x}"
        return ArtifactDescriptor(
            digest=digest,
            media_type="application/json",
            byte_count=len(data),
            storage_path=Path("sha256") / digest[:2] / f"{digest[2:]}.blob",
        )


class _FakeTelemetry:
    class _Span:
        def __enter__(self) -> _FakeTelemetry._Span:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def start_span(self, operation: str, **kwargs: object) -> _Span:
        self.calls.append({"operation": operation, **kwargs})
        return self._Span()


def test_runner_dockerfile_uses_only_pinned_runtime_stages() -> None:
    repository = Path(__file__).resolve().parents[4]
    dockerfile = (repository / "Dockerfile.runner").read_text(encoding="utf-8")
    guidance = (repository / "AGENTS.md").read_text(encoding="utf-8")
    python_reference = (
        "python:3.14.2-slim@sha256:51f5baff157fee39a31e5b32394dde7ed2977bcea7a0b16a8978a8d23c270f85"
    )
    node_reference = (
        "node:24.19.0-slim@sha256:65932751ed4073ed02f5c04e494e4b2572a891b7dbea0568a863dc80341bf848"
    )

    assert dockerfile.count("FROM ") == 2
    assert python_reference in dockerfile
    assert node_reference in dockerfile
    assert python_reference in guidance
    assert node_reference in guidance
    assert "USER 10001:10001" in dockerfile
    assert "docker-ce" not in dockerfile
    assert "docker.io" not in dockerfile


def test_docker_argv_has_fixed_isolation_and_one_canonical_mount(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    worktree.mkdir()
    command = _command()
    runner = DockerRunner(
        policy=_policy(command),
        root=CanonicalRoot(worktree),
        image_digest="parallel-forge-runner@sha256:" + "a" * 64,
    )

    argv = runner.build_argv(
        worktree=worktree,
        spec=command,
        environment={"DATABASE_URL": "postgresql://scoped", "FORGE_INPUT": "secret"},
    )

    assert argv[:3] == ["docker", "run", "--rm"]
    assert "--pull=never" in argv
    assert "--network=none" in argv
    assert "--read-only" in argv
    assert "--security-opt=no-new-privileges" in argv
    assert "--cap-drop=ALL" in argv
    assert "--pids-limit=256" in argv
    assert "--memory=2g" in argv
    assert "--cpus=2" in argv
    assert "--user" in argv
    assert argv[argv.index("--user") + 1] == "10001:10001"
    assert "/var/run/docker.sock" not in " ".join(argv)
    assert "postgresql://scoped" not in argv
    assert "secret" not in argv
    assert argv[argv.index("parallel-forge-runner@sha256:" + "a" * 64) + 1 :] == list(command.argv)
    assert sum(str(worktree) in part for part in argv) == 1
    assert sum(part == "--mount" for part in argv) == 1
    mount = argv[argv.index("--mount") + 1]
    assert "readonly" not in mount


def test_docker_runner_resolves_name_and_kind_before_execution(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    worktree.mkdir()
    command = _command()
    process = _FakeProcess()
    runner = DockerRunner(
        policy=_policy(command),
        root=CanonicalRoot(worktree),
        image_digest="sha256:" + "b" * 64,
        process_runner=process,
        artifact_store=_FakeArtifacts(),
        telemetry=_FakeTelemetry(),
    )

    with pytest.raises(UnknownNamedCommand):
        # A caller cannot smuggle command text through the name field.
        asyncio.run(
            runner.run(
                RunCommandRequest(
                    command_name="npm test && curl attacker.invalid",
                    kind=StepKind.TEST,
                )
            )
        )
    assert process.calls == []


def test_docker_runner_persists_deterministic_redacted_output(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    worktree.mkdir()
    command = _command()
    process = _FakeProcess(
        _ProcessResult(
            stdout="token=literal-secret\n",
            stderr="warning\n",
            stdout_original_byte_count=21,
            stderr_original_byte_count=8,
        )
    )
    artifacts = _FakeArtifacts()
    runner = DockerRunner(
        policy=_policy(command),
        root=CanonicalRoot(worktree),
        image_digest="sha256:" + "c" * 64,
        process_runner=process,
        artifact_store=artifacts,
        telemetry=_FakeTelemetry(),
    )

    result = asyncio.run(
        runner.run(
            RunCommandRequest(
                command_name=command.name,
                kind=command.kind,
                environment={"FORGE_INPUT": "literal-secret"},
            )
        )
    )

    assert len(artifacts.values) == 2
    stdout_evidence = json.loads(artifacts.values[0])
    assert stdout_evidence == {
        "captured_byte_count": len(stdout_evidence["text"].encode()),
        "encoding": "utf-8-replacement",
        "original_byte_count": 21,
        "stream": "stdout",
        "text": "token=[REDACTED]\n",
        "truncated": False,
    }
    assert b"literal-secret" not in artifacts.values[0]
    assert result.stdout_digest == "0" * 63 + "1"
    assert result.stderr_digest == "0" * 63 + "2"


def test_docker_timeout_forces_named_container_cleanup_without_host_fallback(
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "repo"
    worktree.mkdir()
    command = _command()
    process = _FakeProcess(_ProcessResult(return_code=-9, timed_out=True, stdout="", stderr=""))
    runner = DockerRunner(
        policy=_policy(command),
        root=CanonicalRoot(worktree),
        image_digest="sha256:" + "d" * 64,
        process_runner=process,
        artifact_store=_FakeArtifacts(),
        telemetry=_FakeTelemetry(),
    )

    result = asyncio.run(
        runner.run(RunCommandRequest(command_name=command.name, kind=command.kind))
    )

    assert result.timed_out is True
    assert process.calls[1][0][:3] == ("docker", "rm", "-f")
    assert process.calls[1][0][3] == process.calls[0][0][process.calls[0][0].index("--name") + 1]


def test_docker_uncertain_launch_surfaces_generic_error_and_cleans_up(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    worktree.mkdir()
    command = _command()
    process = _FakeProcess(error=OSError("secret launch detail"))
    runner = DockerRunner(
        policy=_policy(command),
        root=CanonicalRoot(worktree),
        image_digest="sha256:" + "e" * 64,
        process_runner=process,
        artifact_store=_FakeArtifacts(),
        telemetry=_FakeTelemetry(),
    )

    with pytest.raises(RunnerExecutionError, match="^runner execution failed$"):
        asyncio.run(runner.run(RunCommandRequest(command_name=command.name, kind=command.kind)))
    assert len(process.calls) == 2


@pytest.mark.parametrize("control_key", ["PATH", "DOCKER_HOST", "TEMP"])
def test_docker_rejects_client_control_environment_keys(
    tmp_path: Path,
    control_key: str,
) -> None:
    worktree = tmp_path / "repo"
    worktree.mkdir()
    command = _command(environment_keys=(control_key,))
    runner = DockerRunner(
        policy=_policy(command),
        root=CanonicalRoot(worktree),
        image_digest="sha256:" + "f" * 64,
    )

    with pytest.raises(ValueError, match="^runner request environment is not allowed$"):
        runner.build_argv(worktree=worktree, spec=command, environment={control_key: "evil"})


def test_docker_network_and_environment_come_only_from_named_policy(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    worktree.mkdir()
    command = _command(network_enabled=True)
    process = _FakeProcess()
    runner = DockerRunner(
        policy=_policy(command),
        root=CanonicalRoot(worktree),
        image_digest="sha256:" + "1" * 64,
        process_runner=process,
        artifact_store=_FakeArtifacts(),
        telemetry=_FakeTelemetry(),
        docker_environment={"PATH": "trusted-client-path"},
    )

    result = asyncio.run(
        runner.run(
            RunCommandRequest(
                command_name=command.name,
                kind=command.kind,
                environment={"FORGE_INPUT": "container-only"},
            )
        )
    )

    run_argv, kwargs = process.calls[0]
    assert "--network=bridge" in run_argv
    assert "container-only" not in run_argv
    assert kwargs["environment"] == {
        "FORGE_INPUT": "container-only",
        "PATH": "trusted-client-path",
    }
    assert result.network_enabled is True


def test_docker_cli_launch_failure_cleans_up_and_returns_no_command_result(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    worktree.mkdir()
    command = _command()
    process = _FakeProcess(_ProcessResult(return_code=125, stdout="", stderr="daemon failure"))
    runner = DockerRunner(
        policy=_policy(command),
        root=CanonicalRoot(worktree),
        image_digest="sha256:" + "2" * 64,
        process_runner=process,
        artifact_store=_FakeArtifacts(),
        telemetry=_FakeTelemetry(),
    )

    with pytest.raises(RunnerExecutionError, match="^runner execution failed$"):
        asyncio.run(runner.run(RunCommandRequest(command_name=command.name, kind=command.kind)))
    assert process.calls[1][0][:3] == ("docker", "rm", "-f")


def test_docker_cleanup_failure_never_returns_a_timeout_result(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    worktree.mkdir()
    command = _command()
    process = _CleanupFailureProcess(
        _ProcessResult(return_code=-9, timed_out=True, stdout="", stderr="")
    )
    runner = DockerRunner(
        policy=_policy(command),
        root=CanonicalRoot(worktree),
        image_digest="sha256:" + "3" * 64,
        process_runner=process,
        artifact_store=_FakeArtifacts(),
        telemetry=_FakeTelemetry(),
    )

    with pytest.raises(RunnerExecutionError, match="^runner execution failed$"):
        asyncio.run(runner.run(RunCommandRequest(command_name=command.name, kind=command.kind)))


def test_malformed_unicode_output_is_normalized_before_artifact_persistence(
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "repo"
    worktree.mkdir()
    command = _command()
    artifacts = _FakeArtifacts()
    runner = DockerRunner(
        policy=_policy(command),
        root=CanonicalRoot(worktree),
        image_digest="sha256:" + "4" * 64,
        process_runner=_FakeProcess(
            _ProcessResult(stdout="bad-\ud800-output", stdout_original_byte_count=12)
        ),
        artifact_store=artifacts,
        telemetry=_FakeTelemetry(),
    )

    asyncio.run(runner.run(RunCommandRequest(command_name=command.name, kind=command.kind)))
    evidence = json.loads(artifacts.values[0])
    assert evidence["text"] == "bad-�-output"


@pytest.mark.asyncio
async def test_cancelling_docker_run_forces_exact_container_cleanup(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    worktree.mkdir()
    command = _command()
    process = _BlockingProcess()
    runner = DockerRunner(
        policy=_policy(command),
        root=CanonicalRoot(worktree),
        image_digest="sha256:" + "5" * 64,
        process_runner=process,
        artifact_store=_FakeArtifacts(),
        telemetry=_FakeTelemetry(),
    )

    task = asyncio.create_task(
        runner.run(RunCommandRequest(command_name=command.name, kind=command.kind))
    )
    assert await asyncio.to_thread(process.started.wait, 1)
    task.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        process.release.set()

    cleanup = process.calls[1][0]
    assert cleanup[:3] == ("docker", "rm", "-f")
    assert cleanup[3] == process.calls[0][0][process.calls[0][0].index("--name") + 1]
