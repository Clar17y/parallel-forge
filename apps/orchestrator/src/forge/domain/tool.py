"""Immutable, framework-independent controlled-tool values."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any
from uuid import UUID

from forge.domain.actor import AgentRole
from forge.domain.artifact import validate_artifact_digest
from forge.domain.payload import redact_durable_text, validate_durable_payload

if TYPE_CHECKING:
    from forge.domain.subscription import SpecialistPurpose

_ARGUMENT_KEY = re.compile(r"\A[a-z][a-z0-9_]{0,63}\Z")
_WORKTREE_ID = re.compile(r"\A[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\Z")
_MAX_ARGUMENT_COLLECTION_SIZE = 64
_MAX_ARGUMENT_DEPTH = 8
_MAX_ARGUMENT_NODES = 256
_MAX_ARGUMENT_STRING_LENGTH = 1_000_000
_MAX_POLICY_VERSION = 2**31 - 1
_MAX_WORKTREE_ID_LENGTH = 128
_MIN_INTEGER = -(2**63)
_MAX_INTEGER = 2**63 - 1
_MAX_ERROR_MESSAGE_LENGTH = 1024


def repository_resource_identity(project_id: UUID) -> str:
    """Return the Forge-owned identity for a registered canonical repository."""

    if not isinstance(project_id, UUID):
        raise TypeError("project identifier must be a UUID")
    if project_id.int == 0:
        raise ValueError("project identifier must not be nil")
    return f"forge-repository-{project_id.hex}"


class ToolName(StrEnum):
    """The complete non-release tool vocabulary available to agent roles."""

    REPOSITORY_LIST_FILES = "repository.list_files"
    REPOSITORY_READ_FILE = "repository.read_file"
    REPOSITORY_SEARCH = "repository.search"
    REPOSITORY_READ_INSTRUCTIONS = "repository.read_instructions"
    REPOSITORY_WRITE_FILE = "repository.write_file"
    REPOSITORY_DELETE_FILE = "repository.delete_file"
    REPOSITORY_RENAME_FILE = "repository.rename_file"
    GIT_STATUS = "git.status"
    GIT_DIFF = "git.diff"
    GIT_COMMIT = "git.commit"
    BUILD_RUN_NAMED_CHECK = "build.run_named_check"
    VALIDATION_RESULTS_READ = "validation-results.read"
    REVIEW_ARTIFACTS_READ = "review-artifacts.read"


class ToolCallStatus(StrEnum):
    """Terminal lifecycle values for an audited controlled-tool call."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DENIED = "denied"
    CANCELLED = "cancelled"


class ToolErrorCode(StrEnum):
    """Stable, non-sensitive categories returned by the invocation boundary."""

    AUTHORIZATION_DENIED = "authorization_denied"
    INVALID_REQUEST = "invalid_request"
    RUN_NOT_ACTIVE = "run_not_active"
    POLICY_MISMATCH = "policy_mismatch"
    RESOURCE_MISMATCH = "resource_mismatch"
    BUDGET_EXCEEDED = "budget_exceeded"
    TOOL_UNAVAILABLE = "tool_unavailable"
    ADAPTER_ERROR = "adapter_error"
    OPERATION_ERROR = "operation_error"
    PERSISTENCE_ERROR = "persistence_error"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolError:
    """A bounded error category safe to expose to an agent or API caller."""

    code: ToolErrorCode
    message: str

    def __post_init__(self) -> None:
        if not isinstance(self.code, ToolErrorCode):
            raise TypeError("tool error code must be a ToolErrorCode")
        if (
            not isinstance(self.message, str)
            or not self.message
            or len(self.message) > _MAX_ERROR_MESSAGE_LENGTH
        ):
            raise ValueError("tool error message is invalid")
        safe = redact_durable_text(self.message)
        if safe != self.message:
            object.__setattr__(self, "message", safe)
        validate_durable_payload({"message": self.message})

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolRequest:
    """One typed tool choice plus bounded agent-selected arguments.

    Argument values are deliberately omitted from the representation. Future
    invocation code can normalize and audit them without making authorization
    errors or diagnostic logs a disclosure channel.
    """

    name: ToolName
    arguments: Mapping[str, object]

    def __post_init__(self) -> None:
        if type(self.name) is not ToolName:
            raise TypeError("tool request name must be a ToolName")
        if not isinstance(self.arguments, Mapping):
            raise TypeError("tool request arguments must be a mapping")
        budget = [_MAX_ARGUMENT_NODES]
        frozen = _freeze_argument(dict(self.arguments), depth=0, budget=budget)
        if not isinstance(frozen, Mapping):
            raise TypeError("tool request arguments must be a mapping")
        object.__setattr__(self, "arguments", frozen)

    def __repr__(self) -> str:
        """Return only the closed tool name and argument keys."""

        return (
            f"{type(self).__name__}(name={self.name.value!r}, "
            f"argument_keys={tuple(sorted(self.arguments))!r})"
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolAuthorizationContext:
    """Forge-supplied authority that agent-selected arguments cannot replace."""

    role: AgentRole
    run_id: UUID
    worktree_id: str
    policy_version: int
    # Task 14A callers may omit these while the invocation boundary requires
    # them.  Defaults preserve the reviewed authorization-only API while
    # allowing Forge to bind the persisted execution and step identities.
    agent_execution_id: UUID | None = None
    step_id: UUID | None = None
    # A Forge-owned deterministic invocation identity, derived at an SDK
    # boundary.  It is intentionally outside agent-selected ToolRequest args.
    invocation_id: UUID | None = None

    def __post_init__(self) -> None:
        if type(self.role) is not AgentRole:
            raise TypeError("tool authorization role must be an AgentRole")
        if not isinstance(self.run_id, UUID):
            raise TypeError("tool authorization run identifier must be a UUID")
        if self.run_id.int == 0:
            raise ValueError("tool authorization run identifier must not be nil")
        if not isinstance(self.worktree_id, str):
            raise TypeError("tool authorization worktree identifier must be a string")
        if len(self.worktree_id) > _MAX_WORKTREE_ID_LENGTH or not _WORKTREE_ID.fullmatch(
            self.worktree_id
        ):
            raise ValueError("tool authorization worktree identifier is invalid")
        if (
            type(self.policy_version) is not int
            or self.policy_version < 1
            or self.policy_version > _MAX_POLICY_VERSION
        ):
            raise ValueError("tool authorization policy version is outside the supported range")
        for value, name in (
            (self.agent_execution_id, "agent execution identifier"),
            (self.step_id, "run-step identifier"),
            (self.invocation_id, "invocation identifier"),
        ):
            if value is not None and (not isinstance(value, UUID) or value.int == 0):
                raise ValueError(f"tool authorization {name} must be a non-nil UUID")


@dataclass(frozen=True, slots=True, kw_only=True)
class SubscriptionToolAuthorizationContext:
    """Trusted v0.2 authority with no fabricated legacy execution identity.

    This deliberately names the specialist purpose and subscription lineage;
    callers cannot replace any of these values through tool arguments.
    """

    run_id: UUID
    task_id: UUID
    attempt_id: UUID
    worktree_id: str
    purpose: SpecialistPurpose
    policy_version: int
    permitted_tools: frozenset[ToolName]
    invocation_id: UUID | None = None
    operation_intent_id: UUID | None = None

    def __post_init__(self) -> None:
        # Delayed import avoids the ToolName <-> subscription domain cycle.
        from forge.domain.subscription import SPECIALIST_ALLOWED_TOOLS, SpecialistPurpose

        for value, name in (
            (self.run_id, "run identifier"),
            (self.task_id, "task identifier"),
            (self.attempt_id, "attempt identifier"),
        ):
            if not isinstance(value, UUID) or value.int == 0:
                raise ValueError(f"subscription tool authorization {name} must be a non-nil UUID")
        if not isinstance(self.worktree_id, str) or (
            len(self.worktree_id) > _MAX_WORKTREE_ID_LENGTH
            or _WORKTREE_ID.fullmatch(self.worktree_id) is None
        ):
            raise ValueError("subscription tool authorization worktree identifier is invalid")
        if not isinstance(self.purpose, SpecialistPurpose):
            raise TypeError("subscription tool authorization purpose must be a SpecialistPurpose")
        if type(self.policy_version) is not int or not 1 <= self.policy_version <= _MAX_POLICY_VERSION:
            raise ValueError("subscription tool authorization policy version is outside the supported range")
        permitted = frozenset(self.permitted_tools)
        allowed = SPECIALIST_ALLOWED_TOOLS[self.purpose]
        if any(not isinstance(tool, ToolName) or tool not in allowed for tool in permitted):
            raise ValueError("subscription tool permissions exceed specialist capability")
        object.__setattr__(self, "permitted_tools", permitted)
        for optional_id, name in ((self.invocation_id, "invocation identifier"), (self.operation_intent_id, "operation identifier")):
            if optional_id is not None and (not isinstance(optional_id, UUID) or optional_id.int == 0):
                raise ValueError(f"subscription tool authorization {name} must be a non-nil UUID")


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolAuthorization:
    """A successful decision bound to Forge-owned context and one typed request."""

    context: ToolAuthorizationContext | SubscriptionToolAuthorizationContext
    request: ToolRequest

    def __post_init__(self) -> None:
        if type(self.context) not in (ToolAuthorizationContext, SubscriptionToolAuthorizationContext):
            raise TypeError("tool authorization requires a trusted context")
        if type(self.request) is not ToolRequest:
            raise TypeError("tool authorization requires a typed request")

    @property
    def role(self) -> AgentRole:
        if type(self.context) is not ToolAuthorizationContext:
            raise TypeError("subscription authority has a specialist purpose, not an agent role")
        return self.context.role

    @property
    def run_id(self) -> UUID:
        return self.context.run_id

    @property
    def worktree_id(self) -> str:
        return self.context.worktree_id

    @property
    def policy_version(self) -> int:
        return self.context.policy_version

    @property
    def agent_execution_id(self) -> UUID | None:
        return self.context.agent_execution_id if type(self.context) is ToolAuthorizationContext else None

    @property
    def step_id(self) -> UUID | None:
        return self.context.step_id if type(self.context) is ToolAuthorizationContext else None

    @property
    def tool_name(self) -> ToolName:
        return self.request.name

    @property
    def arguments(self) -> Mapping[str, object]:
        return self.request.arguments


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolResult:
    """Bounded structured outcome returned by ``ControlledToolService``."""

    tool_name: ToolName
    status: ToolCallStatus
    metadata: Mapping[str, object] = MappingProxyType({})
    artifact_digests: tuple[str, ...] = ()
    error: ToolError | None = None
    tool_call_id: UUID | None = None
    operation_intent_id: UUID | None = None
    correlation_id: UUID | None = None
    agent_execution_id: UUID | None = None
    step_id: UUID | None = None
    duration_ms: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.tool_name, ToolName):
            raise TypeError("tool result name must be a ToolName")
        if not isinstance(self.status, ToolCallStatus):
            raise TypeError("tool result status must be a ToolCallStatus")
        if not isinstance(self.metadata, Mapping):
            raise TypeError("tool result metadata must be a mapping")
        validate_durable_payload(self.metadata)
        object.__setattr__(self, "metadata", _freeze(dict(self.metadata)))
        digests = tuple(self.artifact_digests)
        if digests != tuple(sorted(set(digests))):
            raise ValueError("tool result artifact digests must be unique and sorted")
        for digest in digests:
            validate_artifact_digest(digest)
        object.__setattr__(self, "artifact_digests", digests)
        if self.error is not None and not isinstance(self.error, ToolError):
            raise TypeError("tool result error must be a ToolError")
        if self.status in {ToolCallStatus.FAILED, ToolCallStatus.DENIED, ToolCallStatus.CANCELLED}:
            if self.error is None:
                raise ValueError("non-success tool results require an error")
        elif self.error is not None:
            raise ValueError("successful tool results cannot contain an error")
        for value, name in (
            (self.tool_call_id, "tool call identifier"),
            (self.operation_intent_id, "operation intent identifier"),
            (self.correlation_id, "correlation identifier"),
            (self.agent_execution_id, "agent execution identifier"),
            (self.step_id, "run-step identifier"),
        ):
            if value is not None and (not isinstance(value, UUID) or value.int == 0):
                raise ValueError(f"{name} must be a non-nil UUID")
        if type(self.duration_ms) is not int or self.duration_ms < 0:
            raise ValueError("tool result duration must be a nonnegative integer")

    @property
    def result_metadata(self) -> Mapping[str, object]:
        """Persistence-oriented alias for the bounded metadata object."""

        return self.metadata

    @property
    def result_status(self) -> ToolCallStatus:
        """Compatibility alias for callers naming the status explicitly."""

        return self.status


# Descriptive aliases keep the public vocabulary compatible with callers that
# call this lifecycle an invocation rather than a tool call.
ToolInvocationStatus = ToolCallStatus
ToolStatus = ToolCallStatus
ToolInvocationErrorCode = ToolErrorCode


def _freeze_argument(value: Any, *, depth: int, budget: list[int]) -> object:
    if depth > _MAX_ARGUMENT_DEPTH:
        raise ValueError("tool arguments are nested too deeply")
    budget[0] -= 1
    if budget[0] < 0:
        raise ValueError("tool arguments contain too many values")

    if value is None or type(value) is bool:
        return value
    if type(value) is int:
        if not _MIN_INTEGER <= value <= _MAX_INTEGER:
            raise ValueError("tool argument integer is out of range")
        return value
    if type(value) is str:
        if len(value) > _MAX_ARGUMENT_STRING_LENGTH:
            raise ValueError("tool argument text is too long")
        return value
    if isinstance(value, Mapping):
        if len(value) > _MAX_ARGUMENT_COLLECTION_SIZE:
            raise ValueError("tool argument mapping contains too many entries")
        frozen: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not _ARGUMENT_KEY.fullmatch(key):
                raise ValueError("tool argument keys must be bounded snake_case names")
            frozen[key] = _freeze_argument(item, depth=depth + 1, budget=budget)
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        if len(value) > _MAX_ARGUMENT_COLLECTION_SIZE:
            raise ValueError("tool argument sequence contains too many entries")
        return tuple(_freeze_argument(item, depth=depth + 1, budget=budget) for item in value)
    raise TypeError("tool arguments must contain only bounded JSON-compatible values")


def _freeze(value: Any) -> Any:
    """Detach JSON-compatible result metadata into immutable containers."""

    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


__all__ = [
    "SubscriptionToolAuthorizationContext",
    "ToolAuthorization",
    "ToolAuthorizationContext",
    "ToolCallStatus",
    "ToolError",
    "ToolErrorCode",
    "ToolInvocationErrorCode",
    "ToolInvocationStatus",
    "ToolName",
    "ToolRequest",
    "ToolResult",
    "ToolStatus",
    "repository_resource_identity",
]
