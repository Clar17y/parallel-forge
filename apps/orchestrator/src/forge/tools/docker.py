"""Docker-first execution for exact policy-named repository commands."""

from __future__ import annotations

import asyncio
import os
import re
import secrets
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Protocol, cast
from uuid import uuid4

from forge.application.ports.artifacts import ArtifactStore
from forge.application.ports.clock import Clock, SystemClock
from forge.application.ports.runner import (
    CommandResult,
    CommandTerminalResult,
    RunCommandRequest,
)
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
_CONTAINER_ID = re.compile(r"[0-9a-f]{12,64}\Z")
_OWNER_LABEL = "forge.owner-token"
_ABSENT_CONTAINER_ERROR = re.compile(
    r"(?:(?:error(?: response from daemon)?):\s*)?"
    r"no such (?:container|object):\s*([^\r\n]+)\Z",
    re.IGNORECASE,
)


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


class _CancelledWithoutTerminal(RunnerExecutionError):
    """Compatibility-only marker when cancellation wins an adapter failure."""


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
        """Run one command while preserving the compatibility cancellation contract."""

        try:
            terminal = await self.run_terminal(request)
        except _CancelledWithoutTerminal:
            raise asyncio.CancelledError() from None
        if terminal.caller_cancelled:
            raise asyncio.CancelledError()
        return terminal.result

    async def run_terminal(self, request: RunCommandRequest) -> CommandTerminalResult:
        """Finish all bounded terminal work before reporting caller cancellation."""

        return await self._run_terminal_at(request, self._root.path)

    async def _run_terminal_at(
        self,
        request: RunCommandRequest,
        cwd: str | os.PathLike[str],
        *,
        managed: bool = False,
        before_launch: Callable[[], None] | None = None,
    ) -> CommandTerminalResult:
        """Run through a caller-supplied trusted capability-derived cwd."""

        spec = self._resolver.resolve(request.command_name, kind=request.kind)
        selected = _docker_command_environment(spec, request.environment)
        if self._artifact_store is None:
            raise RunnerExecutionError()
        container_name = f"forge-runner-{uuid4().hex}"
        owner_token = secrets.token_urlsafe(32) if managed else None
        client_environment = dict(self._docker_environment)
        client_environment.update(selected)
        if managed:
            try:
                await self._assert_managed_name_absent(
                    container_name,
                    cwd=cwd,
                    environment=client_environment,
                )
            except RunnerExecutionError:
                raise
            except Exception:  # noqa: BLE001 - ownership proof is fail-closed
                raise RunnerExecutionError() from None
        argv = self.build_argv(
            worktree=cwd,
            spec=spec,
            environment=selected,
            container_name=container_name,
        )
        if owner_token is not None:
            label_index = argv.index("--pull=never") + 1
            argv[label_index:label_index] = ["--label", f"{_OWNER_LABEL}={owner_token}"]
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
                if before_launch is not None:
                    before_launch()
                process_result, caller_cancelled = await await_deferred_cancellation(
                    asyncio.to_thread(
                        self._process_runner.run_argv,
                        tuple(argv),
                        cwd=cwd,
                        environment=client_environment,
                        timeout_seconds=spec.timeout_seconds,
                    ),
                    state=launch_cancellation,
                )
            except asyncio.CancelledError:
                await self._cleanup_for_terminal(
                    container_name,
                    owner_token=owner_token,
                    cwd=cwd,
                    environment=client_environment,
                )
                raise
            except Exception:  # noqa: BLE001 - adapter failures must cross as one safe error
                cleanup_cancelled = await self._cleanup_for_terminal(
                    container_name,
                    owner_token=owner_token,
                    cwd=cwd,
                    environment=client_environment,
                )
                if launch_cancellation.requested or cleanup_cancelled:
                    raise _CancelledWithoutTerminal() from None
                raise RunnerExecutionError() from None
            if caller_cancelled or process_result.timed_out or process_result.return_code == 125:
                cleanup_cancelled = await self._cleanup_for_terminal(
                    container_name,
                    owner_token=owner_token,
                    cwd=cwd,
                    environment=client_environment,
                )
                caller_cancelled = caller_cancelled or cleanup_cancelled
                if process_result.return_code == 125:
                    raise RunnerExecutionError()
            (stdout_digest, stderr_digest), caller_cancelled = await await_deferred_cancellation(
                persist_output_artifacts(
                    self._artifact_store,
                    process_result,
                    secrets=selected,
                ),
                already_cancelled=caller_cancelled,
            )
        duration_ms = max(0, round((self._monotonic() - started) * 1000))
        return CommandTerminalResult(
            result=CommandResult(
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
            ),
            caller_cancelled=caller_cancelled,
        )

    async def _cleanup_for_terminal(
        self,
        container_name: str,
        *,
        owner_token: str | None = None,
        cwd: str | os.PathLike[str] | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> bool:
        """Finish forced removal and report cancellation delivered during cleanup."""

        operation = (
            self._cleanup_managed(
                container_name,
                owner_token=owner_token,
                cwd=cwd if cwd is not None else self._root.path,
                environment=environment if environment is not None else self._docker_environment,
            )
            if owner_token is not None
            else self._cleanup(container_name)
        )
        _, caller_cancelled = await await_deferred_cancellation(operation)
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

    async def _assert_managed_name_absent(
        self,
        container_name: str,
        *,
        cwd: str | os.PathLike[str],
        environment: Mapping[str, str],
    ) -> None:
        existing = await asyncio.to_thread(
            self._query_managed_container,
            container_name,
            cwd=cwd,
            environment=environment,
        )
        if existing is not None:
            raise RunnerExecutionError()

    async def _cleanup_managed(
        self,
        container_name: str,
        *,
        owner_token: str,
        cwd: str | os.PathLike[str],
        environment: Mapping[str, str],
    ) -> None:
        for attempt in range(_CLEANUP_MAX_ATTEMPTS):
            container = await asyncio.to_thread(
                self._query_managed_container,
                container_name,
                cwd=cwd,
                environment=environment,
            )
            if container is None:
                return
            container_id, label = container
            if label != owner_token:
                raise RunnerExecutionError()
            try:
                removed = await asyncio.to_thread(
                    self._process_runner.run_argv,
                    ("docker", "rm", "-f", container_id),
                    cwd=cwd,
                    environment=self._docker_environment,
                    timeout_seconds=_CLEANUP_TIMEOUT_SECONDS,
                )
            except Exception:  # noqa: BLE001 - cleanup fails closed
                removed = None
            if removed is not None and not removed.timed_out and removed.return_code == 0:
                verified = await asyncio.to_thread(
                    self._query_managed_container,
                    container_name,
                    cwd=cwd,
                    environment=environment,
                )
                if verified is None:
                    return
                if verified[0] != container_id or verified[1] != owner_token:
                    raise RunnerExecutionError()
            if attempt + 1 < _CLEANUP_MAX_ATTEMPTS:
                await asyncio.sleep(_CLEANUP_RETRY_DELAY_SECONDS)
        raise RunnerExecutionError()

    def _query_managed_container(
        self,
        container_name: str,
        *,
        cwd: str | os.PathLike[str],
        environment: Mapping[str, str],
    ) -> tuple[str, str] | None:
        try:
            result = self._process_runner.run_argv(
                (
                    "docker",
                    "inspect",
                    "--type",
                    "container",
                    "--format",
                    f'{{{{.Id}}}}\t{{{{index .Config.Labels "{_OWNER_LABEL}"}}}}',
                    container_name,
                ),
                cwd=cwd,
                environment=environment,
                timeout_seconds=_CLEANUP_TIMEOUT_SECONDS,
            )
        except Exception:  # noqa: BLE001 - query details must not cross the runner boundary
            raise RunnerExecutionError() from None
        if result.timed_out:
            raise RunnerExecutionError()
        if (
            type(result.return_code) is not int
            or type(result.timed_out) is not bool
            or not isinstance(result.stdout, str)
            or not isinstance(result.stderr, str)
        ):
            raise RunnerExecutionError()
        output = result.stdout.strip()
        stderr = result.stderr.strip()
        absence = _ABSENT_CONTAINER_ERROR.fullmatch(stderr)
        if (
            result.return_code == 1
            and not output
            and absence is not None
            and absence.group(1).strip() == container_name
        ):
            return None
        if result.return_code != 0 or not output or "\n" in output:
            raise RunnerExecutionError()
        fields = output.split("\t")
        if len(fields) != 2 or _CONTAINER_ID.fullmatch(fields[0]) is None or not fields[1]:
            raise RunnerExecutionError()
        return fields[0], fields[1]


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
        try:
            relative = candidate.relative_to(root.path)
            normalized = root.normalize(relative, allow_root=True)
            access = root._active_directory_access(normalized)
            if access is None:
                raise ValueError("runner worktree is not capability-bound")
            access = root._verify_directory_access(normalized, access)
            if os.name == "nt":
                candidate = access.path
            else:
                proc_fd_root = Path("/proc") / str(os.getpid()) / "fd"
                if not proc_fd_root.is_dir():
                    raise ValueError("runner worktree capability is unavailable")
                candidate = proc_fd_root / str(access.capability)
        except OSError, RuntimeError, TypeError, ValueError:
            raise ValueError("runner worktree is not capability-bound") from None
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
