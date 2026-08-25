"""Immutable, framework-independent controlled-tool values."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any
from uuid import UUID

from forge.domain.actor import AgentRole

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


class ToolName(StrEnum):
    """The complete non-release tool vocabulary available to agent roles."""

    REPOSITORY_LIST_FILES = "repository.list_files"
    REPOSITORY_READ_FILE = "repository.read_file"
    REPOSITORY_SEARCH = "repository.search"
    REPOSITORY_READ_INSTRUCTIONS = "repository.read_instructions"
    REPOSITORY_WRITE_FILE = "repository.write_file"
    GIT_STATUS = "git.status"
    GIT_DIFF = "git.diff"
    GIT_COMMIT = "git.commit"
    BUILD_RUN_NAMED_CHECK = "build.run_named_check"
    VALIDATION_RESULTS_READ = "validation-results.read"
    REVIEW_ARTIFACTS_READ = "review-artifacts.read"


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


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolAuthorization:
    """A successful decision bound to Forge-owned context and one typed request."""

    context: ToolAuthorizationContext
    request: ToolRequest

    def __post_init__(self) -> None:
        if type(self.context) is not ToolAuthorizationContext:
            raise TypeError("tool authorization requires a trusted context")
        if type(self.request) is not ToolRequest:
            raise TypeError("tool authorization requires a typed request")

    @property
    def role(self) -> AgentRole:
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
    def tool_name(self) -> ToolName:
        return self.request.name

    @property
    def arguments(self) -> Mapping[str, object]:
        return self.request.arguments


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


__all__ = [
    "ToolAuthorization",
    "ToolAuthorizationContext",
    "ToolName",
    "ToolRequest",
]
