"""Authorization and audited-invocation boundaries for controlled tools."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from forge.domain.actor import AgentRole
from forge.domain.artifact import validate_artifact_digest
from forge.domain.payload import validate_durable_payload
from forge.domain.tool import (
    ToolAuthorization,
    ToolAuthorizationContext,
    ToolCallStatus,
    ToolName,
    ToolRequest,
)

_HEX_64 = re.compile(r"\A[0-9a-f]{64}\Z", re.ASCII)


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


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolCallRecord:
    """The complete bounded evidence projection for one tool-call row.

    The initial v0.1 SQL model predates some of these lineage fields.  The
    PostgreSQL adapter stores fields absent from that model in the versioned
    result metadata object, preserving the no-migration contract.
    """

    id: UUID
    run_id: UUID
    agent_execution_id: UUID
    tool_name: ToolName
    normalized_arguments: Mapping[str, object]
    authorized: bool
    status: ToolCallStatus
    started_at: datetime
    completed_at: datetime | None = None
    result_metadata: Mapping[str, object] | None = None
    step_id: UUID | None = None
    role: AgentRole | None = None
    policy_version: int | None = None
    duration_ms: int | None = None
    artifact_digests: tuple[str, ...] = ()
    correlation_id: UUID | None = None
    operation_intent_id: UUID | None = None
    arguments_schema_version: int = 1
    result_metadata_schema_version: int | None = None
    request_digest: str | None = None
    resource_id: str | None = None
    invocation_schema_version: int | None = None

    def __post_init__(self) -> None:
        for value, name in (
            (self.id, "tool call identifier"),
            (self.run_id, "tool call run identifier"),
            (self.agent_execution_id, "tool call agent execution identifier"),
        ):
            if not isinstance(value, UUID) or value.int == 0:
                raise ValueError(f"{name} must be a non-nil UUID")
        if self.step_id is not None and (
            not isinstance(self.step_id, UUID) or self.step_id.int == 0
        ):
            raise ValueError("tool call step identifier must be a non-nil UUID")
        if self.role is not None and not isinstance(self.role, AgentRole):
            raise TypeError("tool call role must be an AgentRole")
        if not isinstance(self.tool_name, ToolName):
            raise TypeError("tool call name must be a ToolName")
        if not isinstance(self.status, ToolCallStatus):
            raise TypeError("tool call status must be a ToolCallStatus")
        if type(self.authorized) is not bool:
            raise TypeError("tool call authorization decision must be boolean")
        if self.authorized and self.status is ToolCallStatus.DENIED:
            raise ValueError("an authorized tool call cannot be denied")
        if type(self.arguments_schema_version) is not int or self.arguments_schema_version < 1:
            raise ValueError("tool call argument schema version must be positive")
        if self.result_metadata is not None:
            if not isinstance(self.result_metadata, Mapping):
                raise TypeError("tool call result metadata must be a mapping")
            validate_durable_payload(self.result_metadata)
            if self.result_metadata_schema_version is None:
                raise ValueError("result metadata requires a schema version")
        elif self.result_metadata_schema_version is not None:
            raise ValueError("result metadata schema requires metadata")
        if not isinstance(self.normalized_arguments, Mapping):
            raise TypeError("tool call normalized arguments must be a mapping")
        validate_durable_payload(self.normalized_arguments)
        if not isinstance(self.started_at, datetime):
            raise TypeError("tool call start time must be a datetime")
        if self.started_at.tzinfo is None or self.started_at.utcoffset() is None:
            raise ValueError("tool call start time must be timezone-aware")
        if self.completed_at is not None:
            if not isinstance(self.completed_at, datetime):
                raise TypeError("tool call completion time must be a datetime")
            if self.completed_at.tzinfo is None or self.completed_at.utcoffset() is None:
                raise ValueError("tool call completion time must be timezone-aware")
        if self.completed_at is None and self.status in {
            ToolCallStatus.SUCCEEDED,
            ToolCallStatus.FAILED,
            ToolCallStatus.DENIED,
            ToolCallStatus.CANCELLED,
        }:
            raise ValueError("terminal tool calls require a completion time")
        if self.duration_ms is not None and (
            type(self.duration_ms) is not int or self.duration_ms < 0
        ):
            raise ValueError("tool call duration must be a nonnegative integer")
        digests = tuple(self.artifact_digests)
        if digests != tuple(sorted(set(digests))):
            raise ValueError("tool call artifact digests must be unique and sorted")
        for digest in digests:
            validate_artifact_digest(digest)
        object.__setattr__(self, "artifact_digests", digests)
        for identifier, name in (
            (self.correlation_id, "tool call correlation identifier"),
            (self.operation_intent_id, "tool call operation identifier"),
        ):
            if identifier is not None and (not isinstance(identifier, UUID) or identifier.int == 0):
                raise ValueError(f"{name} must be a non-nil UUID")
        if self.policy_version is not None and (
            type(self.policy_version) is not int or self.policy_version < 1
        ):
            raise ValueError("tool call policy version must be positive")

        binding = (self.request_digest, self.resource_id, self.invocation_schema_version)
        if any(v is not None for v in binding):
            if not all(v is not None for v in binding):
                raise ValueError("tool call invocation binding fields must all be present or all absent")
            if type(self.invocation_schema_version) is not int or self.invocation_schema_version != 1:
                raise ValueError("tool call invocation schema version must be 1")
            if not isinstance(self.request_digest, str) or _HEX_64.fullmatch(self.request_digest) is None:
                raise ValueError("tool call request digest must be lowercase hexadecimal SHA-256")
            if not isinstance(self.resource_id, str):
                raise TypeError("tool call resource identifier must be a string")
            if not self.resource_id or not self.resource_id.strip() or len(self.resource_id) > 512:
                raise ValueError(
                    "tool call resource identifier must be nonblank and at most 512 characters"
                )
            if any(
                ord(c) < 32 or c == "\x7f" or unicodedata.category(c).startswith("C")
                for c in self.resource_id
            ):
                raise ValueError("tool call resource identifier cannot contain control characters")

        if self.result_metadata is not None:
            for key, field_val in (
                ("request_digest", self.request_digest),
                ("resource_id", self.resource_id),
                ("invocation_schema_version", self.invocation_schema_version),
            ):
                if key in self.result_metadata:
                    meta_val = self.result_metadata[key]
                    if field_val is None:
                        if meta_val is not None:
                            raise ValueError(
                                f"result metadata {key} cannot be present when record field is absent"
                            )
                    elif (
                        key == "invocation_schema_version"
                        and type(meta_val) is not int
                    ) or meta_val != field_val:
                        raise ValueError(f"result metadata {key} conflicts with record field")

            meta_present_keys = [
                k
                for k in ("request_digest", "resource_id", "invocation_schema_version")
                if self.result_metadata.get(k) is not None
            ]
            if 0 < len(meta_present_keys) < 3:
                raise ValueError("result metadata cannot contain partial invocation binding fields")

    @property
    def result(self) -> Mapping[str, object] | None:
        """Compatibility alias for callers naming result metadata ``result``."""

        return self.result_metadata


# A descriptive alias used by some application callers.
ToolCallEvidence = ToolCallRecord


class ToolCallRepository(Protocol):
    """Durable tool-call evidence in the caller's transaction."""

    async def reserve(self, record: ToolCallRecord) -> ToolCallRecord: ...

    async def finalize(self, record: ToolCallRecord) -> ToolCallRecord: ...

    async def record(self, record: ToolCallRecord) -> ToolCallRecord: ...

    async def get(self, tool_call_id: UUID) -> ToolCallRecord: ...

    async def find(self, tool_call_id: UUID) -> ToolCallRecord | None: ...

    async def list_for_run(self, run_id: UUID) -> Sequence[ToolCallRecord]: ...

    async def validate_execution_context(
        self,
        run_id: UUID,
        agent_execution_id: UUID,
        step_id: UUID,
        role: AgentRole,
    ) -> bool: ...

    async def count_for_execution(self, agent_execution_id: UUID) -> int: ...


__all__ = [
    "ToolAuthorizationDenied",
    "ToolAuthorizerPort",
    "ToolCallEvidence",
    "ToolCallRecord",
    "ToolCallRepository",
]
