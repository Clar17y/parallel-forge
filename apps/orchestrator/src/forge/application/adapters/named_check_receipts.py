"""Bounded pure named-check result and stream receipt codec."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict
from datetime import datetime
from types import MappingProxyType
from typing import Any, Final, Literal

from forge.application.ports.runner import CommandResult
from forge.domain.policy import RunnerMode, StepKind

_FIXED_SAFE_ERROR_MESSAGE: Final = "invalid named check receipt"
_COMMAND_RESULT_MAX_BYTES: Final = 64 * 1024
_OUTPUT_TEXT_MAX_BYTES: Final = 1024 * 1024
_OUTPUT_ENVELOPE_MAX_BYTES: Final = 6 * 1024 * 1024 + 1024

_EXPECTED_COMMAND_RESULT_KEYS: Final = frozenset(
    {
        "command_digest",
        "command_name",
        "duration_ms",
        "exit_code",
        "image_digest",
        "kind",
        "network_enabled",
        "policy_version",
        "runner_mode",
        "started_at",
        "stderr_digest",
        "stderr_original_byte_count",
        "stderr_truncated",
        "stdout_digest",
        "stdout_original_byte_count",
        "stdout_truncated",
        "timed_out",
        "unsandboxed",
    }
)

_EXPECTED_STREAM_KEYS: Final = frozenset(
    {
        "captured_byte_count",
        "encoding",
        "original_byte_count",
        "stream",
        "text",
        "truncated",
    }
)


class NamedCheckReceiptError(ValueError):
    """A named-check receipt or stream envelope is malformed or unbound."""

    def __init__(self, message: str = _FIXED_SAFE_ERROR_MESSAGE) -> None:
        super().__init__(_FIXED_SAFE_ERROR_MESSAGE)

    def __str__(self) -> str:
        return _FIXED_SAFE_ERROR_MESSAGE

    def __repr__(self) -> str:
        return "NamedCheckReceiptError()"


def _reject_constant(val: str) -> None:
    raise ValueError("JSON constants not allowed")


def _object_pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    obj: dict[str, Any] = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("duplicate JSON key detected")
        obj[key] = value
    return obj


def encode_command_result(result: CommandResult) -> bytes:
    """Encode a validated CommandResult into canonical UTF-8 JSON wire bytes."""
    if not isinstance(result, CommandResult):
        raise NamedCheckReceiptError()

    try:
        validated = CommandResult(
            command_name=result.command_name,
            kind=result.kind,
            command_digest=result.command_digest,
            policy_version=result.policy_version,
            exit_code=result.exit_code,
            timed_out=result.timed_out,
            started_at=result.started_at,
            duration_ms=result.duration_ms,
            stdout_digest=result.stdout_digest,
            stderr_digest=result.stderr_digest,
            runner_mode=result.runner_mode,
            image_digest=result.image_digest,
            network_enabled=result.network_enabled,
            stdout_original_byte_count=result.stdout_original_byte_count,
            stderr_original_byte_count=result.stderr_original_byte_count,
            stdout_truncated=result.stdout_truncated,
            stderr_truncated=result.stderr_truncated,
            unsandboxed=result.unsandboxed,
        )
    except ValueError, TypeError, AttributeError:
        raise NamedCheckReceiptError() from None

    values = asdict(validated)
    values["kind"] = validated.kind.value
    values["runner_mode"] = validated.runner_mode.value
    values["started_at"] = validated.started_at.isoformat()

    try:
        payload = json.dumps(
            values,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except UnicodeEncodeError, OverflowError, ValueError, TypeError:
        raise NamedCheckReceiptError() from None

    if len(payload) > _COMMAND_RESULT_MAX_BYTES:
        raise NamedCheckReceiptError()

    if hashlib.sha256(payload).hexdigest() != validated.evidence_digest:
        raise NamedCheckReceiptError()

    return payload


def decode_command_result(data: bytes) -> CommandResult:
    """Decode canonical wire bytes into a strictly typed CommandResult."""
    if not isinstance(data, bytes) or len(data) == 0:
        raise NamedCheckReceiptError()

    if len(data) > _COMMAND_RESULT_MAX_BYTES:
        raise NamedCheckReceiptError()

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise NamedCheckReceiptError() from None

    try:
        raw = json.loads(
            text,
            object_pairs_hook=_object_pairs_hook,
            parse_constant=_reject_constant,
        )
    except json.JSONDecodeError, ValueError, RecursionError:
        raise NamedCheckReceiptError() from None

    if not isinstance(raw, dict):
        raise NamedCheckReceiptError()

    if frozenset(raw.keys()) != _EXPECTED_COMMAND_RESULT_KEYS:
        raise NamedCheckReceiptError()

    if type(raw["command_name"]) is not str or not raw["command_name"]:
        raise NamedCheckReceiptError()

    if type(raw["kind"]) is not str:
        raise NamedCheckReceiptError()
    try:
        kind = StepKind(raw["kind"])
    except ValueError:
        raise NamedCheckReceiptError() from None

    if type(raw["command_digest"]) is not str:
        raise NamedCheckReceiptError()

    if type(raw["policy_version"]) is not int or isinstance(raw["policy_version"], bool):
        raise NamedCheckReceiptError()

    exit_code = raw["exit_code"]
    if exit_code is not None and (type(exit_code) is not int or isinstance(exit_code, bool)):
        raise NamedCheckReceiptError()

    if type(raw["timed_out"]) is not bool:
        raise NamedCheckReceiptError()

    if type(raw["started_at"]) is not str:
        raise NamedCheckReceiptError()
    try:
        started_at = datetime.fromisoformat(raw["started_at"])
    except ValueError:
        raise NamedCheckReceiptError() from None
    if started_at.utcoffset() is None:
        raise NamedCheckReceiptError()

    if type(raw["duration_ms"]) is not int or isinstance(raw["duration_ms"], bool):
        raise NamedCheckReceiptError()

    if type(raw["stdout_digest"]) is not str:
        raise NamedCheckReceiptError()

    if type(raw["stderr_digest"]) is not str:
        raise NamedCheckReceiptError()

    if type(raw["runner_mode"]) is not str:
        raise NamedCheckReceiptError()
    try:
        runner_mode = RunnerMode(raw["runner_mode"])
    except ValueError:
        raise NamedCheckReceiptError() from None

    image_digest = raw["image_digest"]
    if image_digest is not None and type(image_digest) is not str:
        raise NamedCheckReceiptError()

    if type(raw["network_enabled"]) is not bool:
        raise NamedCheckReceiptError()

    if type(raw["stdout_original_byte_count"]) is not int or isinstance(
        raw["stdout_original_byte_count"], bool
    ):
        raise NamedCheckReceiptError()

    if type(raw["stderr_original_byte_count"]) is not int or isinstance(
        raw["stderr_original_byte_count"], bool
    ):
        raise NamedCheckReceiptError()

    if type(raw["stdout_truncated"]) is not bool:
        raise NamedCheckReceiptError()

    if type(raw["stderr_truncated"]) is not bool:
        raise NamedCheckReceiptError()

    if type(raw["unsandboxed"]) is not bool:
        raise NamedCheckReceiptError()

    try:
        result = CommandResult(
            command_name=raw["command_name"],
            kind=kind,
            command_digest=raw["command_digest"],
            policy_version=raw["policy_version"],
            exit_code=exit_code,
            timed_out=raw["timed_out"],
            started_at=started_at,
            duration_ms=raw["duration_ms"],
            stdout_digest=raw["stdout_digest"],
            stderr_digest=raw["stderr_digest"],
            runner_mode=runner_mode,
            image_digest=image_digest,
            network_enabled=raw["network_enabled"],
            stdout_original_byte_count=raw["stdout_original_byte_count"],
            stderr_original_byte_count=raw["stderr_original_byte_count"],
            stdout_truncated=raw["stdout_truncated"],
            stderr_truncated=raw["stderr_truncated"],
            unsandboxed=raw["unsandboxed"],
        )
    except ValueError, TypeError:
        raise NamedCheckReceiptError() from None

    reencoded = encode_command_result(result)
    if reencoded != data:
        raise NamedCheckReceiptError()

    return result


def verify_output_envelope(
    data: bytes,
    *,
    stream: Literal["stdout", "stderr"],
    result: CommandResult,
) -> Mapping[str, object]:
    """Verify and decode a persisted runner stream output envelope."""
    if stream not in ("stdout", "stderr"):
        raise NamedCheckReceiptError()

    if not isinstance(data, bytes) or len(data) == 0:
        raise NamedCheckReceiptError()

    if len(data) > _OUTPUT_ENVELOPE_MAX_BYTES:
        raise NamedCheckReceiptError()

    if not isinstance(result, CommandResult):
        raise NamedCheckReceiptError()

    expected_digest = result.stdout_digest if stream == "stdout" else result.stderr_digest
    if hashlib.sha256(data).hexdigest() != expected_digest:
        raise NamedCheckReceiptError()

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise NamedCheckReceiptError() from None

    try:
        envelope = json.loads(
            text,
            object_pairs_hook=_object_pairs_hook,
            parse_constant=_reject_constant,
        )
    except json.JSONDecodeError, ValueError, RecursionError:
        raise NamedCheckReceiptError() from None

    if not isinstance(envelope, dict):
        raise NamedCheckReceiptError()

    if frozenset(envelope.keys()) != _EXPECTED_STREAM_KEYS:
        raise NamedCheckReceiptError()

    try:
        reencoded = json.dumps(
            envelope,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except UnicodeEncodeError, OverflowError, ValueError, TypeError:
        raise NamedCheckReceiptError() from None

    if reencoded != data:
        raise NamedCheckReceiptError()

    env_stream = envelope["stream"]
    if type(env_stream) is not str or env_stream != stream:
        raise NamedCheckReceiptError()

    encoding = envelope["encoding"]
    if type(encoding) is not str or encoding != "utf-8-replacement":
        raise NamedCheckReceiptError()

    env_text = envelope["text"]
    if type(env_text) is not str:
        raise NamedCheckReceiptError()

    text_bytes = env_text.encode("utf-8")
    if len(text_bytes) > _OUTPUT_TEXT_MAX_BYTES:
        raise NamedCheckReceiptError()

    captured_byte_count = envelope["captured_byte_count"]
    if (
        type(captured_byte_count) is not int
        or isinstance(captured_byte_count, bool)
        or captured_byte_count < 0
        or captured_byte_count != len(text_bytes)
    ):
        raise NamedCheckReceiptError()

    expected_original_count = (
        result.stdout_original_byte_count
        if stream == "stdout"
        else result.stderr_original_byte_count
    )
    original_byte_count = envelope["original_byte_count"]
    if (
        type(original_byte_count) is not int
        or isinstance(original_byte_count, bool)
        or original_byte_count < 0
        or original_byte_count != expected_original_count
    ):
        raise NamedCheckReceiptError()

    truncated = envelope["truncated"]
    if type(truncated) is not bool:
        raise NamedCheckReceiptError()

    expected_truncated = result.stdout_truncated if stream == "stdout" else result.stderr_truncated
    if expected_truncated and not truncated:
        raise NamedCheckReceiptError()

    return MappingProxyType(dict(envelope))


__all__ = [
    "NamedCheckReceiptError",
    "decode_command_result",
    "encode_command_result",
    "verify_output_envelope",
]
