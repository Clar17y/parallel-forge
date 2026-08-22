"""Docker-first execution for exact policy-named repository commands."""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Protocol, cast
from uuid import uuid4

from forge.application.ports.artifacts import ArtifactStore
from forge.application.ports.clock import Clock, SystemClock
from forge.application.ports.runner import CommandResult, RunCommandRequest
from forge.domain.policy import CommandSpec, ProjectPolicy, RunnerMode
from forge.domain.validation import command_spec_digest, validate_runner_image_reference
from forge.observability.telemetry import Telemetry
from forge.tools.paths import CanonicalRoot
from forge.tools.process import ProcessRunner
from forge.tools.runner import (
    DeferredCancellationState,
    NamedCommandResolver,
    RunnerExecutionError,
    await_deferred_cancellation,
    persist_output_artifacts,
    select_environment,
)

_DOCKER_CONTROL_KEYS = frozenset(
    {
        "APPDATA",
        "COMSPEC",
        "HOME",
        "LOCALAPPDATA",
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "USERPROFILE",
    }
)
_DOCKER_CONTROL_PREFIXES = ("DOCKER_",)
_DOCKER_BASELINE_KEYS = frozenset(
    {
        "APPDATA",
        "COMSPEC",
        "HOME",
        "LOCALAPPDATA",
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "USERPROFILE",
    }
)
_CLEANUP_TIMEOUT_SECONDS = 15.0
_CLEANUP_MAX_ATTEMPTS = 3
_CLEANUP_RETRY_DELAY_SECONDS = 0.25


class _ProcessResultLike(Protocol):
    return_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    stdout_original_byte_count: int
    stderr_original_byte_count: int
    stdout_truncated: bool
    stderr_truncated: bool


class _ProcessRunnerLike(Protocol):
    def run_argv(
        self,
        argv: Sequence[str],
        *,
        cwd: str | os.PathLike[str],
        environment: Mapping[str, str],
        timeout_seconds: float | None = None,
    ) -> _ProcessResultLike: ...


class DockerRunner:
    """Run one exact named command inside a constrained container."""

    def __init__(
        self,
        *,
        policy: ProjectPolicy,
        root: CanonicalRoot,
        image_digest: str,
        process_runner: _ProcessRunnerLike | None = None,
        artifact_store: ArtifactStore | None = None,
        telemetry: Telemetry | None = None,
        clock: Clock | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        docker_environment: Mapping[str, str] | None = None,
    ) -> None:
        if not isinstance(policy, ProjectPolicy) or policy.runner_mode is not RunnerMode.DOCKER:
            raise ValueError("Docker runner requires Docker policy")
        if not isinstance(root, CanonicalRoot):
            raise TypeError("Docker runner requires a canonical root")
        self._image_reference = validate_runner_image_reference(image_digest)
        if not self._image_reference:
            raise ValueError("runner image digest is not configured")
        self._policy = policy
        self._root = root
        self._resolver = NamedCommandResolver(policy)
        self._process_runner = cast(
            _ProcessRunnerLike,
            process_runner if process_runner is not None else ProcessRunner(root),
        )
        self._artifact_store = artifact_store
        self._telemetry = telemetry if telemetry is not None else Telemetry()
        self._clock = clock or SystemClock()
        self._monotonic = monotonic
        self._docker_environment = dict(
            _trusted_docker_environment()
            if docker_environment is None
            else _validate_docker_environment(docker_environment)
        )

    def build_argv(
        self,
        *,
        worktree: str | os.PathLike[str],
        spec: CommandSpec,
        environment: Mapping[str, str],
        container_name: str | None = None,
    ) -> list[str]:
        """Build the complete fixed Docker argv without embedding environment values."""

        registered = self._resolver.resolve(spec.name, kind=spec.kind)
        if registered != spec:
            raise ValueError("runner command is not in the active policy")
        selected = _docker_command_environment(registered, environment)
        mount_source = _canonical_mount_source(self._root, worktree)
        name = container_name or f"forge-runner-{uuid4().hex}"
        argv = [
            "docker",
            "run",
            "--rm",
            "--name",
            name,
            "--pull=never",
            "--network=bridge" if registered.network_enabled else "--network=none",
            "--user",
            "10001:10001",
            "--security-opt=no-new-privileges",
            "--cap-drop=ALL",
            "--pids-limit=256",
            "--memory=2g",
            "--cpus=2",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=64m",
            "--tmpfs",
            "/home/forge:rw,nosuid,nodev,size=64m",
            "--mount",
            f"type=bind,src={mount_source},dst=/workspace",
            "--workdir=/workspace",
        ]
        for key in sorted(selected):
            argv.extend(("--env", key))
        argv.append(self._image_reference)
        argv.extend(registered.argv)
        return argv

    async def run(self, request: RunCommandRequest) -> CommandResult:
        """Resolve, execute, clean up, artifact, and disclose one container run."""

        spec = self._resolver.resolve(request.command_name, kind=request.kind)
        selected = _docker_command_environment(spec, request.environment)
        if self._artifact_store is None:
            raise RunnerExecutionError()
        container_name = f"forge-runner-{uuid4().hex}"
        argv = self.build_argv(
            worktree=self._root.path,
            spec=spec,
            environment=selected,
            container_name=container_name,
        )
        client_environment = dict(self._docker_environment)
        client_environment.update(selected)
        started_at = self._clock.now()
        started = self._monotonic()
        attributes = {
            "command_name": spec.name,
            "command_kind": spec.kind.value,
            "network_enabled": spec.network_enabled,
            "policy_version": self._policy.version,
            "runner_mode": RunnerMode.DOCKER.value,
        }
        with self._telemetry.start_span("runner.docker", attributes=attributes):
            launch_cancellation = DeferredCancellationState()
            try:
                process_result, caller_cancelled = await await_deferred_cancellation(
                    asyncio.to_thread(
                        self._process_runner.run_argv,
                        tuple(argv),
                        cwd=self._root.path,
                        environment=client_environment,
                        timeout_seconds=spec.timeout_seconds,
                    ),
                    state=launch_cancellation,
                )
            except asyncio.CancelledError:
                await self._cleanup_for_terminal(container_name)
                raise
            except Exception:  # noqa: BLE001 - adapter failures must cross as one safe error
                cleanup_cancelled = await self._cleanup_for_terminal(container_name)
                if launch_cancellation.requested or cleanup_cancelled:
                    raise asyncio.CancelledError()
                raise RunnerExecutionError() from None
            if caller_cancelled or process_result.timed_out or process_result.return_code == 125:
                cancelled_before_cleanup = caller_cancelled
                cleanup_cancelled = await self._cleanup_for_terminal(container_name)
                if cancelled_before_cleanup or cleanup_cancelled:
                    raise asyncio.CancelledError()
                if process_result.return_code == 125:
                    raise RunnerExecutionError()
            stdout_digest, stderr_digest = await persist_output_artifacts(
                self._artifact_store,
                process_result,
                secrets=selected,
            )
        duration_ms = max(0, round((self._monotonic() - started) * 1000))
        return CommandResult(
            command_name=spec.name,
            kind=spec.kind,
            command_digest=command_spec_digest(spec),
            policy_version=self._policy.version,
            exit_code=process_result.return_code,
            timed_out=process_result.timed_out,
            started_at=started_at,
            duration_ms=duration_ms,
            stdout_digest=stdout_digest,
            stderr_digest=stderr_digest,
            runner_mode=RunnerMode.DOCKER,
            image_digest=self._image_reference,
            network_enabled=spec.network_enabled,
            stdout_original_byte_count=process_result.stdout_original_byte_count,
            stderr_original_byte_count=process_result.stderr_original_byte_count,
            stdout_truncated=process_result.stdout_truncated,
            stderr_truncated=process_result.stderr_truncated,
            unsandboxed=False,
        )

    async def _cleanup_for_terminal(self, container_name: str) -> bool:
        """Finish forced removal and report cancellation delivered during cleanup."""

        _, caller_cancelled = await await_deferred_cancellation(self._cleanup(container_name))
        return caller_cancelled

    async def _cleanup(self, container_name: str) -> None:
        for attempt in range(_CLEANUP_MAX_ATTEMPTS):
            try:
                result = await asyncio.to_thread(
                    self._process_runner.run_argv,
                    ("docker", "rm", "-f", container_name),
                    cwd=self._root.path,
                    environment=self._docker_environment,
                    timeout_seconds=_CLEANUP_TIMEOUT_SECONDS,
                )
                succeeded = not result.timed_out and result.return_code == 0
            except Exception:  # noqa: BLE001 - cleanup must fail closed for any adapter error
                succeeded = False
            if succeeded:
                return
            if attempt + 1 < _CLEANUP_MAX_ATTEMPTS:
                await asyncio.sleep(_CLEANUP_RETRY_DELAY_SECONDS)
        raise RunnerExecutionError()


def _docker_command_environment(
    spec: CommandSpec,
    environment: Mapping[str, str],
) -> dict[str, str]:
    return select_environment(
        spec,
        environment,
        denied_keys=_DOCKER_CONTROL_KEYS,
        denied_prefixes=_DOCKER_CONTROL_PREFIXES,
    )


def _canonical_mount_source(root: CanonicalRoot, worktree: str | os.PathLike[str]) -> str:
    try:
        candidate = Path(worktree).resolve(strict=True)
    except OSError, RuntimeError, TypeError, ValueError:
        raise ValueError("runner worktree is unavailable") from None
    if candidate != root.path:
        raise ValueError("runner worktree is not the canonical root")
    rendered = str(candidate)
    if any(character in rendered for character in ("\x00", "\r", "\n", ",")):
        raise ValueError("runner worktree cannot be represented as a Docker mount")
    return rendered


def _trusted_docker_environment() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if key.upper() in _DOCKER_BASELINE_KEYS or key.upper().startswith("DOCKER_")
    }


def _validate_docker_environment(environment: Mapping[str, str]) -> dict[str, str]:
    detached = dict(environment)
    if any(
        not isinstance(key, str)
        or not key
        or "\x00" in key
        or not isinstance(value, str)
        or "\x00" in value
        for key, value in detached.items()
    ):
        raise ValueError("Docker client environment is invalid")
    return detached


__all__ = ["DockerRunner", "RunnerExecutionError"]
