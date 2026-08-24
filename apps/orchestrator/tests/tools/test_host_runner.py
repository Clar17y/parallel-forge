from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest
from forge.application.ports.runner import RunCommandRequest
from forge.domain.artifact import ArtifactDescriptor
from forge.domain.policy import CommandSpec, ProjectPolicy, RunnerMode, StepKind
from forge.tools.host import TrustedHostRunner
from forge.tools.paths import CanonicalRoot


def _command() -> CommandSpec:
    return CommandSpec(
        kind=StepKind.LINT,
        name="named-lint",
        argv=("lint", "--check"),
        timeout_seconds=10,
        environment_keys=("FORGE_INPUT",),
    )


def _policy(*, trusted: bool = True, mode: RunnerMode = RunnerMode.TRUSTED_HOST) -> ProjectPolicy:
    return ProjectPolicy(
        id=uuid4(),
        version=4,
        repository_path="D:/Code/Parallel",
        github_repository="Clar17y/Parallel",
        default_branch="main",
        trusted_project=trusted,
        runner_mode=mode,
        commands=(_command(),),
    )


@dataclass
class _ProcessResult:
    return_code: int | None = 0
    stdout: str = "lint ok\n"
    stderr: str = ""
    timed_out: bool = False
    stdout_original_byte_count: int = 9
    stderr_original_byte_count: int = 0
    stdout_truncated: bool = False
    stderr_truncated: bool = False


class _FakeProcess:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def run_argv(self, argv: tuple[str, ...], **kwargs: object) -> _ProcessResult:
        self.calls.append((argv, kwargs))
        return _ProcessResult()


class _BlockingProcess(_FakeProcess):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()

    def run_argv(self, argv: tuple[str, ...], **kwargs: object) -> _ProcessResult:
        self.calls.append((argv, kwargs))
        self.started.set()
        self.release.wait(timeout=5)
        self.finished.set()
        return _ProcessResult()


class _FakeArtifacts:
    def __init__(self) -> None:
        self.values: list[bytes] = []

    async def put_bytes(self, data: bytes, **kwargs: object) -> ArtifactDescriptor:
        del kwargs
        self.values.append(data)
        digest = "1" * 64 if b"stdout" in data else "2" * 64
        return ArtifactDescriptor(
            digest=digest,
            media_type="application/json",
            byte_count=len(data),
            storage_path=Path("sha256") / digest[:2] / f"{digest[2:]}.blob",
        )


class _FakeAudit:
    def __init__(
        self,
        *,
        error: BaseException | None = None,
        fail_on_call: int | None = None,
    ) -> None:
        self.calls: list[tuple[str, str, dict[str, object]]] = []
        self.error = error
        self.fail_on_call = fail_on_call

    async def record(self, event_type: str, *, priority: str, payload: dict[str, object]) -> None:
        if self.error is not None and (
            self.fail_on_call is None or len(self.calls) + 1 == self.fail_on_call
        ):
            raise self.error
        self.calls.append((event_type, priority, payload))


class _BlockingAttemptAudit(_FakeAudit):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    async def record(self, event_type: str, *, priority: str, payload: dict[str, object]) -> None:
        self.started.set()
        await asyncio.to_thread(self.release.wait, 5)
        await super().record(event_type, priority=priority, payload=payload)


class _FakeTelemetry:
    class _Span:
        def __enter__(self) -> _FakeTelemetry._Span:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    def start_span(self, operation: str, **kwargs: object) -> _Span:
        del operation, kwargs
        return self._Span()


def test_trusted_host_requires_trusted_policy_and_discloses_network_uncontained(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    command = _command()
    process = _FakeProcess()
    audit = _FakeAudit()
    runner = TrustedHostRunner(
        policy=_policy(),
        root=CanonicalRoot(root),
        process_runner=process,
        artifact_store=_FakeArtifacts(),
        audit=audit,
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

    assert result.unsandboxed is True
    assert result.image_digest is None
    assert result.network_enabled is True
    assert process.calls == [
        (
            ("lint", "--check"),
            {
                "cwd": root.resolve(),
                "environment": {"FORGE_INPUT": "literal-secret"},
                "timeout_seconds": 10,
            },
        )
    ]
    assert [call[0] for call in audit.calls] == [
        "runner.trusted_host.attempt",
        "runner.trusted_host.completed",
    ]
    assert all(call[1] == "high" for call in audit.calls)
    assert audit.calls[1][2]["evidence_digest"] == result.evidence_digest
    assert "literal-secret" not in repr(audit.calls)


def test_trusted_host_fails_closed_when_attempt_audit_fails(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    process = _FakeProcess()
    runner = TrustedHostRunner(
        policy=_policy(),
        root=CanonicalRoot(root),
        process_runner=process,
        artifact_store=_FakeArtifacts(),
        audit=_FakeAudit(error=RuntimeError("database secret")),
        telemetry=_FakeTelemetry(),
    )

    with pytest.raises(RuntimeError, match="^runner audit failed$"):
        asyncio.run(runner.run(RunCommandRequest(command_name="named-lint", kind=StepKind.LINT)))
    assert process.calls == []


@pytest.mark.asyncio
async def test_trusted_host_cancellation_during_attempt_audit_does_not_launch(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    process = _FakeProcess()
    audit = _BlockingAttemptAudit()
    runner = TrustedHostRunner(
        policy=_policy(),
        root=CanonicalRoot(root),
        process_runner=process,
        artifact_store=_FakeArtifacts(),
        audit=audit,
        telemetry=_FakeTelemetry(),
    )

    task = asyncio.create_task(
        runner.run_terminal(RunCommandRequest(command_name="named-lint", kind=StepKind.LINT))
    )
    assert await asyncio.to_thread(audit.started.wait, 1)
    task.cancel()
    task.cancel()
    await asyncio.sleep(0.05)
    assert not task.done()
    audit.release.set()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert process.calls == []


def test_trusted_host_rejects_untrusted_or_docker_policy(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    untrusted_host = _policy().model_copy(update={"trusted_project": False})
    for policy in (untrusted_host, _policy(mode=RunnerMode.DOCKER)):
        with pytest.raises(ValueError, match="^trusted host runner requires trusted policy$"):
            TrustedHostRunner(policy=policy, root=CanonicalRoot(root))


def test_trusted_host_completion_audit_failure_does_not_return_unaudited_result(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    process = _FakeProcess()
    audit = _FakeAudit(error=RuntimeError("secret persistence detail"), fail_on_call=2)
    runner = TrustedHostRunner(
        policy=_policy(),
        root=CanonicalRoot(root),
        process_runner=process,
        artifact_store=_FakeArtifacts(),
        audit=audit,
        telemetry=_FakeTelemetry(),
    )

    with pytest.raises(RuntimeError, match="^runner audit failed$"):
        asyncio.run(runner.run(RunCommandRequest(command_name="named-lint", kind=StepKind.LINT)))
    assert len(process.calls) == 1
    assert [call[0] for call in audit.calls] == ["runner.trusted_host.attempt"]


@pytest.mark.asyncio
async def test_trusted_host_cancellation_finishes_evidence_and_completion_audit(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    process = _BlockingProcess()
    artifacts = _FakeArtifacts()
    audit = _FakeAudit()
    runner = TrustedHostRunner(
        policy=_policy(),
        root=CanonicalRoot(root),
        process_runner=process,
        artifact_store=artifacts,
        audit=audit,
        telemetry=_FakeTelemetry(),
    )

    task = asyncio.create_task(
        runner.run(RunCommandRequest(command_name="named-lint", kind=StepKind.LINT))
    )
    assert await asyncio.to_thread(process.started.wait, 1)
    task.cancel()
    task.cancel()
    marker = asyncio.Event()
    asyncio.get_running_loop().call_soon(marker.set)
    await marker.wait()
    assert not task.done()
    assert not process.finished.is_set()
    process.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.finished.is_set()

    assert len(artifacts.values) == 2
    assert [call[0] for call in audit.calls] == [
        "runner.trusted_host.attempt",
        "runner.trusted_host.completed",
    ]
    completion = audit.calls[1][2]
    assert completion["caller_cancelled"] is True
    assert isinstance(completion["evidence_digest"], str)
