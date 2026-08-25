"""Exhaustive, deny-by-default authorization for controlled agent tools."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from forge.application.ports.tools import ToolAuthorizationDenied
from forge.domain.actor import AgentRole
from forge.domain.tool import (
    ToolAuthorization,
    ToolAuthorizationContext,
    ToolName,
    ToolRequest,
)

_REPOSITORY_READS = frozenset(
    {
        ToolName.REPOSITORY_LIST_FILES,
        ToolName.REPOSITORY_READ_FILE,
        ToolName.REPOSITORY_SEARCH,
        ToolName.REPOSITORY_READ_INSTRUCTIONS,
    }
)
_CAPABILITIES = {
    AgentRole.PLANNER: _REPOSITORY_READS,
    AgentRole.DEVELOPER: _REPOSITORY_READS
    | {
        ToolName.REPOSITORY_WRITE_FILE,
        ToolName.GIT_STATUS,
        ToolName.GIT_DIFF,
        ToolName.GIT_COMMIT,
        ToolName.BUILD_RUN_NAMED_CHECK,
    },
    AgentRole.REVIEWER: _REPOSITORY_READS
    | {
        ToolName.GIT_STATUS,
        ToolName.GIT_DIFF,
        ToolName.VALIDATION_RESULTS_READ,
        ToolName.REVIEW_ARTIFACTS_READ,
    },
}

_TOOL_ARGUMENT_SCHEMAS = {
    ToolName.REPOSITORY_LIST_FILES: (frozenset(), frozenset({"path"})),
    ToolName.REPOSITORY_READ_FILE: (frozenset({"path"}), frozenset()),
    ToolName.REPOSITORY_SEARCH: (frozenset({"literal"}), frozenset({"path"})),
    ToolName.REPOSITORY_READ_INSTRUCTIONS: (frozenset(), frozenset({"target_path"})),
    ToolName.REPOSITORY_WRITE_FILE: (frozenset({"content", "path"}), frozenset()),
    ToolName.GIT_STATUS: (frozenset(), frozenset()),
    ToolName.GIT_DIFF: (frozenset(), frozenset()),
    ToolName.GIT_COMMIT: (frozenset({"message"}), frozenset()),
    ToolName.BUILD_RUN_NAMED_CHECK: (frozenset({"command_name"}), frozenset()),
    ToolName.VALIDATION_RESULTS_READ: (frozenset(), frozenset()),
    ToolName.REVIEW_ARTIFACTS_READ: (frozenset(), frozenset()),
}


@dataclass(frozen=True, slots=True)
class CapabilityMatrix:
    """The complete decision table for every closed role and tool pair."""

    @property
    def roles(self) -> frozenset[AgentRole]:
        return frozenset(_CAPABILITIES)

    @property
    def tools(self) -> frozenset[ToolName]:
        return frozenset(ToolName)

    def capabilities_for(self, role: AgentRole) -> frozenset[ToolName]:
        """Return no capabilities for values outside the closed role type."""

        if type(role) is not AgentRole:
            return frozenset()
        return frozenset(_CAPABILITIES[role])

    def is_allowed(self, role: AgentRole, tool_name: ToolName) -> bool:
        """Return the exact decision, denying unknown or untyped values."""

        return (
            type(role) is AgentRole
            and type(tool_name) is ToolName
            and tool_name in _CAPABILITIES[role]
        )


@dataclass(frozen=True, slots=True)
class ToolAuthorizer:
    """Bind a typed request to Forge-owned authority or fail closed."""

    _matrix: CapabilityMatrix = field(default_factory=CapabilityMatrix, init=False, repr=False)

    def is_allowed(self, role: AgentRole, tool_name: ToolName) -> bool:
        return self._matrix.is_allowed(role, tool_name)

    def authorize(
        self,
        context: ToolAuthorizationContext,
        request: ToolRequest,
    ) -> ToolAuthorization:
        if type(context) is not ToolAuthorizationContext or type(request) is not ToolRequest:
            raise ToolAuthorizationDenied()
        if not self.is_allowed(context.role, request.name):
            raise ToolAuthorizationDenied()
        if not _arguments_match_schema(request.name, request.arguments):
            raise ToolAuthorizationDenied()
        return ToolAuthorization(context=context, request=request)


def _arguments_match_schema(
    tool_name: ToolName,
    arguments: Mapping[str, object],
) -> bool:
    fields = frozenset(arguments)
    required, optional = _TOOL_ARGUMENT_SCHEMAS[tool_name]
    return required <= fields <= required | optional and all(
        type(value) is str for value in arguments.values()
    )


__all__ = ["CapabilityMatrix", "ToolAuthorizer"]
