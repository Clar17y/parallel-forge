"""Invocation-correlation boundaries at the ADK tool bridge."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import UUID

import pytest
from forge.agents.tool_bridge import _derive_invocation_id, build_adk_tools
from forge.application.services.tools import ControlledToolService
from forge.domain.actor import AgentRole
from forge.domain.tool import ToolAuthorizationContext, ToolCallStatus, ToolName, ToolResult

RUN_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
EXECUTION_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
STEP_ID = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")


class _RecordingService(ControlledToolService):
    def __init__(self) -> None:
        self.contexts: list[ToolAuthorizationContext] = []

    async def invoke(self, context: ToolAuthorizationContext, request: object) -> ToolResult:
        self.contexts.append(context)
        return ToolResult(tool_name=request.name, status=ToolCallStatus.SUCCEEDED)  # type: ignore[union-attr]


def _context(**updates: object) -> ToolAuthorizationContext:
    values: dict[str, object] = {
        "role": AgentRole.DEVELOPER,
        "run_id": RUN_ID,
        "worktree_id": "forge-aaaaaaaaaaaa-bbbbbbbbbbbb",
        "policy_version": 1,
        "agent_execution_id": EXECUTION_ID,
        "step_id": STEP_ID,
    }
    values.update(updates)
    return ToolAuthorizationContext(**values)  # type: ignore[arg-type]


def _sdk_context(invocation_id: object, function_call_id: object) -> SimpleNamespace:
    return SimpleNamespace(invocation_id=invocation_id, function_call_id=function_call_id)


def test_v1_derivation_is_stable_and_excludes_agent_selected_call_details() -> None:
    first = _derive_invocation_id(_context(), _sdk_context("inv-1", "call-1"))
    second = _derive_invocation_id(_context(policy_version=99), _sdk_context("inv-1", "call-1"))

    assert first == UUID("dd92e2d0-3881-5132-b705-4a29b81b08bc")
    assert second == first
    assert _derive_invocation_id(_context(), _sdk_context("inv-1", "call-2")) != first
    assert (
        _derive_invocation_id(
            _context(step_id=UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")),
            _sdk_context("inv-1", "call-1"),
        )
        != first
    )


def test_derivation_rejects_untrusted_or_incomplete_sdk_correlation() -> None:
    assert _derive_invocation_id(_context(), _sdk_context("", "call")) is None
    assert _derive_invocation_id(_context(), _sdk_context("inv\n", "call")) is None
    assert _derive_invocation_id(_context(), _sdk_context("x" * 256, "call")) is None
    assert (
        _derive_invocation_id(_context(agent_execution_id=None), _sdk_context("inv", "call"))
        is None
    )


def test_derivation_namespace_includes_every_trusted_execution_identity() -> None:
    sdk = _sdk_context("inv-1", "call-1")
    baseline = _derive_invocation_id(_context(), sdk)

    assert (
        _derive_invocation_id(_context(run_id=UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee")), sdk)
        != baseline
    )
    assert (
        _derive_invocation_id(
            _context(agent_execution_id=UUID("ffffffff-ffff-4fff-8fff-ffffffffffff")), sdk
        )
        != baseline
    )
    assert (
        _derive_invocation_id(_context(step_id=UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")), sdk)
        != baseline
    )


@pytest.mark.parametrize("bad_id", [None, "", " leading", "trailing ", "line\nfeed", "x" * 256])
async def test_effects_reject_missing_or_malformed_sdk_ids_without_service_calls(
    bad_id: object,
) -> None:
    service = _RecordingService()
    write = {tool.name: tool for tool in build_adk_tools(service, _context())}[
        ToolName.REPOSITORY_WRITE_FILE.value
    ]

    result = await write.func(
        path="result.txt", content="canary-content", tool_context=_sdk_context("inv", bad_id)
    )

    assert result == {
        "agent_execution_id": None,
        "artifact_digests": [],
        "correlation_id": None,
        "duration_ms": 0,
        "error": {
            "code": "operation_error",
            "message": "controlled tool invocation failed",
        },
        "metadata": {},
        "operation_intent_id": None,
        "status": "failed",
        "step_id": None,
        "tool_call_id": None,
        "tool_name": "repository.write_file",
    }
    assert service.contexts == []


async def test_sdk_context_getter_failure_is_safely_contained_before_any_effect() -> None:
    class HostileToolContext:
        @property
        def invocation_id(self) -> str:
            raise RuntimeError("secret-canary-from-sdk-context")

        function_call_id = "call-1"

    service = _RecordingService()
    write = {tool.name: tool for tool in build_adk_tools(service, _context())}[
        ToolName.REPOSITORY_WRITE_FILE.value
    ]

    result = await write.func(
        path="result.txt", content="canary-content", tool_context=HostileToolContext()
    )

    assert result["status"] == ToolCallStatus.FAILED.value
    assert result["error"] == {
        "code": "operation_error",
        "message": "controlled tool invocation failed",
    }
    assert "secret-canary" not in repr(result)
    assert service.contexts == []


async def test_same_sdk_identity_keeps_uuid_when_tool_or_arguments_change() -> None:
    service = _RecordingService()
    tools = {tool.name: tool for tool in build_adk_tools(service, _context())}
    sdk = _sdk_context("inv-1", "call-1")

    await tools[ToolName.REPOSITORY_WRITE_FILE.value].func(
        path="first.txt", content="first", tool_context=sdk
    )
    await tools[ToolName.GIT_COMMIT.value].func(message="different request", tool_context=sdk)

    assert len(service.contexts) == 2
    assert service.contexts[0].invocation_id == service.contexts[1].invocation_id


async def test_bridge_uses_fresh_contexts_and_denies_effect_without_correlation() -> None:
    service = _RecordingService()
    context = _context()
    tools = {tool.name: tool for tool in build_adk_tools(service, context)}
    write = tools[ToolName.REPOSITORY_WRITE_FILE.value]
    status = tools[ToolName.GIT_STATUS.value]

    denied = await write.func(
        path="result.txt", content="ok", tool_context=_sdk_context(None, None)
    )
    first, second = await asyncio.gather(
        status.func(tool_context=_sdk_context("inv-1", "call-1")),
        status.func(tool_context=_sdk_context("inv-1", "call-2")),
    )

    assert denied["status"] == ToolCallStatus.FAILED.value
    assert len(service.contexts) == 2
    assert context.invocation_id is None
    assert service.contexts[0] is not context
    assert service.contexts[0].invocation_id != service.contexts[1].invocation_id
    assert first["status"] == second["status"] == ToolCallStatus.SUCCEEDED.value


async def test_function_schema_hides_sdk_context_and_forge_identity() -> None:
    service = _RecordingService()
    tools = {tool.name: tool for tool in build_adk_tools(service, _context())}
    declaration = tools[ToolName.REPOSITORY_WRITE_FILE.value]._get_declaration()

    assert declaration is not None
    schema = declaration.parameters_json_schema
    assert set(schema["properties"]) == {"content", "path"}
    assert "tool_context" not in schema["properties"]
    assert "invocation_id" not in schema["properties"]
