"""Pinned ADK event-ordering proof for Forge's stream boundary."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator

import forge.agents.adk_runtime as adk_runtime_module
import pytest
from forge.agents.adk_runtime import (
    AdkFinishReason,
    AdkInvocation,
    AdkRuntime,
    AdkRuntimeError,
    _has_duplicate_function_call_ids,
)
from google.adk.agents import LlmAgent
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools.function_tool import FunctionTool
from google.genai import types
from pydantic import BaseModel


class _DuplicateCallModel(BaseLlm):
    """Offline model that emits two finalized calls with one provider ID."""

    closed: asyncio.Event

    async def generate_content_async(
        self, _request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse]:
        assert stream is False
        try:
            yield LlmResponse(
                content=types.Content(
                    role="model",
                    parts=[
                        types.Part(
                            function_call=types.FunctionCall(
                                id="duplicate-call", name="controlled_effect", args={}
                            )
                        ),
                        types.Part(
                            function_call=types.FunctionCall(
                                id="duplicate-call", name="controlled_effect", args={}
                            )
                        ),
                    ],
                ),
                partial=False,
            )
        finally:
            self.closed.set()


class _Output(BaseModel):
    answer: str


class _CredentialResolver:
    async def resolve(self, reference: str) -> str:
        assert reference == "secret://forge/offline_test"
        return "offline-test-key"


class _ScriptedModel(BaseLlm):
    responses: list[LlmResponse]

    async def generate_content_async(
        self, _request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse]:
        assert stream is False
        if not self.responses:
            raise AssertionError("offline model was advanced beyond its bounded script")
        while self.responses:
            response = self.responses.pop(0)
            yield response
            if response.partial is not True:
                return


def _call(call_id: str, *, name: str = "controlled_effect") -> types.Part:
    return types.Part(function_call=types.FunctionCall(id=call_id, name=name, args={}))


def _response(*parts: types.Part, partial: bool = False) -> LlmResponse:
    return LlmResponse(content=types.Content(role="model", parts=list(parts)), partial=partial)


def _request(tool: FunctionTool) -> AdkInvocation:
    return AdkInvocation(
        agent_name="offline_agent",
        model="gemini-2.5-flash",
        instruction="Use the supplied controlled tool once.",
        output_schema=_Output,
        tools=(tool,),
        user_id="offline-user",
        session_id="offline-session",
        user_payload_json='{"task":"offline"}',
        max_input_tokens=1_000,
        max_output_tokens=1_000,
        max_tool_calls=10,
        max_duration_ms=2_000,
        max_cost_minor=1_000,
    )


async def _invoke_script(
    monkeypatch: pytest.MonkeyPatch,
    responses: list[LlmResponse],
    effect: object,
) -> object:
    model = _ScriptedModel(model="offline", responses=responses)
    monkeypatch.setattr(adk_runtime_module, "Gemini", lambda **_kwargs: model)
    runtime = AdkRuntime(_CredentialResolver(), "secret://forge/offline_test")
    return await asyncio.wait_for(
        runtime.invoke(_request(FunctionTool(effect))),  # type: ignore[arg-type]
        timeout=3,
    )


async def test_runner_yields_finalized_duplicate_calls_before_any_tool_callback() -> None:
    """The public Runner iterator gives Forge a pre-effect rejection point."""

    calls = 0

    async def controlled_effect() -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"status": "unexpected"}

    closed = asyncio.Event()
    agent = LlmAgent(
        name="offline_agent",
        model=_DuplicateCallModel(model="offline", closed=closed),
        tools=[FunctionTool(controlled_effect)],
    )
    runner = Runner(
        app_name="forge-ordering-probe",
        agent=agent,
        session_service=InMemorySessionService(),
        auto_create_session=True,
    )
    stream = runner.run_async(
        user_id="offline-user",
        session_id="offline-session",
        invocation_id="offline-invocation",
        new_message=types.Content(role="user", parts=[types.Part(text="run")]),
    )

    event = await anext(stream)
    duplicate = _has_duplicate_function_call_ids(event)
    await stream.aclose()

    assert [call.id for call in event.get_function_calls()] == ["duplicate-call", "duplicate-call"]
    assert duplicate is True
    assert calls == 0
    await asyncio.wait_for(closed.wait(), timeout=1)


async def test_actual_runtime_allows_one_valid_finalized_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def controlled_effect() -> dict[str, str]:
        nonlocal calls
        calls += 1
        return {"status": "ok"}

    result = await _invoke_script(
        monkeypatch,
        [
            _response(_call("valid-call")),
            _response(types.Part(text='{"answer":"done"}')),
        ],
        controlled_effect,
    )

    assert result.finish_reason is AdkFinishReason.COMPLETED
    assert result.output_text == '{"answer":"done"}'
    assert calls == 1


@pytest.mark.parametrize("invalid_id", ["", "x" * 256])
async def test_actual_runtime_rejects_entire_mixed_invalid_id_batch_before_effects(
    monkeypatch: pytest.MonkeyPatch,
    invalid_id: str,
) -> None:
    calls = 0

    async def controlled_effect() -> dict[str, str]:
        nonlocal calls
        calls += 1
        return {"status": "unexpected"}

    with pytest.raises(AdkRuntimeError):
        await _invoke_script(
            monkeypatch,
            [
                _response(_call("valid-call"), _call(invalid_id)),
                _response(types.Part(text='{"answer":"unexpected"}')),
            ],
            controlled_effect,
        )

    assert calls == 0


async def test_actual_runtime_ignores_duplicate_ids_in_partial_fragments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def controlled_effect() -> dict[str, str]:
        nonlocal calls
        calls += 1
        return {"status": "ok"}

    result = await _invoke_script(
        monkeypatch,
        [
            _response(_call("fragment"), _call("fragment"), partial=True),
            _response(_call("final-call")),
            _response(types.Part(text='{"answer":"done"}')),
        ],
        controlled_effect,
    )

    assert result.finish_reason is AdkFinishReason.COMPLETED
    assert calls == 1
