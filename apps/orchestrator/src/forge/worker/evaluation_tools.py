"""Evaluation adapter for observing Forge-controlled ADK tool outcomes.

This module deliberately composes the same ``build_adk_tools`` bridge used by
production delivery.  Evaluation code supplies an already-admitted service and
authority context; it cannot provide a path, command, or synthetic success.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from functools import wraps
from inspect import signature
from typing import Protocol

from google.adk.tools.function_tool import FunctionTool

from forge.agents.adk_gateway import AdkToolProvider, BoundAdkTools
from forge.agents.errors import AgentGatewayError
from forge.agents.tool_bridge import build_adk_tools
from forge.application.services.tools import ControlledToolService
from forge.domain.agent import AgentRequest
from forge.domain.tool import ToolAuthorizationContext, ToolName


class EvaluationToolOutcomeObserver(Protocol):
    """Narrow observation sink kept independent of evaluation implementation."""

    def record_controlled_result(
        self,
        tool_name: ToolName,
        result: Mapping[str, object],
        *,
        arguments: Mapping[str, object] | None = None,
    ) -> None: ...

    def check_harness_is_intact(self, command_name: str) -> bool: ...

    async def record_check_artifacts(
        self, result: Mapping[str, object], *, command_name: str, harness_trusted: bool
    ) -> None: ...


class ControlledEvaluationToolProvider(AdkToolProvider):
    """Bind one admitted execution to real Forge-controlled ADK functions."""

    def __init__(
        self,
        service: ControlledToolService,
        context: ToolAuthorizationContext,
        observer: EvaluationToolOutcomeObserver,
        request: AgentRequest,
    ) -> None:
        if not isinstance(service, ControlledToolService):
            raise TypeError("evaluation tools require ControlledToolService")
        if type(context) is not ToolAuthorizationContext or type(request) is not AgentRequest:
            raise TypeError("evaluation tools require durable execution authority")
        if (
            context.role is not request.role
            or context.run_id != request.run_id
            or context.agent_execution_id != request.execution_id
        ):
            raise AgentGatewayError("evaluation tool authority differs from request")
        self._service = service
        self._context = context
        self._observer = observer
        self._request = request
        self._invocation_lock = asyncio.Lock()

    def tools_for(self, request: AgentRequest) -> BoundAdkTools:
        if type(request) is not AgentRequest or request != self._request:
            raise AgentGatewayError("evaluation tool request differs")
        original = build_adk_tools(self._service, self._context)
        available = {ToolName(tool.name): tool for tool in original}
        if not set(request.allowed_tools).issubset(available):
            raise AgentGatewayError("evaluation tools differ from admitted request")
        names = request.allowed_tools
        return BoundAdkTools(
            names=names,
            tools=tuple(self._observe(name, available[name]) for name in names),
        )

    def _observe(self, name: ToolName, tool: FunctionTool) -> FunctionTool:
        # ADK derives the model-visible JSON declaration from the callable
        # signature.  Preserve the original bridge function's signature (and
        # annotations) while replacing only its execution body for observing
        # the controlled receipt.
        @wraps(tool.func)
        async def invoke(*args: object, **kwargs: object) -> dict[str, object]:
            # One fixture execution must not race writes against its check harness.
            async with self._invocation_lock:
                command_name = kwargs.get("command_name")
                check_name = (
                    command_name
                    if name is ToolName.BUILD_RUN_NAMED_CHECK and isinstance(command_name, str)
                    else None
                )
                harness_trusted = (
                    self._observer.check_harness_is_intact(check_name)
                    if check_name is not None
                    else False
                )
                result = await tool.func(*args, **kwargs)
                if isinstance(result, Mapping):
                    self._observer.record_controlled_result(name, result, arguments=kwargs)
                    if check_name is not None:
                        await self._observer.record_check_artifacts(
                            result, command_name=check_name, harness_trusted=harness_trusted
                        )
                    return dict(result)
                raise AgentGatewayError("controlled evaluation tool response is malformed")

        invoke.__dict__["__signature__"] = signature(tool.func)
        return FunctionTool(invoke)


class EvaluationToolRegistry(AdkToolProvider):
    """Resolve one production-controlled binding for each admitted eval execution."""

    def __init__(self) -> None:
        self._providers: dict[object, ControlledEvaluationToolProvider] = {}
        self._observers: dict[object, EvaluationToolOutcomeObserver] = {}

    def register(
        self,
        request: AgentRequest,
        service: ControlledToolService,
        context: ToolAuthorizationContext,
        observer: EvaluationToolOutcomeObserver,
    ) -> None:
        if request.execution_id in self._providers:
            raise AgentGatewayError("evaluation execution tools already registered")
        self._providers[request.execution_id] = ControlledEvaluationToolProvider(
            service, context, observer, request
        )
        self._observers[request.execution_id] = observer

    def observer_for(self, execution_id: object) -> EvaluationToolOutcomeObserver | None:
        return self._observers.get(execution_id)

    def tools_for(self, request: AgentRequest) -> BoundAdkTools:
        if type(request) is not AgentRequest:
            raise AgentGatewayError("evaluation tool request is invalid")
        provider = self._providers.get(request.execution_id)
        if provider is None:
            raise AgentGatewayError("evaluation execution tools are not admitted")
        return provider.tools_for(request)


__all__ = ["ControlledEvaluationToolProvider", "EvaluationToolRegistry"]
