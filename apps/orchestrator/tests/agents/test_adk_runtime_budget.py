"""Offline ADK regressions for stream-time cost admission."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from dataclasses import replace
from pathlib import Path
from typing import Any

import forge.agents.adk_runtime as runtime_module
import pytest
from forge.agents.adk_runtime import (
    AdkFinishReason,
    AdkInvocation,
    AdkInvocationInvalid,
    AdkRuntime,
    AdkUsageSummary,
    _ObservedStream,
    _over_budget,
)
from forge.agents.prompt_loader import PromptLoader
from forge.domain.actor import AgentRole
from google.adk.events.event import Event
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.tools.function_tool import FunctionTool
from google.genai import types
from pydantic import BaseModel


class _Output(BaseModel):
    answer: str


def test_stream_distinguishes_missing_partial_and_reported_zero_usage() -> None:
    missing = _ObservedStream()
    missing.observe(
        Event(
            author="offline_agent",
            content=types.Content(role="model", parts=[types.Part(text="answer")]),
        ),
        expected_author="offline_agent",
    )
    assert missing.current_usage().input_tokens_complete is False
    assert missing.current_usage().output_tokens_complete is False

    zero = _ObservedStream()
    zero.observe(
        LlmResponse(
            usage_metadata=types.GenerateContentResponseUsageMetadata(
                prompt_token_count=0, candidates_token_count=0, cached_content_token_count=0
            ),
            partial=False,
        ),
        expected_author="offline_agent",
    )
    assert zero.current_usage().input_tokens_complete is True
    assert zero.current_usage().output_tokens_complete is True
    assert zero.current_usage().cached_input_tokens_complete is True

    partial = _ObservedStream()
    partial.observe(
        LlmResponse(
            usage_metadata=types.GenerateContentResponseUsageMetadata(
                prompt_token_count=8,
                candidates_token_count=2,
            ),
            partial=False,
        ),
        expected_author="offline_agent",
    )
    assert partial.current_usage().input_tokens == 8
    assert partial.current_usage().input_tokens_complete is True
    assert partial.current_usage().output_tokens_complete is True
    assert partial.current_usage().cached_input_tokens_complete is False

    cache_only = _ObservedStream()
    cache_only.observe(
        LlmResponse(
            usage_metadata=types.GenerateContentResponseUsageMetadata(
                cached_content_token_count=5,
            ),
            partial=False,
        ),
        expected_author="offline_agent",
    )
    assert cache_only.current_usage().input_tokens == 5
    assert cache_only.current_usage().cached_input_tokens == 5
    assert cache_only.current_usage().input_tokens_complete is False
    assert cache_only.current_usage().cached_input_tokens_complete is True

    zero.observe(
        Event(
            author="offline_agent",
            content=types.Content(role="model", parts=[types.Part(text="next answer")]),
        ),
        expected_author="offline_agent",
    )
    assert zero.current_usage().input_tokens_complete is False
    assert zero.current_usage().output_tokens_complete is False


def test_sdk_tool_response_does_not_erase_complete_model_turns() -> None:
    observed = _ObservedStream()
    first = Event(
        author="offline_agent",
        content=types.Content(
            role="model",
            parts=[
                types.Part(
                    function_call=types.FunctionCall(id="call-1", name="controlled_effect", args={})
                )
            ],
        ),
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=7, candidates_token_count=2, cached_content_token_count=0
        ),
    )
    # ADK's installed function response builder uses the agent author, a user
    # content role, and can append display text without model usage metadata.
    tool_response = Event(
        author="offline_agent",
        content=types.Content(
            role="user",
            parts=[
                types.Part(
                    function_response=types.FunctionResponse(
                        id="call-1", name="controlled_effect", response={"ok": True}
                    )
                ),
                types.Part(text="tool result"),
            ],
        ),
    )
    last = Event(
        author="offline_agent",
        content=types.Content(role="model", parts=[types.Part(text="answer")]),
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=3, candidates_token_count=1, cached_content_token_count=0
        ),
    )
    for event in (first, tool_response, last):
        observed.observe(event, expected_author="offline_agent")
    usage = observed.current_usage()
    assert (usage.input_tokens, usage.output_tokens, usage.tool_call_count) == (10, 3, 1)
    assert (
        usage.input_tokens_complete,
        usage.output_tokens_complete,
        usage.cached_input_tokens_complete,
    ) == (True, True, True)
    assert observed.last_text == "answer"


def test_empty_control_and_missing_model_metadata_have_distinct_evidence() -> None:
    empty = _ObservedStream()
    assert (
        empty.current_usage().input_tokens_complete,
        empty.current_usage().output_tokens_complete,
        empty.current_usage().cached_input_tokens_complete,
    ) == (False, False, False)
    control = _ObservedStream()
    control.observe(
        Event(
            author="offline_agent",
            content=types.Content(role="user", parts=[types.Part(text="display only")]),
        ),
        expected_author="offline_agent",
    )
    assert control.last_text == "display only"
    assert control.current_usage().input_tokens_complete is False
    model = _ObservedStream()
    model.observe(
        Event(
            author="offline_agent",
            content=types.Content(role="model", parts=[types.Part(text="answer")]),
        ),
        expected_author="offline_agent",
    )
    assert model.current_usage().input_tokens_complete is False
    assert model.current_usage().output_tokens_complete is False


def test_model_partial_and_final_usage_is_counted_once_per_turn() -> None:
    observed = _ObservedStream()
    for partial, input_tokens, output_tokens in ((True, 2, 1), (False, 5, 3)):
        observed.observe(
            Event(
                author="offline_agent",
                partial=partial,
                content=types.Content(role="model", parts=[types.Part(text="answer")]),
                usage_metadata=types.GenerateContentResponseUsageMetadata(
                    prompt_token_count=input_tokens,
                    candidates_token_count=output_tokens,
                    cached_content_token_count=0,
                ),
            ),
            expected_author="offline_agent",
        )
    usage = observed.current_usage()
    assert (usage.input_tokens, usage.output_tokens) == (5, 3)
    assert (
        usage.input_tokens_complete,
        usage.output_tokens_complete,
        usage.cached_input_tokens_complete,
    ) == (True, True, True)


class _Resolver:
    async def resolve(self, reference: str) -> str:
        assert reference == "secret://forge/offline-budget"
        return "offline-budget-key"


class _ScriptedModel(BaseLlm):
    responses: list[LlmResponse]
    closed: asyncio.Event | None = None

    async def generate_content_async(
        self, request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse]:
        del request
        assert stream is False
        try:
            if not self.responses:
                raise AssertionError("offline model was advanced beyond its bounded script")
            yield self.responses.pop(0)
        finally:
            if self.closed is not None:
                self.closed.set()


def _usage(tokens: int) -> types.GenerateContentResponseUsageMetadata:
    return types.GenerateContentResponseUsageMetadata(prompt_token_count=tokens)


def _call(call_id: str) -> LlmResponse:
    return LlmResponse(
        content=types.Content(
            role="model",
            parts=[
                types.Part(
                    function_call=types.FunctionCall(id=call_id, name="controlled_effect", args={})
                )
            ],
        ),
        usage_metadata=_usage(1),
        partial=False,
    )


def _answer() -> LlmResponse:
    return LlmResponse(
        content=types.Content(role="model", parts=[types.Part(text='{"answer":"done"}')]),
        partial=False,
    )


def _invocation(
    tool: FunctionTool,
    *,
    max_cost_minor: int = 1,
    max_duration_ms: int = 2_000,
    cost_estimator: Any = None,
    instruction: str = "Use the supplied controlled tool once.",
) -> AdkInvocation:
    return AdkInvocation(
        agent_name="offline_agent",
        model="gemini-2.5-flash",
        instruction=instruction,
        output_schema=_Output,
        tools=(tool,),
        user_id="offline-user",
        session_id="offline-session",
        user_payload_json='{"task":"offline"}',
        max_input_tokens=1_000,
        max_output_tokens=1_000,
        max_tool_calls=10,
        max_duration_ms=max_duration_ms,
        max_cost_minor=max_cost_minor,
        cost_estimator=cost_estimator,
    )


def test_raw_adk_optional_token_caps_preserve_other_limits() -> None:
    request = replace(
        _invocation(FunctionTool(lambda: {"status": "unused"})),
        max_input_tokens=None,
        max_output_tokens=None,
        max_tool_calls=0,
    )
    assert _over_budget(AdkUsageSummary(input_tokens=1_000_000, cost_minor=0), request) is False
    assert _over_budget(
        AdkUsageSummary(input_tokens=1_000_000, tool_call_count=1, cost_minor=0), request
    )
    capped = replace(request, max_input_tokens=100)
    assert _over_budget(AdkUsageSummary(input_tokens=101, cost_minor=0), capped)


async def _run(
    monkeypatch: pytest.MonkeyPatch,
    responses: list[LlmResponse],
    effect: Any,
    closed: asyncio.Event | None = None,
    **kwargs: Any,
) -> Any:
    model = _ScriptedModel(model="offline", responses=responses, closed=closed)
    monkeypatch.setattr(runtime_module, "Gemini", lambda **_kwargs: model)
    runtime = AdkRuntime(_Resolver(), "secret://forge/offline-budget")
    return await runtime.invoke(_invocation(FunctionTool(effect), **kwargs))


class _BlockingCloseStream:
    """A stream whose close waits forever until cleanup cancellation arrives."""

    def __init__(self, *, propagate_cancellation: bool = False) -> None:
        self.started = asyncio.Event()
        self.cleanup_cancelled = asyncio.Event()
        self.release = asyncio.Event()
        self.active_closes = 0
        self.propagate_cancellation = propagate_cancellation

    def __aiter__(self) -> _BlockingCloseStream:
        return self

    async def __anext__(self) -> object:
        self.started.set()
        await self.release.wait()
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self.active_closes += 1
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cleanup_cancelled.set()
            if self.propagate_cancellation:
                raise
        finally:
            self.active_closes -= 1


def _install_blocking_stream_runner(
    monkeypatch: pytest.MonkeyPatch, stream: _BlockingCloseStream
) -> None:
    class _Runner:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def run_async(self, **_kwargs: Any) -> _BlockingCloseStream:
            return stream

    monkeypatch.setattr(runtime_module, "Runner", _Runner)


@pytest.mark.asyncio
async def test_initial_sdk_runner_error_has_unknown_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_runner(**_kwargs: Any) -> None:
        raise RuntimeError("offline runner failure")

    monkeypatch.setattr(runtime_module, "Runner", fail_runner)
    runtime = AdkRuntime(_Resolver(), "secret://forge/offline-budget")
    result = await runtime.invoke(_invocation(FunctionTool(lambda: {"status": "unused"})))
    assert result.finish_reason is AdkFinishReason.FAILED
    assert (
        result.usage.input_tokens_complete,
        result.usage.output_tokens_complete,
        result.usage.cached_input_tokens_complete,
    ) == (False, False, False)


@pytest.mark.asyncio
async def test_credential_deadline_before_model_has_unknown_usage() -> None:
    class _SlowResolver:
        async def resolve(self, _reference: str) -> str:
            await asyncio.Event().wait()
            return "unreachable"

    runtime = AdkRuntime(_SlowResolver(), "secret://forge/offline-budget")
    request = _invocation(FunctionTool(lambda: {"status": "unused"}), max_duration_ms=50)
    result = await runtime.invoke(request)
    assert result.finish_reason is AdkFinishReason.BUDGET_EXHAUSTED
    assert (
        result.usage.input_tokens_complete,
        result.usage.output_tokens_complete,
        result.usage.cached_input_tokens_complete,
    ) == (False, False, False)


@pytest.mark.asyncio
@pytest.mark.parametrize("caller_cancellation", [False, True])
@pytest.mark.parametrize("propagate_cancellation", [False, True])
async def test_actual_runtime_bounded_cleanup_cancels_blocking_close(
    monkeypatch: pytest.MonkeyPatch, caller_cancellation: bool, propagate_cancellation: bool
) -> None:
    """Deadline and caller cancellation never wait forever for cooperative stream closure."""
    stream = _BlockingCloseStream(propagate_cancellation=propagate_cancellation)
    _install_blocking_stream_runner(monkeypatch, stream)
    runtime = AdkRuntime(_Resolver(), "secret://forge/offline-budget")
    request = _invocation(
        FunctionTool(lambda: {"status": "unused"}),
        # Allow SDK setup to finish before exercising the stream deadline.
        max_duration_ms=500 if not caller_cancellation else 10_000,
    )
    task = asyncio.create_task(runtime.invoke(request))
    await asyncio.wait_for(stream.started.wait(), timeout=2)
    if caller_cancellation:
        task.cancel()

    try:
        done, _ = await asyncio.wait({task}, timeout=2)
        assert task in done
        if caller_cancellation:
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            result = await task
            assert result.finish_reason is AdkFinishReason.BUDGET_EXHAUSTED
            assert (
                result.usage.input_tokens_complete,
                result.usage.output_tokens_complete,
                result.usage.cached_input_tokens_complete,
            ) == (False, False, False)
        assert stream.cleanup_cancelled.is_set() is True
        assert stream.active_closes == 0
    finally:
        stream.release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_repeated_owner_cancellation_drains_cooperative_cleanup() -> None:
    started = asyncio.Event()
    cancelling = asyncio.Event()
    release = asyncio.Event()
    closed = asyncio.Event()

    class _Close:
        async def aclose(self) -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelling.set()
                await release.wait()
                closed.set()

    owner = asyncio.create_task(runtime_module._close_stream(_Close()))
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        owner.cancel()
        await asyncio.wait_for(cancelling.wait(), timeout=2)
        owner.cancel()
        asyncio.get_running_loop().call_soon(owner.cancel)
        release.set()
        done, _ = await asyncio.wait({owner}, timeout=2)
        assert owner in done
        with pytest.raises(asyncio.CancelledError):
            await owner
        assert closed.is_set()
        assert owner.cancelling() >= 2
    finally:
        release.set()
        if not owner.done():
            owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)


@pytest.mark.asyncio
async def test_actual_sdk_cost_estimator_allows_exact_boundary_before_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def controlled_effect() -> dict[str, str]:
        nonlocal calls
        calls += 1
        return {"status": "ok"}

    result = await _run(
        monkeypatch,
        [_call("equal-cost"), _answer()],
        controlled_effect,
        cost_estimator=lambda usage: usage.input_tokens,
    )

    assert result.finish_reason is AdkFinishReason.COMPLETED
    assert result.usage.input_tokens == 1
    assert calls == 1


@pytest.mark.asyncio
async def test_actual_sdk_tool_cycle_preserves_complete_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def controlled_effect() -> dict[str, str]:
        return {"status": "ok"}

    def metadata(prompt: int, output: int) -> types.GenerateContentResponseUsageMetadata:
        return types.GenerateContentResponseUsageMetadata(
            prompt_token_count=prompt,
            candidates_token_count=output,
            cached_content_token_count=0,
        )

    first = _call("complete-usage").model_copy(update={"usage_metadata": metadata(2, 1)})
    last = _answer().model_copy(update={"usage_metadata": metadata(3, 2)})
    result = await _run(
        monkeypatch, [first, last], controlled_effect, cost_estimator=lambda _usage: 0
    )

    assert result.finish_reason is AdkFinishReason.COMPLETED
    assert (
        result.usage.input_tokens,
        result.usage.output_tokens,
        result.usage.tool_call_count,
    ) == (5, 3, 1)
    assert (
        result.usage.input_tokens_complete,
        result.usage.output_tokens_complete,
        result.usage.cached_input_tokens_complete,
    ) == (True, True, True)


@pytest.mark.asyncio
async def test_actual_sdk_cost_estimator_stops_overshoot_before_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def controlled_effect() -> dict[str, str]:
        nonlocal calls
        calls += 1
        return {"status": "unexpected"}

    closed = asyncio.Event()
    result = await _run(
        monkeypatch,
        [_call("over-cost"), _answer()],
        controlled_effect,
        closed=closed,
        cost_estimator=lambda _usage: 2,
    )

    assert result.finish_reason is AdkFinishReason.BUDGET_EXHAUSTED
    assert result.usage.input_tokens == 1
    assert calls == 0
    assert closed.is_set() is True


@pytest.mark.asyncio
@pytest.mark.parametrize("estimator", [lambda _usage: -1, lambda _usage: "1"])
async def test_actual_sdk_invalid_estimator_fails_closed_before_tool(
    monkeypatch: pytest.MonkeyPatch, estimator: Any
) -> None:
    calls = 0

    async def controlled_effect() -> dict[str, str]:
        nonlocal calls
        calls += 1
        return {"status": "unexpected"}

    result = await _run(
        monkeypatch, [_call("invalid-cost")], controlled_effect, cost_estimator=estimator
    )

    assert result.finish_reason is AdkFinishReason.BUDGET_EXHAUSTED
    assert calls == 0


@pytest.mark.asyncio
async def test_actual_sdk_estimator_exception_fails_closed_before_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def controlled_effect() -> dict[str, str]:
        nonlocal calls
        calls += 1
        return {"status": "unexpected"}

    def failing_estimator(_usage: AdkUsageSummary) -> int:
        raise RuntimeError("untrusted pricing failure")

    result = await _run(
        monkeypatch, [_call("raised-cost")], controlled_effect, cost_estimator=failing_estimator
    )

    assert result.finish_reason is AdkFinishReason.BUDGET_EXHAUSTED
    assert calls == 0


@pytest.mark.asyncio
async def test_actual_sdk_absent_cost_with_measured_tokens_fails_closed_before_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def controlled_effect() -> dict[str, str]:
        nonlocal calls
        calls += 1
        return {"status": "unexpected"}

    result = await _run(monkeypatch, [_call("unknown-cost")], controlled_effect)

    assert result.finish_reason is AdkFinishReason.BUDGET_EXHAUSTED
    assert calls == 0


@pytest.mark.asyncio
async def test_actual_sdk_estimator_uses_cumulative_usage_before_second_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def controlled_effect() -> dict[str, str]:
        nonlocal calls
        calls += 1
        return {"status": "ok"}

    result = await _run(
        monkeypatch,
        [_call("first"), _call("second"), _answer()],
        controlled_effect,
        cost_estimator=lambda usage: usage.input_tokens,
    )

    assert result.finish_reason is AdkFinishReason.BUDGET_EXHAUSTED
    assert result.usage.input_tokens == 2
    assert calls == 1


def test_provider_cost_is_an_additional_conservative_budget_input() -> None:
    """A lower catalog estimate cannot mask a higher provider-reported cost."""

    async def controlled_effect() -> dict[str, str]:
        return {"status": "unused"}

    request = _invocation(
        FunctionTool(controlled_effect),
        cost_estimator=lambda _usage: 1,
    )

    assert _over_budget(AdkUsageSummary(input_tokens=1, cost_minor=2), request) is True


@pytest.mark.parametrize("instruction", ["", " \t\n"])
def test_instruction_must_be_nonblank_without_rewriting_edge_whitespace(instruction: str) -> None:
    async def controlled_effect() -> dict[str, str]:
        return {"status": "unused"}

    with pytest.raises(AdkInvocationInvalid):
        _invocation(FunctionTool(controlled_effect), instruction=instruction)


@pytest.mark.asyncio
async def test_actual_loaded_prompt_with_trailing_newline_runs_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompt = PromptLoader(Path(__file__).resolve().parents[4] / "agents").load(AgentRole.PLANNER)

    async def controlled_effect() -> dict[str, str]:
        return {"status": "unexpected"}

    result = await _run(
        monkeypatch,
        [_answer()],
        controlled_effect,
        instruction=prompt.instruction,
    )

    assert prompt.instruction.endswith("\n")
    assert result.finish_reason is AdkFinishReason.COMPLETED
