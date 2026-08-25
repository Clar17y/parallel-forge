"""Authorization boundary for controlled agent tools."""

from typing import Protocol

from forge.domain.actor import AgentRole
from forge.domain.tool import ToolAuthorization, ToolAuthorizationContext, ToolName, ToolRequest


class ToolAuthorizationDenied(PermissionError):
    """A stable denial that never includes agent-selected or repository text."""

    def __init__(self) -> None:
        super().__init__("tool authorization denied")


class ToolAuthorizerPort(Protocol):
    """Framework-independent fail-closed tool authorization contract."""

    def is_allowed(self, role: AgentRole, tool_name: ToolName) -> bool: ...

    def authorize(
        self,
        context: ToolAuthorizationContext,
        request: ToolRequest,
    ) -> ToolAuthorization: ...


__all__ = ["ToolAuthorizationDenied", "ToolAuthorizerPort"]
