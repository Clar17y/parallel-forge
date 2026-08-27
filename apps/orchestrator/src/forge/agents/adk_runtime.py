"""Narrow Google ADK 2.5.0 runtime boundary for Forge agent executions."""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final, NoReturn, Protocol, cast, runtime_checkable

from google.adk.agents import LlmAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.base_toolset import BaseToolset
from google.genai import types
from pydantic import BaseModel

_PG_INT32_MAX: Final = 2_147_483_647
_MAX_INSTRUCTION_CHARS: Final = 1_000_000
_MAX_OUTPUT_CHARS: Final = 1_000_000
_MAX_PAYLOAD_BYTES: Final = 4_194_304
_MAX_TOOLS: Final = 100
_MAX_EVENTS: Final = 10_000
_MAX_PARTS_PER_EVENT: Final = 1_000
_AGENT_NAME_RE: Final = re.compile(r"\A[A-Za-z][A-Za-z0-9_.-]{0,95}\Z")
_OPAQUE_ID_RE: Final = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.:-]{0,254}\Z")
_PROVIDER_REQUEST_ID_RE: Final = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.:-]{0,254}\Z")
_STREAM_TIMED_OUT: Final = object()


class AdkRuntimeError(RuntimeError):
    """Stable, context-free failure at the raw ADK boundary."""

    def __init__(self, _detail: object = None) -> None:
        super().__init__("ADK runtime execution failed")

    def __repr__(self) -> str:
        return "AdkRuntimeError('ADK runtime execution failed')"


class AdkInvocationInvalid(ValueError):
    """Stable, context-free rejection before provider invocation."""

    def __init__(self, _detail: object = None) -> None:
        super().__init__("ADK invocation is invalid")

    def __repr__(self) -> str:
        return "AdkInvocationInvalid('ADK invocation is invalid')"


class AdkFinishReason(StrEnum):
    """Closed outcomes emitted by the raw runtime boundary."""

    COMPLETED = "completed"
    BUDGET_EXHAUSTED = "budget_exhausted"
    TIMED_OUT = "timed_out"
    FAILED = "failed"
    CANCELLED = "cancelled"


def _strict_nonnegative_int32(value: object) -> int:
    if type(value) is not int or not 0 <= value <= _PG_INT32_MAX:
        raise AdkInvocationInvalid()
    return value


def _strict_text(value: object, *, maximum: int) -> str:
    if type(value) is not str or value != value.strip() or not value or len(value) > maximum:
        raise AdkInvocationInvalid()
    return value


def _opaque_id(value: object) -> str:
    text = _strict_text(value, maximum=255)
    if _OPAQUE_ID_RE.fullmatch(text) is None:
        raise AdkInvocationInvalid()
    return text


def _reject_json_constant(_value: str) -> NoReturn:
    raise ValueError


def _canonical_json(value: object) -> str:
    if type(value) is not str:
        raise AdkInvocationInvalid()
    try:
        encoded = value.encode("utf-8")
        parsed = json.loads(value, parse_constant=_reject_json_constant)
        canonical = json.dumps(
            parsed,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError, UnicodeError) as error:
        raise AdkInvocationInvalid() from error
    if not encoded or len(encoded) > _MAX_PAYLOAD_BYTES or value != canonical:
        raise AdkInvocationInvalid()
    return value


def _checked_add(left: int, right: int) -> int:
    result = left + right
    if result > _PG_INT32_MAX:
        raise AdkRuntimeError()
    return result


def _provider_count(value: object) -> int:
    if value is None:
        return 0
    if type(value) is not int or not 0 <= value <= _PG_INT32_MAX:
        raise AdkRuntimeError()
    return value


def _usage_snapshot(usage: object) -> tuple[int, int, int]:
    if usage is None:
        return (0, 0, 0)
    input_tokens = _checked_add(
        _provider_count(getattr(usage, "prompt_token_count", None)),
        _provider_count(getattr(usage, "tool_use_prompt_token_count", None)),
    )
    output_tokens = _checked_add(
        _provider_count(getattr(usage, "candidates_token_count", None)),
        _provider_count(getattr(usage, "thoughts_token_count", None)),
    )
    cached_tokens = _provider_count(getattr(usage, "cached_content_token_count", None))
    return (input_tokens, output_tokens, cached_tokens)


def _event_cost(event: object, usage: object) -> int | None:
    for source in (event, usage):
        if source is None:
            continue
        for field_name in ("cost_minor", "estimated_cost_minor"):
            value = getattr(source, field_name, None)
            if value is not None:
                return _provider_count(value)
    metadata = getattr(event, "custom_metadata", None)
    if isinstance(metadata, Mapping):
        for field_name in ("cost_minor", "estimated_cost_minor"):
            value = metadata.get(field_name)
            if value is not None:
                return _provider_count(value)
    return None


def _provider_request_id(event: object) -> str | None:
    metadata = getattr(event, "custom_metadata", None)
    candidates: list[object] = []
    if isinstance(metadata, Mapping):
        candidates.extend(
            metadata.get(name) for name in ("provider_request_id", "request_id", "response_id")
        )
    candidates.append(getattr(event, "interaction_id", None))
    for candidate in candidates:
        if type(candidate) is str and _PROVIDER_REQUEST_ID_RE.fullmatch(candidate) is not None:
            return candidate
    return None


def _event_text(event: object, expected_author: str) -> str | None:
    if getattr(event, "author", None) != expected_author:
        return None
    content = getattr(event, "content", None)
    parts = getattr(content, "parts", None)
    if isinstance(parts, (str, bytes, bytearray)) or not isinstance(parts, Sequence):
        return None
    if len(parts) > _MAX_PARTS_PER_EVENT:
        raise AdkRuntimeError()
    chunks: list[str] = []
    length = 0
    for part in parts:
        text = getattr(part, "text", None)
        if getattr(part, "thought", False) is True or type(text) is not str or not text:
            continue
        length += len(text)
        if length > _MAX_OUTPUT_CHARS:
            raise AdkRuntimeError()
        chunks.append(text)
    joined = "".join(chunks)
    return joined if joined.strip() else None


def _event_finish_reason(event: object) -> AdkFinishReason | None:
    reason = getattr(event, "finish_reason", None)
    if reason is not None:
        normalized = str(reason).upper()
        if "MAX_TOKENS" in normalized or "TOO_MANY_TOOL_CALLS" in normalized:
            return AdkFinishReason.BUDGET_EXHAUSTED
        if "TIMEOUT" in normalized or "DEADLINE" in normalized:
            return AdkFinishReason.TIMED_OUT
        if "CANCELLED" in normalized or "CANCELED" in normalized:
            return AdkFinishReason.CANCELLED
        if any(
            marker in normalized
            for marker in ("SAFETY", "BLOCKLIST", "PROHIBITED", "ERROR", "MALFORMED")
        ):
            return AdkFinishReason.FAILED
    if getattr(event, "interrupted", False) is True:
        return AdkFinishReason.CANCELLED
    if getattr(event, "error_code", None) is not None:
        return AdkFinishReason.FAILED
    return None


def _function_call_count(event: object) -> int:
    get_calls = getattr(event, "get_function_calls", None)
    if not callable(get_calls):
        return 0
    calls = get_calls()
    if isinstance(calls, (str, bytes, bytearray)) or not isinstance(calls, Sequence):
        raise AdkRuntimeError()
    return _provider_count(len(calls))


@dataclass(frozen=True, slots=True)
class AdkUsageSummary:
    """Detached provider usage observed at the ADK boundary."""

    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    tool_call_count: int = 0
    cost_minor: int | None = None

    def __post_init__(self) -> None:
        _strict_nonnegative_int32(self.input_tokens)
        _strict_nonnegative_int32(self.output_tokens)
        _strict_nonnegative_int32(self.cached_input_tokens)
        _strict_nonnegative_int32(self.tool_call_count)
        if self.cost_minor is not None:
            _strict_nonnegative_int32(self.cost_minor)


@dataclass(frozen=True, slots=True)
class AdkInvocation:
    """Immutable inputs admitted to the raw ADK runtime."""

    agent_name: str
    model: str
    instruction: str
    output_schema: type[BaseModel]
    tools: tuple[BaseTool | BaseToolset, ...]
    user_id: str
    session_id: str
    user_payload_json: str
    max_input_tokens: int
    max_output_tokens: int
    max_tool_calls: int
    max_duration_ms: int
    max_cost_minor: int

    def __post_init__(self) -> None:
        name = _strict_text(self.agent_name, maximum=96)
        if _AGENT_NAME_RE.fullmatch(name) is None:
            raise AdkInvocationInvalid()
        _strict_text(self.model, maximum=255)
        _strict_text(self.instruction, maximum=_MAX_INSTRUCTION_CHARS)
        if not isinstance(self.output_schema, type) or not issubclass(
            self.output_schema, BaseModel
        ):
            raise AdkInvocationInvalid()
        if type(self.tools) is not tuple or len(self.tools) > _MAX_TOOLS:
            raise AdkInvocationInvalid()
        if any(not isinstance(tool, (BaseTool, BaseToolset)) for tool in self.tools):
            raise AdkInvocationInvalid()
        _opaque_id(self.user_id)
        _opaque_id(self.session_id)
        _canonical_json(self.user_payload_json)
        _strict_nonnegative_int32(self.max_input_tokens)
        _strict_nonnegative_int32(self.max_output_tokens)
        _strict_nonnegative_int32(self.max_tool_calls)
        _strict_nonnegative_int32(self.max_duration_ms)
        _strict_nonnegative_int32(self.max_cost_minor)

    def __repr__(self) -> str:
        return (
            "AdkInvocation("
            f"agent_name={self.agent_name!r}, model={self.model!r}, "
            f"tools={len(self.tools)}, has_instruction={bool(self.instruction)}, "
            f"has_payload={bool(self.user_payload_json)})"
        )


@dataclass(frozen=True, slots=True)
class AdkInvocationResult:
    """Detached, JSON-safe evidence returned by the raw runtime."""

    finish_reason: AdkFinishReason
    output_text: str | None = None
    usage: AdkUsageSummary = field(default_factory=AdkUsageSummary)
    provider_request_id: str | None = None
    duration_ms: int = 0

    def __post_init__(self) -> None:
        if type(self.finish_reason) is not AdkFinishReason:
            raise TypeError("finish_reason must be an AdkFinishReason")
        if self.output_text is not None:
            if type(self.output_text) is not str or not self.output_text.strip():
                raise TypeError("output_text must be nonblank text or None")
            if len(self.output_text) > _MAX_OUTPUT_CHARS:
                raise TypeError("output_text exceeds its bound")
        if type(self.usage) is not AdkUsageSummary:
            raise TypeError("usage must be an AdkUsageSummary")
        if self.provider_request_id is not None and (
            type(self.provider_request_id) is not str
            or _PROVIDER_REQUEST_ID_RE.fullmatch(self.provider_request_id) is None
        ):
            raise TypeError("provider_request_id is invalid")
        _strict_nonnegative_int32(self.duration_ms)

    def __repr__(self) -> str:
        return (
            "AdkInvocationResult("
            f"finish_reason={self.finish_reason.value!r}, "
            f"has_output={self.output_text is not None}, "
            f"has_provider_request_id={self.provider_request_id is not None}, "
            f"duration_ms={self.duration_ms}, usage={self.usage!r})"
        )


@runtime_checkable
class AdkRuntimeProtocol(Protocol):
    """Provider-neutral seam used by the Forge gateway and its fake."""

    async def invoke(self, request: AdkInvocation) -> AdkInvocationResult: ...


@dataclass(slots=True)
class _ObservedStream:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    tool_calls: int = 0
    cost_minor: int | None = None
    active_input_tokens: int = 0
    active_output_tokens: int = 0
    active_cached_input_tokens: int = 0
    active_cost_minor: int | None = None
    last_text: str | None = None
    provider_request_id: str | None = None
    finish_reason: AdkFinishReason = AdkFinishReason.COMPLETED

    def observe(self, event: object, *, expected_author: str) -> None:
        usage = getattr(event, "usage_metadata", None)
        if usage is not None:
            snapshot = _usage_snapshot(usage)
            self.active_input_tokens = max(self.active_input_tokens, snapshot[0])
            self.active_output_tokens = max(self.active_output_tokens, snapshot[1])
            self.active_cached_input_tokens = max(self.active_cached_input_tokens, snapshot[2])
        cost = _event_cost(event, usage)
        if cost is not None:
            self.active_cost_minor = max(self.active_cost_minor or 0, cost)
        self.tool_calls = _checked_add(self.tool_calls, _function_call_count(event))
        if self.provider_request_id is None:
            self.provider_request_id = _provider_request_id(event)
        text = _event_text(event, expected_author)
        if text is not None:
            self.last_text = text
        reason = _event_finish_reason(event)
        if reason is not None:
            self.finish_reason = reason
        if getattr(event, "partial", None) is not True:
            self.commit_active_turn()

    def commit_active_turn(self) -> None:
        self.input_tokens = _checked_add(self.input_tokens, self.active_input_tokens)
        self.output_tokens = _checked_add(self.output_tokens, self.active_output_tokens)
        self.cached_input_tokens = _checked_add(
            self.cached_input_tokens, self.active_cached_input_tokens
        )
        if self.active_cost_minor is not None:
            self.cost_minor = (
                self.active_cost_minor
                if self.cost_minor is None
                else _checked_add(self.cost_minor, self.active_cost_minor)
            )
        self.active_input_tokens = 0
        self.active_output_tokens = 0
        self.active_cached_input_tokens = 0
        self.active_cost_minor = None

    def current_usage(self) -> AdkUsageSummary:
        input_tokens = _checked_add(self.input_tokens, self.active_input_tokens)
        output_tokens = _checked_add(self.output_tokens, self.active_output_tokens)
        cached = _checked_add(self.cached_input_tokens, self.active_cached_input_tokens)
        cost = self.cost_minor
        if self.active_cost_minor is not None:
            cost = (
                self.active_cost_minor
                if cost is None
                else _checked_add(cost, self.active_cost_minor)
            )
        return AdkUsageSummary(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached,
            tool_call_count=self.tool_calls,
            cost_minor=cost,
        )


def _duration_ms(started: float) -> int:
    elapsed = int((time.monotonic() - started) * 1_000)
    if elapsed < 0 or elapsed > _PG_INT32_MAX:
        raise AdkRuntimeError()
    return elapsed


def _over_budget(usage: AdkUsageSummary, request: AdkInvocation) -> bool:
    return (
        usage.input_tokens > request.max_input_tokens
        or usage.output_tokens > request.max_output_tokens
        or usage.tool_call_count > request.max_tool_calls
        or (usage.cost_minor is not None and usage.cost_minor > request.max_cost_minor)
    )


async def _close_stream(stream: object) -> None:
    close = getattr(stream, "aclose", None)
    if not callable(close):
        return

    async def call_close() -> None:
        try:
            await close()
        except Exception:  # noqa: BLE001 - provider cleanup details are not exposed
            return

    cleanup = asyncio.create_task(call_close())
    extra_cancellations = 0
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            extra_cancellations += 1
    if extra_cancellations:
        current = asyncio.current_task()
        if current is not None:
            for _ in range(extra_cancellations):
                current.cancel()
        raise asyncio.CancelledError


async def _next_with_deadline(stream: AsyncIterator[object], *, remaining_seconds: float) -> object:
    if remaining_seconds <= 0:
        return _STREAM_TIMED_OUT
    try:
        async with asyncio.timeout(remaining_seconds):
            return await anext(stream)
    except TimeoutError:
        return _STREAM_TIMED_OUT


class AdkRuntime:
    """Concrete raw runner/session/event adapter for pinned Google ADK 2.5.0."""

    async def invoke(self, request: AdkInvocation) -> AdkInvocationResult:
        if type(request) is not AdkInvocation:
            raise TypeError("request must be an AdkInvocation")

        started = time.monotonic()
        observed = _ObservedStream()
        stream: AsyncIterator[object] | None = None
        try:
            if request.max_duration_ms == 0:
                return self._result(observed, AdkFinishReason.BUDGET_EXHAUSTED, started=started)
            agent = LlmAgent(
                name=request.agent_name,
                model=request.model,
                instruction=request.instruction,
                include_contents="none",
                output_schema=request.output_schema,
                tools=list(request.tools),
            )
            sessions = InMemorySessionService()  # type: ignore[no-untyped-call]
            runner = Runner(
                app_name="forge",
                agent=agent,
                session_service=sessions,
                auto_create_session=True,
            )
            content = types.Content(role="user", parts=[types.Part(text=request.user_payload_json)])
            stream = cast(
                AsyncIterator[object],
                runner.run_async(
                    user_id=request.user_id,
                    session_id=request.session_id,
                    new_message=content,
                ),
            )
            event_count = 0
            while True:
                remaining = request.max_duration_ms / 1_000 - (time.monotonic() - started)
                event = await _next_with_deadline(stream, remaining_seconds=remaining)
                if event is _STREAM_TIMED_OUT:
                    await _close_stream(stream)
                    return self._result(observed, AdkFinishReason.BUDGET_EXHAUSTED, started=started)
                event_count += 1
                if event_count > _MAX_EVENTS:
                    raise AdkRuntimeError()
                observed.observe(event, expected_author=request.agent_name)
                if _over_budget(observed.current_usage(), request):
                    await _close_stream(stream)
                    return self._result(observed, AdkFinishReason.BUDGET_EXHAUSTED, started=started)
        except StopAsyncIteration:
            observed.commit_active_turn()
        except asyncio.CancelledError:
            if stream is not None:
                await _close_stream(stream)
            raise
        except Exception:  # noqa: BLE001 - provider exceptions stay behind this boundary
            if stream is not None:
                await _close_stream(stream)
            return self._result(observed, AdkFinishReason.FAILED, started=started)

        if _duration_ms(started) > request.max_duration_ms:
            return self._result(observed, AdkFinishReason.BUDGET_EXHAUSTED, started=started)
        return self._result(observed, observed.finish_reason, started=started)

    @staticmethod
    def _result(
        observed: _ObservedStream,
        reason: AdkFinishReason,
        *,
        started: float,
    ) -> AdkInvocationResult:
        return AdkInvocationResult(
            finish_reason=reason,
            output_text=observed.last_text if reason is AdkFinishReason.COMPLETED else None,
            usage=observed.current_usage(),
            provider_request_id=observed.provider_request_id,
            duration_ms=_duration_ms(started),
        )


__all__ = [
    "AdkFinishReason",
    "AdkInvocation",
    "AdkInvocationInvalid",
    "AdkInvocationResult",
    "AdkRuntime",
    "AdkRuntimeError",
    "AdkRuntimeProtocol",
    "AdkUsageSummary",
]
