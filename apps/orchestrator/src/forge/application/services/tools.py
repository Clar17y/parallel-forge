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

_AUTHORITY_ARGUMENTS = frozenset(
    {
        "agent_role",
        "policy_version",
        "resource_id",
        "role",
        "run_id",
        "worktree_id",
        "worktree_path",
    }
)
_EXECUTION_CONTROL_ARGUMENTS = frozenset(
    {
        "argv",
        "command_text",
        "cwd",
        "docker_flags",
        "environment",
        "mounts",
        "network_enabled",
        "shell",
        "shell_command",
    }
)
_FORBIDDEN_ARGUMENTS = _AUTHORITY_ARGUMENTS | _EXECUTION_CONTROL_ARGUMENTS


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
        if _contains_forbidden_argument(request.arguments):
            raise ToolAuthorizationDenied()
        return ToolAuthorization(context=context, request=request)


def _contains_forbidden_argument(value: object) -> bool:
    if isinstance(value, Mapping):
        return any(
            key in _FORBIDDEN_ARGUMENTS or _contains_forbidden_argument(item)
            for key, item in value.items()
        )
    if isinstance(value, tuple):
        return any(_contains_forbidden_argument(item) for item in value)
    return False


__all__ = ["CapabilityMatrix", "ToolAuthorizer"]
