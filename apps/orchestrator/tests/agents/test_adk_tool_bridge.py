"""Contract tests for ADK controlled tool bridge functions and role authority boundaries."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from forge.agents.tool_bridge import _SAFE_FAILURE_MESSAGE, build_adk_tools
from forge.application.services.tools import CapabilityMatrix, ControlledToolService
from forge.domain.actor import AgentRole
from forge.domain.tool import (
    ToolAuthorizationContext,
    ToolCallStatus,
    ToolError,
    ToolErrorCode,
    ToolName,
    ToolRequest,
    ToolResult,
)
from google.adk.tools.function_tool import FunctionTool

RUN_ID = UUID("11111111-1111-4111-8111-111111111111")
EXECUTION_ID = UUID("22222222-2222-4222-8222-222222222222")
STEP_ID = UUID("33333333-3333-4333-8333-333333333333")

_RELEASE_KEYWORDS = frozenset(
    {"release", "deploy", "publish", "promote", "rollback", "secret", "token", "credential"}
)


class _RecordingControlledToolService(ControlledToolService):
    """Stub service recording contexts and requests, with configurable response/behavior."""

    def __init__(
        self,
        *,
        side_effect: BaseException | None = None,
        tool_result: ToolResult | None = None,
    ) -> None:
        self.contexts: list[ToolAuthorizationContext] = []
        self.requests: list[ToolRequest] = []
        self.side_effect = side_effect
        self.tool_result = tool_result

    async def invoke(
        self,
        context: ToolAuthorizationContext,
        request: ToolRequest,
    ) -> ToolResult:
        self.contexts.append(context)
        self.requests.append(request)
        if self.side_effect is not None:
            raise self.side_effect
        if self.tool_result is not None:
            return self.tool_result
        return ToolResult(
            tool_name=request.name,
            status=ToolCallStatus.SUCCEEDED,
            agent_execution_id=context.agent_execution_id,
            step_id=context.step_id,
            metadata={"recorded": True},
        )


def _context(
    *, role: AgentRole = AgentRole.PLANNER, **overrides: object
) -> ToolAuthorizationContext:
    values: dict[str, object] = {
        "role": role,
        "run_id": RUN_ID,
        "worktree_id": "forge-wt-tool-bridge",
        "policy_version": 1,
        "agent_execution_id": EXECUTION_ID,
        "step_id": STEP_ID,
    }
    values.update(overrides)
    return ToolAuthorizationContext(**values)  # type: ignore[arg-type]


def _sdk_context(
    invocation_id: object = "inv-smoke",
    function_call_id: object = "call-smoke",
) -> SimpleNamespace:
    return SimpleNamespace(invocation_id=invocation_id, function_call_id=function_call_id)


def test_planner_receives_only_planner_tools_and_no_release_symbols() -> None:
    """Planner receives only read tools; no write, commit, check, or release capabilities."""
    service = _RecordingControlledToolService()
    tools = build_adk_tools(service, _context(role=AgentRole.PLANNER))

    tool_names = {tool.name for tool in tools}
    expected_names = {
        ToolName.REPOSITORY_LIST_FILES.value,
        ToolName.REPOSITORY_READ_FILE.value,
        ToolName.REPOSITORY_SEARCH.value,
        ToolName.REPOSITORY_READ_INSTRUCTIONS.value,
    }
    assert tool_names == expected_names

    # Assert Planner does not receive write or mutation tools
    forbidden_names = {
        ToolName.REPOSITORY_WRITE_FILE.value,
        ToolName.GIT_COMMIT.value,
        ToolName.BUILD_RUN_NAMED_CHECK.value,
        ToolName.GIT_STATUS.value,
        ToolName.GIT_DIFF.value,
        ToolName.VALIDATION_RESULTS_READ.value,
        ToolName.REVIEW_ARTIFACTS_READ.value,
    }
    assert tool_names.isdisjoint(forbidden_names)

    # Assert no release controller, deploy, or credential symbols
    for tool in tools:
        assert isinstance(tool, FunctionTool)
        lower_name = tool.name.lower()
        lower_doc = (tool.description or "").lower()
        for keyword in _RELEASE_KEYWORDS:
            assert keyword not in lower_name, f"Forbidden keyword {keyword!r} in {tool.name}"
            assert keyword not in lower_doc, f"Forbidden keyword {keyword!r} in {tool.name} doc"


def test_developer_receives_developer_tools_without_review_or_release_symbols() -> None:
    """Developer receives write/check/commit tools but no review-evidence or release tools."""
    service = _RecordingControlledToolService()
    tools = build_adk_tools(service, _context(role=AgentRole.DEVELOPER))

    tool_names = {tool.name for tool in tools}
    expected_names = {
        ToolName.REPOSITORY_LIST_FILES.value,
        ToolName.REPOSITORY_READ_FILE.value,
        ToolName.REPOSITORY_SEARCH.value,
        ToolName.REPOSITORY_READ_INSTRUCTIONS.value,
        ToolName.REPOSITORY_WRITE_FILE.value,
        ToolName.GIT_STATUS.value,
        ToolName.GIT_DIFF.value,
        ToolName.GIT_COMMIT.value,
        ToolName.BUILD_RUN_NAMED_CHECK.value,
    }
    assert tool_names == expected_names
    assert ToolName.VALIDATION_RESULTS_READ.value not in tool_names
    assert ToolName.REVIEW_ARTIFACTS_READ.value not in tool_names

    for tool in tools:
        lower_name = tool.name.lower()
        for keyword in _RELEASE_KEYWORDS:
            assert keyword not in lower_name


def test_reviewer_receives_reviewer_tools_without_write_or_release_symbols() -> None:
    """Reviewer receives evidence/status tools but no write/commit or release tools."""
    service = _RecordingControlledToolService()
    tools = build_adk_tools(service, _context(role=AgentRole.REVIEWER))

    tool_names = {tool.name for tool in tools}
    expected_names = {
        ToolName.REPOSITORY_LIST_FILES.value,
        ToolName.REPOSITORY_READ_FILE.value,
        ToolName.REPOSITORY_SEARCH.value,
        ToolName.REPOSITORY_READ_INSTRUCTIONS.value,
        ToolName.GIT_STATUS.value,
        ToolName.GIT_DIFF.value,
        ToolName.VALIDATION_RESULTS_READ.value,
        ToolName.REVIEW_ARTIFACTS_READ.value,
    }
    assert tool_names == expected_names
    assert ToolName.REPOSITORY_WRITE_FILE.value not in tool_names
    assert ToolName.GIT_COMMIT.value not in tool_names
    assert ToolName.BUILD_RUN_NAMED_CHECK.value not in tool_names

    for tool in tools:
        lower_name = tool.name.lower()
        for keyword in _RELEASE_KEYWORDS:
            assert keyword not in lower_name


def test_all_roles_strictly_match_capability_matrix() -> None:
    """All roles configured in CapabilityMatrix match tool bridge output."""
    matrix = CapabilityMatrix()
    service = _RecordingControlledToolService()
    for role in AgentRole:
        tools = build_adk_tools(service, _context(role=role))
        tool_enum_set = {ToolName(tool.name) for tool in tools}
        assert tool_enum_set == matrix.capabilities_for(role)


def test_git_diff_bridge_selects_candidate_without_exposing_refs() -> None:
    service = _RecordingControlledToolService()
    tools = {t.name: t for t in build_adk_tools(service, _context(role=AgentRole.DEVELOPER))}
    diff = tools[ToolName.GIT_DIFF.value]
    asyncio.run(diff.func(tool_context=_sdk_context(), scope="candidate"))
    assert service.requests[-1].arguments == {"scope": "candidate"}
    asyncio.run(diff.func(tool_context=_sdk_context()))
    assert service.requests[-1].arguments == {}


def test_cannot_override_forge_identity_via_tool_invocation() -> None:
    """Invoking a bridge function retains Forge authorization context identity."""
    service = _RecordingControlledToolService()
    base_context = _context(role=AgentRole.PLANNER)
    tools = {t.name: t for t in build_adk_tools(service, base_context)}

    tool = tools[ToolName.REPOSITORY_READ_FILE.value]
    asyncio.run(
        tool.func(
            path="safe.py",
            tool_context=_sdk_context("inv-42", "call-99"),
        )
    )

    assert len(service.contexts) == 1
    call_ctx = service.contexts[0]
    assert call_ctx.role == AgentRole.PLANNER
    assert call_ctx.run_id == RUN_ID
    assert call_ctx.worktree_id == "forge-wt-tool-bridge"
    assert call_ctx.policy_version == 1
    assert call_ctx.agent_execution_id == EXECUTION_ID
    assert call_ctx.step_id == STEP_ID
    assert call_ctx.invocation_id is not None


def test_planner_tool_functions_dispatch_correct_requests() -> None:
    """Each Planner tool correctly packages its request arguments to the service."""
    service = _RecordingControlledToolService()
    tools = {t.name: t for t in build_adk_tools(service, _context(role=AgentRole.PLANNER))}
    sdk = _sdk_context()

    asyncio.run(tools[ToolName.REPOSITORY_LIST_FILES.value].func(path="sub", tool_context=sdk))
    asyncio.run(tools[ToolName.REPOSITORY_READ_FILE.value].func(path="doc.md", tool_context=sdk))
    asyncio.run(
        tools[ToolName.REPOSITORY_SEARCH.value].func(literal="query", path="sub", tool_context=sdk)
    )
    asyncio.run(
        tools[ToolName.REPOSITORY_READ_INSTRUCTIONS.value].func(target_path="sub", tool_context=sdk)
    )

    assert len(service.requests) == 4
    assert service.requests[0] == ToolRequest(
        name=ToolName.REPOSITORY_LIST_FILES, arguments={"path": "sub"}
    )
    assert service.requests[1] == ToolRequest(
        name=ToolName.REPOSITORY_READ_FILE, arguments={"path": "doc.md"}
    )
    assert service.requests[2] == ToolRequest(
        name=ToolName.REPOSITORY_SEARCH, arguments={"literal": "query", "path": "sub"}
    )
    assert service.requests[3] == ToolRequest(
        name=ToolName.REPOSITORY_READ_INSTRUCTIONS, arguments={"target_path": "sub"}
    )


def test_service_exception_returns_stable_safe_error_and_no_leak() -> None:
    """Service exceptions return safe error codes without leaking exception or credential details."""
    secret_leak = "postgres://forge_user:super_secret_password_12345@db.internal:5432/forge"
    service = _RecordingControlledToolService(
        side_effect=RuntimeError(f"Database crash: {secret_leak}")
    )
    tools = {t.name: t for t in build_adk_tools(service, _context(role=AgentRole.PLANNER))}

    result: dict[str, Any] = asyncio.run(
        tools[ToolName.REPOSITORY_READ_FILE.value].func(
            path="file.txt",
            tool_context=_sdk_context(),
        )
    )

    assert result["status"] == ToolCallStatus.FAILED.value
    assert result["error"] == {
        "code": ToolErrorCode.OPERATION_ERROR.value,
        "message": _SAFE_FAILURE_MESSAGE,
    }
    serialized = str(result)
    assert "super_secret_password" not in serialized
    assert "Database crash" not in serialized
    assert "RuntimeError" not in serialized


def test_service_cancellation_propagates_unswallowed() -> None:
    """CancelledError from service is re-raised and never caught as a safe failure."""
    service = _RecordingControlledToolService(side_effect=asyncio.CancelledError())
    tools = {t.name: t for t in build_adk_tools(service, _context(role=AgentRole.PLANNER))}

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            tools[ToolName.REPOSITORY_READ_FILE.value].func(
                path="file.txt",
                tool_context=_sdk_context(),
            )
        )


def test_service_error_result_is_safely_structured() -> None:
    """Service returning ToolResult with error is structured cleanly."""
    service = _RecordingControlledToolService(
        tool_result=ToolResult(
            tool_name=ToolName.REPOSITORY_READ_FILE,
            status=ToolCallStatus.FAILED,
            error=ToolError(
                code=ToolErrorCode.OPERATION_ERROR,
                message="file not found in worktree",
            ),
        )
    )
    tools = {t.name: t for t in build_adk_tools(service, _context(role=AgentRole.PLANNER))}

    result: dict[str, Any] = asyncio.run(
        tools[ToolName.REPOSITORY_READ_FILE.value].func(
            path="missing.txt",
            tool_context=_sdk_context(),
        )
    )

    assert result["status"] == ToolCallStatus.FAILED.value
    assert result["error"] == {
        "code": ToolErrorCode.OPERATION_ERROR.value,
        "message": "file not found in worktree",
    }


def test_build_adk_tools_rejects_invalid_service_or_context() -> None:
    """build_adk_tools strictly enforces types for service and context."""
    service = _RecordingControlledToolService()
    valid_context = _context()

    with pytest.raises(TypeError, match="ControlledToolService"):
        build_adk_tools("invalid_service", valid_context)  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="Forge tool authority"):
        build_adk_tools(service, "invalid_context")  # type: ignore[arg-type]
