"""Framework-free contracts for exact named command execution."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

from forge.domain.policy import RunnerMode, StepKind
from forge.domain.validation import require_evidence_digest, validate_runner_image_reference

if TYPE_CHECKING:
    from forge.application.ports.worktrees import ManagedWorktree
    from forge.domain.policy import ProjectPolicy


@dataclass(frozen=True, slots=True, kw_only=True)
class RunCommandRequest:
    """One agent-safe request containing no argv or runner controls."""

    command_name: str
    kind: StepKind
    environment: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.command_name, str):
            raise TypeError("command name must be a string")
        if not isinstance(self.kind, StepKind):
            raise TypeError("command kind must be a StepKind")
        if not isinstance(self.environment, Mapping):
            raise TypeError("command environment must be a mapping")
        detached = dict(self.environment)
        for key, value in detached.items():
            if (
                not isinstance(key, str)
                or not key
                or "\x00" in key
                or not isinstance(value, str)
                or "\x00" in value
            ):
                raise ValueError("command environment must contain valid string values")
        object.__setattr__(self, "environment", MappingProxyType(detached))

    def __repr__(self) -> str:
        """Redact environment values from diagnostic representations."""

        return (
            f"{type(self).__name__}(command_name={self.command_name!r}, "
            f"kind={self.kind.value!r}, "
            f"environment_keys={tuple(sorted(self.environment))!r})"
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class CommandResult:
    """Immutable command and runner evidence safe to bind into approvals."""

    command_name: str
    kind: StepKind
    command_digest: str
    policy_version: int
    exit_code: int | None
    timed_out: bool
    started_at: datetime
    duration_ms: int
    stdout_digest: str
    stderr_digest: str
    runner_mode: RunnerMode
    image_digest: str | None
    network_enabled: bool
    stdout_original_byte_count: int
    stderr_original_byte_count: int
    stdout_truncated: bool
    stderr_truncated: bool
    unsandboxed: bool

    def __post_init__(self) -> None:
        if not isinstance(self.command_name, str) or not self.command_name:
            raise ValueError("command result requires a command name")
        if not isinstance(self.kind, StepKind):
            raise TypeError("command result kind must be a StepKind")
        for field_name in ("command_digest", "stdout_digest", "stderr_digest"):
            require_evidence_digest(getattr(self, field_name), field_name)
        if type(self.policy_version) is not int or self.policy_version < 1:
            raise ValueError("command policy version must be positive")
        if self.exit_code is not None and type(self.exit_code) is not int:
            raise TypeError("command exit code must be an integer or absent")
        if type(self.timed_out) is not bool:
            raise TypeError("command timeout evidence must be boolean")
        if self.exit_code is None and not self.timed_out:
            raise ValueError("a completed command requires an exit code")
        if not isinstance(self.started_at, datetime) or self.started_at.utcoffset() is None:
            raise ValueError("command start time must be timezone-aware")
        for field_name in (
            "duration_ms",
            "stdout_original_byte_count",
            "stderr_original_byte_count",
        ):
            value = getattr(self, field_name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{field_name} must be a nonnegative integer")
        for field_name in (
            "network_enabled",
            "stdout_truncated",
            "stderr_truncated",
            "unsandboxed",
        ):
            if type(getattr(self, field_name)) is not bool:
                raise TypeError(f"{field_name} must be boolean")
        if not isinstance(self.runner_mode, RunnerMode):
            raise TypeError("runner mode must be a RunnerMode")
        self._validate_runner_disclosure()

    def _validate_runner_disclosure(self) -> None:
        if self.runner_mode is RunnerMode.DOCKER:
            if self.image_digest is None:
                raise ValueError("Docker command evidence requires an image digest")
            validate_runner_image_reference(self.image_digest)
            if not self.image_digest:
                raise ValueError("Docker command evidence requires an image digest")
            if self.unsandboxed:
                raise ValueError("Docker command evidence cannot claim trusted-host execution")
            return
        if self.image_digest is not None:
            raise ValueError("trusted-host command evidence has no container image")
        if not self.unsandboxed:
            raise ValueError("trusted-host command evidence must disclose unsandboxed execution")

    @property
    def evidence_digest(self) -> str:
        """Hash every command, outcome, artifact, and runner disclosure field."""

        values = asdict(self)
        values["kind"] = self.kind.value
        values["runner_mode"] = self.runner_mode.value
        values["started_at"] = self.started_at.isoformat()
        payload = json.dumps(
            values,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True, kw_only=True)
class CommandTerminalResult:
    """The immutable outcome at the command's terminal evidence boundary.

    ``caller_cancelled`` is deliberately recorded rather than raised here.  A
    caller that needs the compatibility behaviour can use ``RunnerPort.run``;
    orchestration code uses this value to persist the terminal checkpoint before
    propagating cancellation.
    """

    result: CommandResult
    caller_cancelled: bool

    def __post_init__(self) -> None:
        if not isinstance(self.result, CommandResult):
            raise TypeError("terminal command result requires command evidence")
        if type(self.caller_cancelled) is not bool:
            raise TypeError("terminal cancellation marker must be boolean")


class RunnerPort(Protocol):
    """The one asynchronous path for every repository-controlled command."""

    async def run(self, request: RunCommandRequest) -> CommandResult: ...


class TerminalRunnerPort(Protocol):
    """Runner contract that exposes completed evidence before cancellation."""

    async def run_terminal(self, request: RunCommandRequest) -> CommandTerminalResult: ...


@runtime_checkable
class WorktreeRunnerPort(RunnerPort, TerminalRunnerPort, Protocol):
    """Compatibility and terminal runner methods required by E2 orchestration."""


class WorktreeRunnerFactoryPort(Protocol):
    """Construct a runner bound to one exact retained managed worktree."""

    def create(
        self,
        worktree: ManagedWorktree,
        policy: ProjectPolicy,
    ) -> WorktreeRunnerPort: ...


class RunnerAuditSink(Protocol):
    """A context-bound durable sink used to disclose trusted-host execution."""

    async def record(
        self,
        event_type: str,
        *,
        priority: Literal["high"],
        payload: Mapping[str, object],
    ) -> None: ...


__all__ = [
    "CommandResult",
    "CommandTerminalResult",
    "RunCommandRequest",
    "RunnerAuditSink",
    "RunnerPort",
    "TerminalRunnerPort",
    "WorktreeRunnerFactoryPort",
    "WorktreeRunnerPort",
]
