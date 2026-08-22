"""Framework-free contracts for exact named command execution."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Literal, Protocol

from forge.domain.policy import RunnerMode, StepKind
from forge.domain.validation import require_evidence_digest, validate_runner_image_reference


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


class RunnerPort(Protocol):
    """The one asynchronous path for every repository-controlled command."""

    async def run(self, request: RunCommandRequest) -> CommandResult: ...


class RunnerAuditSink(Protocol):
    """A context-bound durable sink used to disclose trusted-host execution."""

    async def record(
        self,
        event_type: str,
        *,
        priority: Literal["high"],
        payload: Mapping[str, object],
    ) -> None: ...


__all__ = ["CommandResult", "RunCommandRequest", "RunnerAuditSink", "RunnerPort"]
