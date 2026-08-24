"""Explicitly unsandboxed execution for operator-designated trusted projects."""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Protocol, cast

from forge.application.ports.artifacts import ArtifactStore
from forge.application.ports.clock import Clock, SystemClock
from forge.application.ports.runner import (
    CommandResult,
    CommandTerminalResult,
    RunCommandRequest,
    RunnerAuditSink,
)
from forge.domain.policy import ProjectPolicy, RunnerMode
from forge.domain.validation import command_spec_digest
from forge.observability.telemetry import Telemetry
from forge.tools.paths import CanonicalRoot
from forge.tools.process import ProcessRunner
from forge.tools.runner import (
    NamedCommandResolver,
    RunnerExecutionError,
    await_deferred_cancellation,
    persist_output_artifacts,
    select_environment,
)

_HOST_CONTROL_KEYS = frozenset(
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
_HOST_CONTROL_PREFIXES = ("DOCKER_",)


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


class TrustedHostRunner:
    """Run exact policy commands with an explicit no-containment disclosure."""

    def __init__(
        self,
        *,
        policy: ProjectPolicy,
        root: CanonicalRoot,
        process_runner: _ProcessRunnerLike | None = None,
        artifact_store: ArtifactStore | None = None,
        audit: RunnerAuditSink | None = None,
        telemetry: Telemetry | None = None,
        clock: Clock | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if (
            not isinstance(policy, ProjectPolicy)
            or policy.runner_mode is not RunnerMode.TRUSTED_HOST
            or not policy.trusted_project
        ):
            raise ValueError("trusted host runner requires trusted policy")
        if not isinstance(root, CanonicalRoot):
            raise TypeError("trusted host runner requires a canonical root")
        self._policy = policy
        self._root = root
        self._resolver = NamedCommandResolver(policy)
        self._process_runner = cast(
            _ProcessRunnerLike,
            process_runner if process_runner is not None else ProcessRunner(root),
        )
        self._artifact_store = artifact_store
        self._audit = audit
        self._telemetry = telemetry if telemetry is not None else Telemetry()
        self._clock = clock or SystemClock()
        self._monotonic = monotonic

    async def run(self, request: RunCommandRequest) -> CommandResult:
        """Run one command while preserving the compatibility cancellation contract."""

        terminal = await self.run_terminal(request)
        if terminal.caller_cancelled:
            raise asyncio.CancelledError()
        return terminal.result

    async def run_terminal(self, request: RunCommandRequest) -> CommandTerminalResult:
        """Finish process, artifacts, and completion audit before cancellation."""

        return await self._run_terminal_at(request, self._root.path)

    async def _run_terminal_at(
        self,
        request: RunCommandRequest,
        cwd: str | os.PathLike[str],
        *,
        before_launch: Callable[[], None] | None = None,
    ) -> CommandTerminalResult:
        """Run from a trusted capability-derived working directory."""

        spec = self._resolver.resolve(request.command_name, kind=request.kind)
        selected = select_environment(
            spec,
            request.environment,
            denied_keys=_HOST_CONTROL_KEYS,
            denied_prefixes=_HOST_CONTROL_PREFIXES,
        )
        if self._artifact_store is None or self._audit is None:
            raise RunnerExecutionError()
        audit_payload = {
            "command_kind": spec.kind.value,
            "command_name": spec.name,
            "network_containment": False,
            "policy_version": self._policy.version,
            "runner_mode": RunnerMode.TRUSTED_HOST.value,
            "unsandboxed": True,
        }
        _, caller_cancelled = await await_deferred_cancellation(
            self._record_audit("runner.trusted_host.attempt", audit_payload)
        )
        started_at = self._clock.now()
        started = self._monotonic()
        with self._telemetry.start_span(
            "runner.trusted_host",
            attributes=audit_payload,
        ):
            try:
                if before_launch is not None:
                    before_launch()
                process_result, launch_cancelled = await await_deferred_cancellation(
                    asyncio.to_thread(
                        self._process_runner.run_argv,
                        spec.argv,
                        cwd=cwd,
                        environment=selected,
                        timeout_seconds=spec.timeout_seconds,
                    ),
                    already_cancelled=caller_cancelled,
                )
                caller_cancelled = caller_cancelled or launch_cancelled
            except Exception:  # noqa: BLE001 - adapter failures must cross as one safe error
                raise RunnerExecutionError() from None
            (stdout_digest, stderr_digest), artifact_cancelled = await await_deferred_cancellation(
                persist_output_artifacts(
                    self._artifact_store,
                    process_result,
                    secrets=selected,
                ),
                already_cancelled=caller_cancelled,
            )
            caller_cancelled = caller_cancelled or artifact_cancelled
        duration_ms = max(0, round((self._monotonic() - started) * 1000))
        result = CommandResult(
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
            runner_mode=RunnerMode.TRUSTED_HOST,
            image_digest=None,
            network_enabled=True,
            stdout_original_byte_count=process_result.stdout_original_byte_count,
            stderr_original_byte_count=process_result.stderr_original_byte_count,
            stdout_truncated=process_result.stdout_truncated,
            stderr_truncated=process_result.stderr_truncated,
            unsandboxed=True,
        )
        completion_payload = {**audit_payload, "evidence_digest": result.evidence_digest}
        if caller_cancelled:
            completion_payload["caller_cancelled"] = True
        _, completion_cancelled = await await_deferred_cancellation(
            self._record_audit(
                "runner.trusted_host.completed",
                completion_payload,
            ),
            already_cancelled=caller_cancelled,
        )
        caller_cancelled = caller_cancelled or completion_cancelled
        return CommandTerminalResult(result=result, caller_cancelled=caller_cancelled)

    async def _record_audit(self, event_type: str, payload: Mapping[str, object]) -> None:
        audit = self._audit
        if audit is None:
            raise RunnerExecutionError()
        try:
            await audit.record(event_type, priority="high", payload=payload)
        except Exception:  # noqa: BLE001 - audit failure is a fail-closed security boundary
            raise RuntimeError("runner audit failed") from None


__all__ = ["TrustedHostRunner"]
