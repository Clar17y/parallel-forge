"""Evaluation bindings must retain the production controlled-tool bridge."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import UUID

import pytest
from forge.application.services.tools import CapabilityMatrix, ControlledToolService
from forge.domain.actor import AgentRole
from forge.domain.tool import (
    ToolAuthorizationContext,
    ToolCallStatus,
    ToolName,
    ToolRequest,
    ToolResult,
)
from forge.worker.evaluation_tools import ControlledEvaluationToolProvider

from apps.orchestrator.tests.agents.test_contracts import build_agent_request


class _RecordingService(ControlledToolService):
    """Controlled boundary double proving the wrapper delegates real invocations."""

    def __init__(self) -> None:
        self.requests: list[ToolRequest] = []

    async def invoke(self, context: ToolAuthorizationContext, request: ToolRequest) -> ToolResult:
        self.requests.append(request)
        return ToolResult(
            tool_name=request.name,
            status=ToolCallStatus.SUCCEEDED,
            agent_execution_id=context.agent_execution_id,
            step_id=context.step_id,
            metadata={"changed_paths": ["app.py"]},
        )


class _Observer:
    def __init__(self) -> None:
        self.results: list[tuple[ToolName, dict[str, object]]] = []
        self.trust_observations: list[bool] = []

    def check_harness_is_intact(self, command_name: str) -> bool:
        return False

    async def record_check_artifacts(
        self, result: dict[str, object], *, command_name: str, harness_trusted: bool = True
    ) -> None:
        self.trust_observations.append(harness_trusted)

    def record_controlled_result(
        self,
        name: ToolName,
        result: dict[str, object],
        *,
        arguments: dict[str, object] | None = None,
    ) -> None:
        self.results.append((name, result))


@pytest.mark.parametrize(
    ("role", "tool_name", "arguments", "forbidden"),
    [
        (
            AgentRole.PLANNER,
            ToolName.REPOSITORY_LIST_FILES,
            {"path": "."},
            ToolName.REPOSITORY_WRITE_FILE,
        ),
        (
            AgentRole.DEVELOPER,
            ToolName.REPOSITORY_WRITE_FILE,
            {"path": "app.py", "content": "updated"},
            ToolName.REVIEW_ARTIFACTS_READ,
        ),
        (AgentRole.REVIEWER, ToolName.GIT_DIFF, {}, ToolName.REPOSITORY_WRITE_FILE),
    ],
)
def test_evaluation_provider_binds_each_role_to_controlled_adk_tools(
    role: AgentRole,
    tool_name: ToolName,
    arguments: dict[str, str],
    forbidden: ToolName,
) -> None:
    """A mocked ADK call crosses the real controlled-service boundary per role."""

    request = build_agent_request(
        role=role,
        provider="google",
        model="test-model",
        allowed_tools=tuple(
            tool for tool in ToolName if tool in CapabilityMatrix().capabilities_for(role)
        ),
    )
    service = _RecordingService()
    observer = _Observer()
    context = ToolAuthorizationContext(
        role=role,
        run_id=request.run_id,
        worktree_id="forge-evaluation-tools",
        policy_version=1,
        agent_execution_id=request.execution_id,
        step_id=UUID("33333333-3333-4333-8333-333333333333"),
    )
    provider = ControlledEvaluationToolProvider(service, context, observer, request)
    tools = {tool.name: tool for tool in provider.tools_for(request).tools}

    result = asyncio.run(
        tools[tool_name.value].run_async(
            args=arguments,
            tool_context=SimpleNamespace(invocation_id="eval", function_call_id="call"),
        )
    )

    assert service.requests == [ToolRequest(name=tool_name, arguments=arguments)]
    assert result["status"] == ToolCallStatus.SUCCEEDED.value
    assert observer.results == [(tool_name, result)]
    assert forbidden.value not in tools


def test_evaluation_provider_invokes_bound_controlled_write_and_records_receipt() -> None:
    request = build_agent_request(
        role=AgentRole.DEVELOPER,
        provider="google",
        model="test-model",
        allowed_tools=tuple(
            tool
            for tool in ToolName
            if tool in CapabilityMatrix().capabilities_for(AgentRole.DEVELOPER)
        ),
    )
    service = _RecordingService()
    observer = _Observer()
    context = ToolAuthorizationContext(
        role=AgentRole.DEVELOPER,
        run_id=request.run_id,
        worktree_id="forge-evaluation-tools",
        policy_version=1,
        agent_execution_id=request.execution_id,
        step_id=UUID("33333333-3333-4333-8333-333333333333"),
    )
    provider = ControlledEvaluationToolProvider(service, context, observer, request)

    tools = {tool.name: tool for tool in provider.tools_for(request).tools}
    result = asyncio.run(
        tools[ToolName.REPOSITORY_WRITE_FILE.value].run_async(
            args={"path": "app.py", "content": "updated"},
            tool_context=SimpleNamespace(invocation_id="eval", function_call_id="write"),
        )
    )

    assert service.requests == [
        ToolRequest(
            name=ToolName.REPOSITORY_WRITE_FILE,
            arguments={"path": "app.py", "content": "updated"},
        )
    ]
    assert result["status"] == ToolCallStatus.SUCCEEDED.value
    assert observer.results[0][0] is ToolName.REPOSITORY_WRITE_FILE


def test_evaluation_provider_removes_an_admitted_prohibited_role_tool() -> None:
    """A case restriction changes the actual ADK-visible tool set."""
    role = AgentRole.DEVELOPER
    request = build_agent_request(
        role=role,
        provider="google",
        model="test-model",
        allowed_tools=tuple(
            tool
            for tool in ToolName
            if tool in CapabilityMatrix().capabilities_for(role) and tool is not ToolName.GIT_COMMIT
        ),
    )
    context = ToolAuthorizationContext(
        role=role,
        run_id=request.run_id,
        worktree_id="forge-evaluation-tools",
        policy_version=1,
        agent_execution_id=request.execution_id,
        step_id=UUID("33333333-3333-4333-8333-333333333333"),
    )
    tools = ControlledEvaluationToolProvider(
        _RecordingService(), context, _Observer(), request
    ).tools_for(request)

    assert ToolName.GIT_COMMIT not in tools.names
    assert ToolName.GIT_COMMIT.value not in {tool.name for tool in tools.tools}


def test_evaluation_provider_preserves_adk_write_declaration_schema() -> None:
    """Observation must not erase ADK's model-visible path/content arguments."""

    request = build_agent_request(
        role=AgentRole.DEVELOPER,
        provider="google",
        model="test-model",
        allowed_tools=tuple(
            tool
            for tool in ToolName
            if tool in CapabilityMatrix().capabilities_for(AgentRole.DEVELOPER)
        ),
    )
    context = ToolAuthorizationContext(
        role=AgentRole.DEVELOPER,
        run_id=request.run_id,
        worktree_id="forge-evaluation-tools",
        policy_version=1,
        agent_execution_id=request.execution_id,
        step_id=UUID("33333333-3333-4333-8333-333333333333"),
    )
    provider = ControlledEvaluationToolProvider(_RecordingService(), context, _Observer(), request)
    write_tool = {tool.name: tool for tool in provider.tools_for(request).tools}[
        ToolName.REPOSITORY_WRITE_FILE.value
    ]

    declaration = write_tool._get_declaration()
    assert declaration.parameters_json_schema is not None
    assert set(declaration.parameters_json_schema["properties"]) == {"path", "content"}


@pytest.mark.asyncio
async def test_check_binds_preexecution_trust_and_excludes_concurrent_writes() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    class BlockingService(_RecordingService):
        async def invoke(
            self, context: ToolAuthorizationContext, request: ToolRequest
        ) -> ToolResult:
            if request.name is ToolName.BUILD_RUN_NAMED_CHECK:
                entered.set()
                await release.wait()
            return await super().invoke(context, request)

    request = build_agent_request(
        role=AgentRole.DEVELOPER,
        provider="google",
        model="test-model",
        allowed_tools=tuple(
            tool
            for tool in ToolName
            if tool in CapabilityMatrix().capabilities_for(AgentRole.DEVELOPER)
        ),
    )
    context = ToolAuthorizationContext(
        role=request.role,
        run_id=request.run_id,
        worktree_id="forge-evaluation-tools",
        policy_version=1,
        agent_execution_id=request.execution_id,
        step_id=UUID("33333333-3333-4333-8333-333333333333"),
    )
    service = BlockingService()
    observer = _Observer()
    tools = {
        tool.name: tool
        for tool in ControlledEvaluationToolProvider(service, context, observer, request)
        .tools_for(request)
        .tools
    }
    check = asyncio.create_task(
        tools[ToolName.BUILD_RUN_NAMED_CHECK.value].run_async(
            args={"command_name": "pytest"},
            tool_context=SimpleNamespace(invocation_id="eval", function_call_id="check"),
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=2)
    write = asyncio.create_task(
        tools[ToolName.REPOSITORY_WRITE_FILE.value].run_async(
            args={"path": "run_checks.py", "content": "forged"},
            tool_context=SimpleNamespace(invocation_id="eval", function_call_id="write"),
        )
    )
    await asyncio.sleep(0)
    try:
        assert service.requests == [], "write raced the check's integrity observation"
    finally:
        release.set()
        await asyncio.gather(check, write)
    assert observer.trust_observations == [False]
