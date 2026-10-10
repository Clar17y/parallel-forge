"""Runtime-checkable async agent gateway port."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from forge.application.ports.task_usage import TaskUsageObserver
from forge.domain.agent import AgentRequest, AgentResult
from forge.domain.task_usage_contract import WorkUnitBinding


@runtime_checkable
class AgentGateway(Protocol):
    """Provider-neutral boundary executing one typed agent request."""

    async def execute(self, request: AgentRequest) -> AgentResult:
        """Execute one validated agent request and return structured result."""
        ...


class ObservableAgentGateway(AgentGateway, Protocol):
    """Opt-in execution with a caller-owned observation sink."""

    async def execute_observed(
        self,
        request: AgentRequest,
        *,
        usage_binding: WorkUnitBinding,
        usage_sink: TaskUsageObserver,
    ) -> AgentResult: ...


__all__ = ["AgentGateway", "ObservableAgentGateway"]
