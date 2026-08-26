"""Runtime-checkable async agent gateway port."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from forge.domain.agent import AgentRequest, AgentResult


@runtime_checkable
class AgentGateway(Protocol):
    """Provider-neutral boundary executing one typed agent request."""

    async def execute(self, request: AgentRequest) -> AgentResult:
        """Execute one validated agent request and return structured result."""
        ...


__all__ = ["AgentGateway"]
