"""Stable, context-free errors exposed by the Forge agent gateway."""

from __future__ import annotations


class AgentGatewayError(RuntimeError):
    """A gateway boundary failure without request, provider, or output details."""

    _MESSAGE = "agent gateway execution failed"

    def __init__(self, _detail: object = None) -> None:
        del _detail
        super().__init__(self._MESSAGE)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self._MESSAGE!r})"


class AgentOutputInvalid(AgentGatewayError):
    """The provider produced invalid output on both allowed attempts."""

    _MESSAGE = "agent output is invalid"


class AgentBudgetExceeded(AgentGatewayError):
    """The gateway cannot safely represent, price, or enforce the request budget."""

    _MESSAGE = "agent budget cannot be safely enforced"


__all__ = ["AgentBudgetExceeded", "AgentGatewayError", "AgentOutputInvalid"]
