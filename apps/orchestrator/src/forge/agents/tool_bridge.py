"""Google ADK functions that retain Forge-owned controlled-tool authority."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from uuid import UUID, uuid5

from forge.application.services.tools import CapabilityMatrix, ControlledToolService
from forge.domain.event import thaw_payload
from forge.domain.tool import (
    ToolAuthorizationContext,
    ToolCallStatus,
    ToolErrorCode,
    ToolName,
    ToolRequest,
    ToolResult,
)
from google.adk.tools.function_tool import FunctionTool
from google.adk.tools.tool_context import ToolContext

_SAFE_FAILURE_MESSAGE = "controlled tool invocation failed"
_INVOCATION_NAMESPACE = UUID("dfae00ec-cdd7-5cf0-a270-b1236cc4c7cf")
_SDK_CORRELATION_ID_MAX_BYTES = 255
_SDK_CORRELATION_ID = re.compile(r"\A[^\x00-\x1f\x7f]+\Z")
_EFFECT_TOOLS = frozenset(
    {
        ToolName.REPOSITORY_WRITE_FILE,
        ToolName.GIT_COMMIT,
        ToolName.BUILD_RUN_NAMED_CHECK,
    }
)
type _AdkFunction = Callable[..., Awaitable[dict[str, object]]]


def build_adk_tools(
    service: ControlledToolService,
    context: ToolAuthorizationContext,
) -> tuple[FunctionTool, ...]:
    """Return only role-allowed ADK tools closed over exact Forge authority."""

    if not isinstance(service, ControlledToolService):
        raise TypeError("ADK tool bridge requires ControlledToolService")
    if type(context) is not ToolAuthorizationContext:
        raise TypeError("ADK tool bridge requires Forge tool authority")

    async def invoke(
        name: ToolName,
        arguments: Mapping[str, object],
        tool_context: ToolContext,
    ) -> dict[str, object]:
        try:
            # ToolContext is supplied by the SDK.  Reading its attributes is
            # therefore part of the untrusted adapter boundary as well.
            invocation_id = _derive_invocation_id(context, tool_context)
            if invocation_id is None and name in _EFFECT_TOOLS:
                return _failed_result(name)
            call_context = replace(context, invocation_id=invocation_id)
            result = await service.invoke(
                call_context,
                ToolRequest(name=name, arguments=arguments),
            )
            return _tool_result(result)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - model context receives one safe category
            return _failed_result(name)

    async def repository_list_files(
        tool_context: ToolContext, path: str = "."
    ) -> dict[str, object]:
        """List bounded repository entries below a repository-relative path."""

        return await invoke(ToolName.REPOSITORY_LIST_FILES, {"path": path}, tool_context)

    async def repository_read_file(path: str, tool_context: ToolContext) -> dict[str, object]:
        """Read one bounded UTF-8 file from the retained managed worktree."""

        return await invoke(ToolName.REPOSITORY_READ_FILE, {"path": path}, tool_context)

    async def repository_search(
        literal: str, tool_context: ToolContext, path: str = "."
    ) -> dict[str, object]:
        """Search for a literal string below a repository-relative path."""

        return await invoke(
            ToolName.REPOSITORY_SEARCH, {"literal": literal, "path": path}, tool_context
        )

    async def repository_read_instructions(
        tool_context: ToolContext, target_path: str = "."
    ) -> dict[str, object]:
        """Read bounded untrusted repository instructions for a target path."""

        return await invoke(
            ToolName.REPOSITORY_READ_INSTRUCTIONS,
            {"target_path": target_path},
            tool_context,
        )

    async def repository_write_file(
        path: str, content: str, tool_context: ToolContext
    ) -> dict[str, object]:
        """Write bounded UTF-8 content to one repository-relative file."""

        return await invoke(
            ToolName.REPOSITORY_WRITE_FILE,
            {"path": path, "content": content},
            tool_context,
        )

    async def git_status(tool_context: ToolContext) -> dict[str, object]:
        """Return bounded Git status for the retained managed worktree."""

        return await invoke(ToolName.GIT_STATUS, {}, tool_context)

    async def git_diff(tool_context: ToolContext, scope: str = "working_tree") -> dict[str, object]:
        """Read working_tree changes, or candidate evidence after committing all changes.

        Candidate requires a clean worktree and returns the approved-base-to-HEAD
        patch, head_sha, changed_paths and diff_digest for structured agent output.
        Only working_tree and candidate scopes are accepted; no refs or argv.
        """

        arguments = {} if scope == "working_tree" else {"scope": scope}
        return await invoke(ToolName.GIT_DIFF, arguments, tool_context)

    async def git_commit(message: str, tool_context: ToolContext) -> dict[str, object]:
        """Create one controlled commit with a bounded commit message."""

        return await invoke(ToolName.GIT_COMMIT, {"message": message}, tool_context)

    async def build_run_named_check(
        command_name: str, tool_context: ToolContext
    ) -> dict[str, object]:
        """Run one policy-defined named build check without accepting argv."""

        return await invoke(
            ToolName.BUILD_RUN_NAMED_CHECK,
            {"command_name": command_name},
            tool_context,
        )

    async def validation_results_read(tool_context: ToolContext) -> dict[str, object]:
        """Read bounded validation evidence already owned by Forge."""

        return await invoke(ToolName.VALIDATION_RESULTS_READ, {}, tool_context)

    async def review_artifacts_read(tool_context: ToolContext) -> dict[str, object]:
        """Read bounded review evidence already owned by Forge."""

        return await invoke(ToolName.REVIEW_ARTIFACTS_READ, {}, tool_context)

    functions: dict[ToolName, _AdkFunction] = {
        ToolName.REPOSITORY_LIST_FILES: repository_list_files,
        ToolName.REPOSITORY_READ_FILE: repository_read_file,
        ToolName.REPOSITORY_SEARCH: repository_search,
        ToolName.REPOSITORY_READ_INSTRUCTIONS: repository_read_instructions,
        ToolName.REPOSITORY_WRITE_FILE: repository_write_file,
        ToolName.GIT_STATUS: git_status,
        ToolName.GIT_DIFF: git_diff,
        ToolName.GIT_COMMIT: git_commit,
        ToolName.BUILD_RUN_NAMED_CHECK: build_run_named_check,
        ToolName.VALIDATION_RESULTS_READ: validation_results_read,
        ToolName.REVIEW_ARTIFACTS_READ: review_artifacts_read,
    }
    allowed = CapabilityMatrix().capabilities_for(context.role)
    return tuple(_function_tool(name, functions[name]) for name in ToolName if name in allowed)


def _function_tool(name: ToolName, function: _AdkFunction) -> FunctionTool:
    function.__name__ = name.value
    return FunctionTool(function)


def _derive_invocation_id(
    context: ToolAuthorizationContext,
    tool_context: ToolContext,
) -> UUID | None:
    """Derive the v1 Forge invocation UUID without retaining provider IDs.

    The length-prefixed UTF-8 encoding prevents tuples such as ``("ab", "c")``
    and ``("a", "bc")`` from sharing a UUID input.  This is deliberately the
    only place ADK correlation data crosses into Forge authority.
    """

    if context.agent_execution_id is None or context.step_id is None:
        return None
    invocation_id = _sdk_correlation_id(getattr(tool_context, "invocation_id", None))
    function_call_id = _sdk_correlation_id(getattr(tool_context, "function_call_id", None))
    if invocation_id is None or function_call_id is None:
        return None
    parts = (
        b"forge.tool-invocation.v1",
        context.run_id.bytes,
        context.agent_execution_id.bytes,
        context.step_id.bytes,
        invocation_id,
        function_call_id,
    )
    encoded = b"".join(len(part).to_bytes(4, "big") + part for part in parts)
    return uuid5(_INVOCATION_NAMESPACE, encoded.hex())


def _sdk_correlation_id(value: object) -> bytes | None:
    if (
        type(value) is not str
        or value != value.strip()
        or _SDK_CORRELATION_ID.fullmatch(value) is None
    ):
        return None
    try:
        encoded = value.encode("utf-8")
    except UnicodeError:
        return None
    if not encoded or len(encoded) > _SDK_CORRELATION_ID_MAX_BYTES:
        return None
    return encoded


def _tool_result(result: ToolResult) -> dict[str, object]:
    if not isinstance(result, ToolResult):
        raise TypeError("controlled tool result is malformed")
    metadata = thaw_payload(result.metadata)
    if not isinstance(metadata, Mapping):
        raise TypeError("controlled tool metadata is malformed")
    error: dict[str, object] | None = None
    if result.error is not None:
        error = {
            "code": result.error.code.value,
            "message": result.error.message,
        }
    return {
        "agent_execution_id": _identifier(result.agent_execution_id),
        "artifact_digests": list(result.artifact_digests),
        "correlation_id": _identifier(result.correlation_id),
        "duration_ms": result.duration_ms,
        "error": error,
        "metadata": dict(metadata),
        "operation_intent_id": _identifier(result.operation_intent_id),
        "status": result.status.value,
        "step_id": _identifier(result.step_id),
        "tool_call_id": _identifier(result.tool_call_id),
        "tool_name": result.tool_name.value,
    }


def _failed_result(name: ToolName) -> dict[str, object]:
    return {
        "agent_execution_id": None,
        "artifact_digests": [],
        "correlation_id": None,
        "duration_ms": 0,
        "error": {
            "code": ToolErrorCode.OPERATION_ERROR.value,
            "message": _SAFE_FAILURE_MESSAGE,
        },
        "metadata": {},
        "operation_intent_id": None,
        "status": ToolCallStatus.FAILED.value,
        "step_id": None,
        "tool_call_id": None,
        "tool_name": name.value,
    }


def _identifier(value: object) -> str | None:
    return None if value is None else str(value)


__all__ = ["build_adk_tools"]
