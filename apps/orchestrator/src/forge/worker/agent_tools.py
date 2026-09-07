"""One request's controlled tools supplied to the provider gateway."""

from forge.agents.adk_gateway import BoundAdkTools
from forge.agents.errors import AgentGatewayError
from forge.domain.agent import AgentRequest


class PerRequestToolProvider:
    def __init__(self, bound_tools: BoundAdkTools, request: AgentRequest) -> None:
        self._bound_tools = bound_tools
        self._request = request

    def tools_for(self, request: AgentRequest) -> BoundAdkTools:
        if type(request) is not AgentRequest or request != self._request:
            raise AgentGatewayError("request execution mismatch")
        return self._bound_tools
