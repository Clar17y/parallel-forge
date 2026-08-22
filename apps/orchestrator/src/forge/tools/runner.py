"""Exact named-command resolution shared by every runner adapter."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Mapping
from typing import Protocol

from forge.application.ports.artifacts import ArtifactStore
from forge.application.ports.repository import ProcessResult
from forge.domain.policy import CommandSpec, ProjectPolicy, StepKind
from forge.domain.validation import UnknownNamedCommand, command_spec_digest
from forge.observability.redaction import RedactionPolicy, Redactor

_OUTPUT_TEXT_MAX_BYTES = 1024 * 1024
_ENVIRONMENT_VALUE_MAX_BYTES = 64 * 1024


class RunnerExecutionError(RuntimeError):
    """A command could not cross the configured runner boundary safely."""

    def __init__(self) -> None:
        super().__init__("runner execution failed")


async def await_deferred_cancellation[DeferredResult](
    awaitable: Awaitable[DeferredResult],
    *,
    already_cancelled: bool = False,
) -> tuple[DeferredResult, bool]:
    """Finish a bounded operation before allowing caller cancellation to propagate."""

    operation = asyncio.ensure_future(awaitable)
    caller_cancelled = already_cancelled
    while True:
        try:
            result = await asyncio.shield(operation)
        except asyncio.CancelledError:
            if operation.cancelled():
                raise
            caller_cancelled = True
            continue
        return result, caller_cancelled


class _ProcessResultLike(Protocol):
    return_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    stdout_original_byte_count: int
    stderr_original_byte_count: int
    stdout_truncated: bool
    stderr_truncated: bool


class NamedCommandResolver:
    """Resolve one exact policy name without parsing command text."""

    def __init__(self, policy: ProjectPolicy) -> None:
        if not isinstance(policy, ProjectPolicy):
            raise TypeError("named commands require a project policy")
        self._commands = {command.name: command for command in policy.commands}

    def resolve(self, name: str, *, kind: StepKind) -> CommandSpec:
        """Return only an exact name and expected-kind match."""

        if type(name) is not str or not isinstance(kind, StepKind):
            raise UnknownNamedCommand()
        command = self._commands.get(name)
        if command is None or command.kind is not kind:
            raise UnknownNamedCommand()
        return command


def select_environment(
    spec: CommandSpec,
    environment: Mapping[str, str],
    *,
    denied_keys: frozenset[str] = frozenset(),
    denied_prefixes: tuple[str, ...] = (),
) -> dict[str, str]:
    """Detach exactly the values permitted by one resolved command."""

    if not isinstance(environment, Mapping):
        raise TypeError("runner request environment must be a mapping")
    try:
        detached = dict(environment)
    except TypeError, ValueError:
        raise ValueError("runner request environment is not allowed") from None
    permitted = frozenset(spec.environment_keys)
    if set(detached) - permitted:
        raise ValueError("runner request environment is not allowed")
    for key, value in detached.items():
        if not isinstance(key, str):
            raise TypeError("runner request environment keys must be strings")
        normalized = key.upper()
        if (
            not key
            or normalized in denied_keys
            or any(normalized.startswith(prefix) for prefix in denied_prefixes)
            or not isinstance(value, str)
            or "\x00" in value
            or _utf8_size(value) > _ENVIRONMENT_VALUE_MAX_BYTES
        ):
            raise ValueError("runner request environment is not allowed")
    if any(
        key.upper() in denied_keys
        or any(key.upper().startswith(prefix) for prefix in denied_prefixes)
        for key in permitted
    ):
        raise ValueError("runner request environment is not allowed")
    return detached


async def persist_output_artifacts(
    artifact_store: ArtifactStore,
    result: ProcessResult | _ProcessResultLike,
    *,
    secrets: Mapping[str, str],
) -> tuple[str, str]:
    """Persist deterministic redacted envelopes for both bounded streams."""

    redactor = Redactor(
        secrets=(value for value in secrets.values() if value),
        policy=RedactionPolicy(max_string_bytes=_OUTPUT_TEXT_MAX_BYTES),
    )
    try:
        stdout = await _persist_stream(
            artifact_store,
            redactor,
            stream="stdout",
            text=result.stdout,
            original_byte_count=result.stdout_original_byte_count,
            truncated=result.stdout_truncated,
        )
        stderr = await _persist_stream(
            artifact_store,
            redactor,
            stream="stderr",
            text=result.stderr,
            original_byte_count=result.stderr_original_byte_count,
            truncated=result.stderr_truncated,
        )
    except Exception:  # noqa: BLE001 - artifact adapters fail as one safe runner error
        raise RunnerExecutionError() from None
    return stdout, stderr


async def _persist_stream(
    artifact_store: ArtifactStore,
    redactor: Redactor,
    *,
    stream: str,
    text: str,
    original_byte_count: int,
    truncated: bool,
) -> str:
    safe_text = redactor.redact(text)
    if not isinstance(safe_text, str):
        raise RunnerExecutionError()
    redactor_truncated = len(text.encode("utf-8", errors="replace")) > _OUTPUT_TEXT_MAX_BYTES
    payload = {
        "captured_byte_count": len(safe_text.encode("utf-8")),
        "encoding": "utf-8-replacement",
        "original_byte_count": original_byte_count,
        "stream": stream,
        "text": safe_text,
        "truncated": truncated or redactor_truncated,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    descriptor = await artifact_store.put_bytes(
        encoded,
        media_type="application/vnd.forge.command-output+json",
    )
    return descriptor.digest


def _utf8_size(value: str) -> int:
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise ValueError("runner request environment is not allowed") from None


__all__ = [
    "NamedCommandResolver",
    "RunnerExecutionError",
    "UnknownNamedCommand",
    "await_deferred_cancellation",
    "command_spec_digest",
    "persist_output_artifacts",
    "select_environment",
]
