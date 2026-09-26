"""Bounded diagnostics for the documented agy headless stream, without authority.

The init model is an echo of requested configuration. Neither it nor a successful
result proves effective authentication, billing, auxiliary models or callbacks.
Only the supervisor, never a provider event, supplies process-tree settlement.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from forge.agents.subscription_protocol import ProtocolError, freeze_context
from forge.domain.subscription_launch import SubscriptionLaunchTerminalProof

_USAGE_KEYS = frozenset(
    {"input_tokens", "output_tokens", "thinking_tokens", "cache_read_tokens", "total_tokens"}
)
_MAX_FRAMES = 4096
_MAX_BYTES = 1024 * 1024


class AntigravityStreamError(ValueError):
    """No provider-controlled text is included in this failure."""

    def __init__(self) -> None:
        super().__init__("antigravity_stream_unproved")


@dataclass(frozen=True, slots=True)
class AntigravityStreamObservation:
    observation_digest: str
    input_tokens: int
    output_tokens: int
    thinking_tokens: int
    cache_read_tokens: int
    total_tokens: int
    failure: str | None
    warnings: tuple[str, ...] = field(default=("approved_tools_unproved",), init=False)
    publishable: bool = field(default=False, init=False)
    quota_exhausted: bool = field(default=False, init=False)


def parse_antigravity_stream(
    frames: Iterable[Mapping[str, Any]],
    *,
    expected_model: str,
    expected_output: Mapping[str, object],
    output_schema: Mapping[str, object],
    terminal: SubscriptionLaunchTerminalProof,
    max_tokens: int,
    max_duration_seconds: float,
) -> AntigravityStreamObservation:
    """Check a single bounded conformance turn against its exact expected result.

    This is deliberately not a general runtime decision decoder or a callback
    dispatcher. Stream tool text is untrusted diagnostic data, never a receipt.
    Cumulative terminal usage is counted once; step usage is not added again.
    """
    if (
        type(expected_model) is not str
        or not expected_model
        or type(max_tokens) is not int
        or not 0 < max_tokens <= 10_000_000
        or type(max_duration_seconds) not in (int, float)
        or not math.isfinite(max_duration_seconds)
        or not 0 < max_duration_seconds <= 300
    ):
        raise AntigravityStreamError()
    if (
        not isinstance(terminal, SubscriptionLaunchTerminalProof)
        or not terminal.permits_decision
        or terminal.stderr_truncated
        or terminal.stdout_bytes > _MAX_BYTES
    ):
        raise AntigravityStreamError()
    schema_wire, output_wire = _wire(output_schema), _wire(expected_output)
    conversation: str | None = None
    result: Mapping[str, Any] | None = None
    total_bytes = 0
    for index, frame in enumerate(frames):
        if index >= _MAX_FRAMES:
            raise AntigravityStreamError()
        total_bytes += len(_wire(frame))
        if total_bytes > _MAX_BYTES or result is not None:
            raise AntigravityStreamError()
        event = frame.get("event")
        if event == "init" and conversation is None and index == 0:
            candidate = frame.get("conversation_id")
            init = frame.get("init")
            if (
                type(candidate) is not str
                or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", candidate) is None
                or not isinstance(init, Mapping)
                or init.get("model") != expected_model
                or _wire(init.get("json_schema")) != schema_wire
            ):
                raise AntigravityStreamError()
            conversation = candidate
        elif event in ("step_update", "result") and conversation is not None:
            payload = frame.get(event)
            if not isinstance(payload, Mapping) or payload.get("conversation_id") != conversation:
                raise AntigravityStreamError()
            if event == "result":
                result = payload
            elif (
                type(payload.get("step_index")) is not int
                or not 0 <= payload["step_index"] < _MAX_FRAMES
                or payload.get("state") not in ("ACTIVE", "DONE")
                or payload.get("step_type")
                not in ("user_input", "agent_response", "tool", "checkpoint")
            ):
                raise AntigravityStreamError()
            if event == "step_update" and "usage" in payload:
                _usage(payload["usage"], max_tokens)
        else:
            raise AntigravityStreamError()
    if result is None:
        raise AntigravityStreamError()
    status = result.get("status")
    duration = result.get("duration_seconds")
    turns = result.get("num_turns")
    if (
        status not in ("SUCCESS", "ERROR", "CANCELED", "INTERRUPTED", "INVALID")
        or type(turns) is not int
        or not 0 <= turns <= 1
        or isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or not math.isfinite(duration)
        or not 0 <= duration <= max_duration_seconds
    ):
        raise AntigravityStreamError()
    usage = _usage(result.get("usage"), max_tokens)
    failure = None
    if status == "SUCCESS":
        if (
            terminal.return_code != 0
            or turns != 1
            or _wire(result.get("json_schema")) != schema_wire
            or _wire(result.get("structured_output")) != output_wire
            or result.get("error")
        ):
            raise AntigravityStreamError()
    else:
        error = result.get("error", "")
        # agy exposes no reliable allowance-exhaustion/reset schema here. A
        # generic 429 can only indicate throttling; it never depletes a pool.
        failure = (
            "throttled"
            if status == "ERROR" and type(error) is str and re.search(r"\b429\b", error)
            else "provider_error"
        )
    digest = hashlib.sha256(
        _wire(
            {
                "requested_model": expected_model,
                "status": status,
                "usage": usage,
                "terminal": terminal.model_dump(mode="json"),
                "output_digest": hashlib.sha256(output_wire).hexdigest()
                if failure is None
                else None,
            }
        )
    ).hexdigest()
    return AntigravityStreamObservation(digest, **usage, failure=failure)


def _wire(value: object) -> bytes:
    if not isinstance(value, Mapping):
        raise AntigravityStreamError()
    try:
        freeze_context(value)
        wire = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    except TypeError, ValueError, RecursionError, ProtocolError:
        raise AntigravityStreamError() from None
    if len(wire) > 256 * 1024:
        raise AntigravityStreamError()
    return wire


def _usage(value: object, limit: int) -> dict[str, int]:
    if (
        not isinstance(value, Mapping)
        or set(value) != _USAGE_KEYS
        or any(type(item) is not int or not 0 <= item <= limit for item in value.values())
        or value["input_tokens"] + value["output_tokens"] != value["total_tokens"]
    ):
        raise AntigravityStreamError()
    return dict(value)
