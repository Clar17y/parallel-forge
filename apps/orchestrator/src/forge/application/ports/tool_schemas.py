"""Closed controlled-tool argument schemas shared without effect dependencies."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from forge.domain.tool import ToolName

GIT_DIFF_SCOPES = ("working_tree", "candidate", "snapshot")


TOOL_ARGUMENT_SCHEMAS = MappingProxyType(
    {
        ToolName.REPOSITORY_LIST_FILES: (frozenset(), frozenset({"path"})),
        ToolName.REPOSITORY_READ_FILE: (frozenset({"path"}), frozenset()),
        ToolName.REPOSITORY_SEARCH: (frozenset({"literal"}), frozenset({"path"})),
        ToolName.REPOSITORY_READ_INSTRUCTIONS: (frozenset(), frozenset({"target_path"})),
        ToolName.REPOSITORY_WRITE_FILE: (frozenset({"content", "path"}), frozenset()),
        ToolName.REPOSITORY_DELETE_FILE: (frozenset({"path", "expected_digest"}), frozenset()),
        ToolName.REPOSITORY_RENAME_FILE: (
            frozenset({"source", "destination", "expected_digest"}),
            frozenset(),
        ),
        ToolName.GIT_STATUS: (frozenset(), frozenset()),
        ToolName.GIT_DIFF: (frozenset(), frozenset({"scope"})),
        ToolName.GIT_COMMIT: (frozenset({"message"}), frozenset()),
        ToolName.BUILD_RUN_NAMED_CHECK: (frozenset({"command_name"}), frozenset()),
        ToolName.VALIDATION_RESULTS_READ: (frozenset(), frozenset()),
        ToolName.REVIEW_ARTIFACTS_READ: (frozenset(), frozenset()),
    }
)


def arguments_match_schema(tool_name: ToolName, arguments: Mapping[str, object]) -> bool:
    required, optional = TOOL_ARGUMENT_SCHEMAS[tool_name]
    fields = frozenset(arguments)
    if (
        tool_name is ToolName.GIT_DIFF
        and arguments.get("scope", "working_tree") not in GIT_DIFF_SCOPES
    ):
        return False
    return required <= fields <= required | optional and all(
        type(value) is str for value in arguments.values()
    )


__all__ = ["GIT_DIFF_SCOPES", "TOOL_ARGUMENT_SCHEMAS", "arguments_match_schema"]
