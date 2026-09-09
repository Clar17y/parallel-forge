"""Offline ADK regressions for stream-time cost admission."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
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
    _over_budget,
)
from forge.agents.prompt_loader import PromptLoader
from forge.domain.actor import AgentRole
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.tools.function_tool import FunctionTool
from google.genai import types
from pydantic import BaseModel


class _Output(BaseModel):
    answer: str


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
        max_duration_ms=10 if not caller_cancellation else 10_000,
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
            assert (await task).finish_reason is AdkFinishReason.BUDGET_EXHAUSTED
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
