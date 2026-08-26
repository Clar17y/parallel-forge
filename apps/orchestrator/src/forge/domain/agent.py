"""Immutable structured agent domain contracts and role specifications."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from forge.domain.actor import AgentRole
from forge.domain.paths import normalize_policy_paths
from forge.domain.payload import validate_durable_payload
from forge.domain.plan import PlanOutput
from forge.domain.policy import (
    AgentModelPolicy,
    ProjectPolicy,
    RunnerMode,
)
from forge.domain.review import FindingSeverity, ReviewFinding
from forge.domain.tool import ToolName
from forge.observability.usage import UsageRecord

_DIGEST = re.compile(r"\A[0-9a-f]{64}\Z", re.ASCII)
_COMMIT_SHA = re.compile(r"\A[0-9a-f]{40}\Z", re.ASCII)
_WORKTREE_ID = re.compile(r"\A[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\Z")

_MAX_TEXT_LENGTH = 10_000
_MAX_ITEM_LENGTH = 5_000
_MAX_COLLECTION_SIZE = 100
_MAX_CONTENT_BYTES = 1_048_576
_MAX_CONTEXT_BYTES = 4_194_304
_MAX_REPOSITORY_PATH_LENGTH = 4_096

_ALLOWED_ROLE_TOOLS: dict[AgentRole, frozenset[ToolName]] = {
    AgentRole.PLANNER: frozenset(
        {
            ToolName.REPOSITORY_LIST_FILES,
            ToolName.REPOSITORY_READ_FILE,
            ToolName.REPOSITORY_SEARCH,
            ToolName.REPOSITORY_READ_INSTRUCTIONS,
        }
    ),
    AgentRole.DEVELOPER: frozenset(
        {
            ToolName.REPOSITORY_LIST_FILES,
            ToolName.REPOSITORY_READ_FILE,
            ToolName.REPOSITORY_SEARCH,
            ToolName.REPOSITORY_READ_INSTRUCTIONS,
            ToolName.REPOSITORY_WRITE_FILE,
            ToolName.GIT_STATUS,
            ToolName.GIT_DIFF,
            ToolName.GIT_COMMIT,
            ToolName.BUILD_RUN_NAMED_CHECK,
        }
    ),
    AgentRole.REVIEWER: frozenset(
        {
            ToolName.REPOSITORY_LIST_FILES,
            ToolName.REPOSITORY_READ_FILE,
            ToolName.REPOSITORY_SEARCH,
            ToolName.REPOSITORY_READ_INSTRUCTIONS,
            ToolName.GIT_STATUS,
            ToolName.GIT_DIFF,
            ToolName.VALIDATION_RESULTS_READ,
            ToolName.REVIEW_ARTIFACTS_READ,
        }
    ),
}


def _validate_non_nil_uuid(value: UUID, field_name: str) -> UUID:
    if not isinstance(value, UUID):
        raise TypeError(f"{field_name} must be a UUID")
    if value.int == 0:
        raise ValueError(f"{field_name} must not be a nil UUID")
    return value


def _validate_non_blank_text(
    value: str, field_name: str, *, max_length: int = _MAX_TEXT_LENGTH
) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if not value or not value.strip():
        raise ValueError(f"{field_name} must not be blank")
    if len(value) > max_length:
        raise ValueError(f"{field_name} exceeds maximum length of {max_length}")
    return value


def _validate_sha256_digest(value: str, field_name: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a canonical lowercase SHA-256 digest")
    return value


def _validate_commit_sha(value: str, field_name: str) -> str:
    if not isinstance(value, str) or _COMMIT_SHA.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a canonical lowercase SHA-1 commit ID")
    return value


def _validate_string_tuple(
    values: Sequence[str],
    field_name: str,
    *,
    reject_duplicates: bool = True,
    max_length: int = _MAX_ITEM_LENGTH,
    max_items: int = _MAX_COLLECTION_SIZE,
) -> tuple[str, ...]:
    if not isinstance(values, (tuple, list)):
        raise TypeError(f"{field_name} must be a sequence of strings")
    if len(values) > max_items:
        raise ValueError(f"{field_name} exceeds maximum count of {max_items}")
    items: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(values):
        cleaned = _validate_non_blank_text(item, f"{field_name}[{index}]", max_length=max_length)
        if reject_duplicates:
            if cleaned in seen:
                raise ValueError(f"{field_name} must not contain duplicate entries")
            seen.add(cleaned)
        items.append(cleaned)
    return tuple(items)


class UntrustedSourceKind(StrEnum):
    """Closed source classification for untrusted text envelopes."""

    TASK = "task"
    ISSUE = "issue"
    INSTRUCTION = "instruction"
    REPOSITORY_TREE = "repository_tree"
    DIFF = "diff"
    CHECK = "check"
    REVIEW = "review"
    REPOSITORY = "repository"


class UntrustedContent(BaseModel):
    """Immutable envelope encapsulating untrusted prose with cryptographic proof."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_kind: UntrustedSourceKind
    source_reference: str = Field(min_length=1)
    content: str = Field(min_length=1)
    content_digest: str = Field(min_length=64, max_length=64)
    original_byte_count: int = Field(ge=0)
    truncated: bool = False

    @field_validator("source_reference")
    @classmethod
    def validate_source_reference(cls, value: str) -> str:
        return _validate_non_blank_text(value, "source_reference", max_length=1024)

    @field_validator("content_digest")
    @classmethod
    def validate_digest_format(cls, value: str) -> str:
        return _validate_sha256_digest(value, "content_digest")

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("content must not be blank")
        if len(value.encode("utf-8")) > _MAX_CONTENT_BYTES:
            raise ValueError("content exceeds maximum byte size of 1048576")
        return value

    @model_validator(mode="after")
    def validate_content_integrity(self) -> Self:
        utf8_bytes = self.content.encode("utf-8")
        actual_byte_count = len(utf8_bytes)
        expected_digest = hashlib.sha256(utf8_bytes).hexdigest()
        if self.content_digest != expected_digest:
            raise ValueError("content_digest does not match UTF-8 content hash")
        if self.truncated:
            if self.original_byte_count <= actual_byte_count:
                raise ValueError(
                    "truncated content requires original_byte_count strictly greater than content byte count"
                )
        else:
            if self.original_byte_count != actual_byte_count:
                raise ValueError(
                    "untruncated content requires original_byte_count equal to content byte count"
                )
        return self

    @classmethod
    def from_text(
        cls,
        content: str,
        *,
        source_kind: UntrustedSourceKind | str,
        source_reference: str,
        original_byte_count: int | None = None,
        truncated: bool = False,
    ) -> UntrustedContent:
        if not isinstance(content, str):
            raise TypeError("content must be a string")
        utf8_bytes = content.encode("utf-8")
        actual_byte_count = len(utf8_bytes)
        digest = hashlib.sha256(utf8_bytes).hexdigest()
        if original_byte_count is None:
            if truncated:
                raise ValueError("truncated content requires an explicit original_byte_count")
            original_byte_count = actual_byte_count
        kind = UntrustedSourceKind(source_kind) if isinstance(source_kind, str) else source_kind
        return cls(
            source_kind=kind,
            source_reference=source_reference,
            content=content,
            content_digest=digest,
            original_byte_count=original_byte_count,
            truncated=truncated,
        )


class PolicySummary(BaseModel):
    """Non-secret, immutable summary of the active project execution policy."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    policy_id: UUID
    policy_version: int = Field(ge=1)
    runner_mode: RunnerMode = RunnerMode.DOCKER
    trusted_project: bool = False
    required_checks: tuple[str, ...] = Field(default=())
    allowed_merge_methods: tuple[str, ...] = Field(default=("squash",))
    publication_blocking_severities: tuple[FindingSeverity, ...] = Field(
        default=(FindingSeverity.BLOCKER, FindingSeverity.MAJOR)
    )
    merge_blocking_severities: tuple[FindingSeverity, ...] = Field(
        default=(FindingSeverity.BLOCKER, FindingSeverity.MAJOR)
    )

    @field_validator("policy_id")
    @classmethod
    def validate_policy_id(cls, value: UUID) -> UUID:
        return _validate_non_nil_uuid(value, "policy_id")

    @field_validator("required_checks")
    @classmethod
    def validate_checks(cls, value: Sequence[str]) -> tuple[str, ...]:
        return _validate_string_tuple(value, "required_checks", reject_duplicates=True)

    @field_validator("allowed_merge_methods")
    @classmethod
    def validate_merge_methods(cls, value: Sequence[str]) -> tuple[str, ...]:
        return _validate_string_tuple(value, "allowed_merge_methods", reject_duplicates=True)

    @classmethod
    def from_policy(cls, policy: ProjectPolicy) -> Self:
        if not isinstance(policy, ProjectPolicy):
            raise TypeError("policy must be a ProjectPolicy")
        return cls(
            policy_id=policy.id,
            policy_version=policy.version,
            runner_mode=policy.runner_mode,
            trusted_project=policy.trusted_project,
            required_checks=tuple(cmd.name for cmd in policy.required_checks),
            allowed_merge_methods=policy.allowed_merge_methods,
            publication_blocking_severities=tuple(
                sorted(policy.publication_blocking_severities, key=lambda s: s.value)
            ),
            merge_blocking_severities=tuple(
                sorted(policy.merge_blocking_severities, key=lambda s: s.value)
            ),
        )


class PlannerInput(BaseModel):
    """Structured input contract for the Planner role."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    original_task: UntrustedContent
    base_commit: str = Field(min_length=40, max_length=40)
    repository_tree: UntrustedContent
    relevant_instructions: tuple[UntrustedContent, ...] = Field(
        default=(), max_length=_MAX_COLLECTION_SIZE
    )
    policy_summary: PolicySummary

    @field_validator("base_commit")
    @classmethod
    def validate_base_commit(cls, value: str) -> str:
        return _validate_commit_sha(value, "base_commit")

    @model_validator(mode="after")
    def validate_context_size(self) -> Self:
        _validate_context_size(
            (self.original_task, self.repository_tree, *self.relevant_instructions)
        )
        return self


class DeveloperInput(BaseModel):
    """Structured input contract for the Developer role."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    original_task: UntrustedContent
    plan: PlanOutput
    worktree_id: str = Field(min_length=1, max_length=128)
    base_commit: str = Field(min_length=40, max_length=40)
    remediation_findings: tuple[ReviewFinding, ...] = Field(
        default=(), max_length=_MAX_COLLECTION_SIZE
    )
    relevant_instructions: tuple[UntrustedContent, ...] = Field(
        default=(), max_length=_MAX_COLLECTION_SIZE
    )

    @field_validator("worktree_id")
    @classmethod
    def validate_worktree_id(cls, value: str) -> str:
        if not _WORKTREE_ID.fullmatch(value):
            raise ValueError("worktree_id is invalid")
        return value

    @field_validator("base_commit")
    @classmethod
    def validate_base_commit(cls, value: str) -> str:
        return _validate_commit_sha(value, "base_commit")

    @field_validator("remediation_findings")
    @classmethod
    def validate_findings(cls, value: Sequence[ReviewFinding]) -> tuple[ReviewFinding, ...]:
        if len(value) > _MAX_COLLECTION_SIZE:
            raise ValueError(
                f"remediation_findings exceeds maximum count of {_MAX_COLLECTION_SIZE}"
            )
        seen: set[str] = set()
        for finding in value:
            if not isinstance(finding, ReviewFinding):
                raise TypeError("remediation findings must be ReviewFinding instances")
            if finding.finding_id in seen:
                raise ValueError("duplicate finding_id in remediation_findings")
            seen.add(finding.finding_id)
        return tuple(value)

    @model_validator(mode="after")
    def validate_context_size(self) -> Self:
        _validate_context_size((self.original_task, *self.relevant_instructions))
        return self


class ReviewerInput(BaseModel):
    """Structured input contract for the Reviewer role."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    original_task: UntrustedContent
    plan: PlanOutput
    current_diff: UntrustedContent
    check_evidence: tuple[UntrustedContent, ...] = Field(
        default=(), max_length=_MAX_COLLECTION_SIZE
    )
    relevant_instructions: tuple[UntrustedContent, ...] = Field(
        default=(), max_length=_MAX_COLLECTION_SIZE
    )

    @model_validator(mode="after")
    def validate_context_size(self) -> Self:
        _validate_context_size(
            (
                self.original_task,
                self.current_diff,
                *self.check_evidence,
                *self.relevant_instructions,
            )
        )
        return self


class DeveloperOutput(BaseModel):
    """Structured output contract produced by the Developer role."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    summary: str = Field(min_length=1)
    changed_paths: tuple[str, ...] = Field(max_length=_MAX_COLLECTION_SIZE)
    tests_added_or_changed: tuple[str, ...] = Field(max_length=_MAX_COLLECTION_SIZE)
    named_checks_run: tuple[str, ...]
    local_commit_sha: str = Field(min_length=40, max_length=40)
    diff_digest: str = Field(min_length=64, max_length=64)
    unresolved_concerns: tuple[str, ...]
    plan_deviations: tuple[str, ...]

    @field_validator("summary")
    @classmethod
    def validate_summary(cls, value: str) -> str:
        return _validate_non_blank_text(value, "summary")

    @field_validator("changed_paths", "tests_added_or_changed")
    @classmethod
    def validate_repository_paths(cls, value: Sequence[str]) -> tuple[str, ...]:
        if not isinstance(value, (tuple, list)):
            raise TypeError("repository paths must be a sequence of strings")
        if len(value) > _MAX_COLLECTION_SIZE:
            raise ValueError(f"repository paths exceed maximum count of {_MAX_COLLECTION_SIZE}")
        for index, path in enumerate(value):
            _validate_non_blank_text(
                path,
                f"repository paths[{index}]",
                max_length=_MAX_REPOSITORY_PATH_LENGTH,
            )
        return normalize_policy_paths(value)

    @field_validator("named_checks_run")
    @classmethod
    def validate_named_checks(cls, value: Sequence[str]) -> tuple[str, ...]:
        return _validate_string_tuple(value, "named_checks_run", reject_duplicates=True)

    @field_validator("local_commit_sha")
    @classmethod
    def validate_commit_sha(cls, value: str) -> str:
        return _validate_commit_sha(value, "local_commit_sha")

    @field_validator("diff_digest")
    @classmethod
    def validate_diff_digest(cls, value: str) -> str:
        return _validate_sha256_digest(value, "diff_digest")

    @field_validator("unresolved_concerns")
    @classmethod
    def validate_concerns(cls, value: Sequence[str]) -> tuple[str, ...]:
        return _validate_string_tuple(value, "unresolved_concerns", reject_duplicates=True)

    @field_validator("plan_deviations")
    @classmethod
    def validate_deviations(cls, value: Sequence[str]) -> tuple[str, ...]:
        return _validate_string_tuple(value, "plan_deviations", reject_duplicates=True)


class ReviewDecision(StrEnum):
    """Closed decision values for Reviewer evaluations."""

    APPROVE = "approve"
    REQUEST_CHANGES = "request_changes"
    BLOCKED = "blocked"


class ReviewOutput(BaseModel):
    """Structured output contract produced by the Reviewer role."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    decision: ReviewDecision
    findings: tuple[ReviewFinding, ...] = Field(default=(), max_length=_MAX_COLLECTION_SIZE)
    tested_claims: tuple[str, ...]
    missing_evidence: tuple[str, ...]
    summary: str = Field(min_length=1)

    @field_validator("summary")
    @classmethod
    def validate_summary(cls, value: str) -> str:
        return _validate_non_blank_text(value, "summary")

    @field_validator("findings")
    @classmethod
    def validate_findings(cls, value: Sequence[ReviewFinding]) -> tuple[ReviewFinding, ...]:
        if len(value) > _MAX_COLLECTION_SIZE:
            raise ValueError(f"findings exceeds maximum count of {_MAX_COLLECTION_SIZE}")
        seen: set[str] = set()
        for finding in value:
            if not isinstance(finding, ReviewFinding):
                raise TypeError("findings must be ReviewFinding instances")
            if finding.finding_id in seen:
                raise ValueError("duplicate finding_id in review findings")
            seen.add(finding.finding_id)
        return tuple(value)

    @field_validator("tested_claims")
    @classmethod
    def validate_tested_claims(cls, value: Sequence[str]) -> tuple[str, ...]:
        return _validate_string_tuple(value, "tested_claims", reject_duplicates=True)

    @field_validator("missing_evidence")
    @classmethod
    def validate_missing_evidence(cls, value: Sequence[str]) -> tuple[str, ...]:
        return _validate_string_tuple(value, "missing_evidence", reject_duplicates=True)

    @model_validator(mode="after")
    def validate_decision_rules(self) -> Self:
        has_unresolved_blocker_or_major = any(
            not f.is_resolved and f.severity in {FindingSeverity.BLOCKER, FindingSeverity.MAJOR}
            for f in self.findings
        )
        has_unresolved_finding = any(not f.is_resolved for f in self.findings)
        has_missing_evidence = len(self.missing_evidence) > 0

        if self.decision == ReviewDecision.APPROVE:
            if has_unresolved_blocker_or_major:
                raise ValueError(
                    "approval is invalid when unresolved blocker or major findings remain"
                )
            if has_missing_evidence:
                raise ValueError("approval is invalid when missing evidence remains")
        elif self.decision == ReviewDecision.REQUEST_CHANGES:
            if not (has_unresolved_finding or has_missing_evidence):
                raise ValueError(
                    "request_changes requires at least one unresolved finding or missing evidence"
                )
        elif self.decision == ReviewDecision.BLOCKED:
            if not has_missing_evidence:
                raise ValueError("blocked decision requires a non-empty missing_evidence entry")
        return self


class AgentBudget(BaseModel):
    """Budget and execution limits for an agent execution."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_input_tokens: int = Field(default=100_000, ge=1, strict=True)
    max_output_tokens: int = Field(default=16_000, ge=1, strict=True)
    max_tool_calls: int = Field(default=100, ge=0, strict=True)
    max_duration_seconds: int = Field(default=1800, ge=1, strict=True)
    max_cost_minor: int = Field(default=1000, ge=0, strict=True)

    @classmethod
    def from_model_policy(cls, policy: AgentModelPolicy) -> Self:
        if not isinstance(policy, AgentModelPolicy):
            raise TypeError("policy must be an AgentModelPolicy")
        return cls(
            max_input_tokens=policy.max_input_tokens,
            max_output_tokens=policy.max_output_tokens,
            max_tool_calls=policy.max_tool_calls,
            max_duration_seconds=policy.max_duration_seconds,
            max_cost_minor=policy.max_cost_minor,
        )


class AgentRequest(BaseModel):
    """Immutable execution request sent to an agent gateway."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    execution_id: UUID
    run_id: UUID
    task_id: UUID
    role: AgentRole
    context: PlannerInput | DeveloperInput | ReviewerInput
    parent_execution_id: UUID | None = None
    provider: str = Field(min_length=1, max_length=96)
    model: str = Field(min_length=1, max_length=255)
    instruction_version: str = Field(min_length=1, max_length=96)
    system_instruction: str = Field(min_length=1)
    instruction_digest: str = Field(min_length=64, max_length=64)
    allowed_tools: tuple[ToolName, ...]
    budget: AgentBudget

    @field_validator("execution_id", "run_id", "task_id")
    @classmethod
    def validate_uuids(cls, value: UUID) -> UUID:
        return _validate_non_nil_uuid(value, "request identifier")

    @field_validator("parent_execution_id")
    @classmethod
    def validate_parent_uuid(cls, value: UUID | None) -> UUID | None:
        if value is not None:
            return _validate_non_nil_uuid(value, "parent_execution_id")
        return value

    @field_validator("provider", "model", "instruction_version")
    @classmethod
    def validate_metadata_text(cls, value: str) -> str:
        return _validate_non_blank_text(value, "metadata field", max_length=255)

    @field_validator("instruction_digest")
    @classmethod
    def validate_instruction_digest(cls, value: str) -> str:
        return _validate_sha256_digest(value, "instruction_digest")

    @field_validator("system_instruction")
    @classmethod
    def validate_system_instruction(cls, value: str) -> str:
        return _validate_non_blank_text(value, "system_instruction")

    @field_validator("allowed_tools")
    @classmethod
    def validate_tools_sequence(cls, value: Sequence[ToolName]) -> tuple[ToolName, ...]:
        seen: set[ToolName] = set()
        for tool in value:
            if not isinstance(tool, ToolName):
                raise TypeError("allowed_tools entries must be ToolName values")
            if tool in seen:
                raise ValueError("allowed_tools must not contain duplicate entries")
            seen.add(tool)
        return tuple(value)

    @model_validator(mode="after")
    def validate_request_integrity(self) -> Self:
        expected_digest = hashlib.sha256(self.system_instruction.encode("utf-8")).hexdigest()
        if self.instruction_digest != expected_digest:
            raise ValueError("instruction_digest does not match system_instruction hash")

        if self.role == AgentRole.PLANNER and not isinstance(self.context, PlannerInput):
            raise ValueError(
                f"context type {type(self.context).__name__} does not match role {self.role.value}"
            )
        if self.role == AgentRole.DEVELOPER and not isinstance(self.context, DeveloperInput):
            raise ValueError(
                f"context type {type(self.context).__name__} does not match role {self.role.value}"
            )
        if self.role == AgentRole.REVIEWER and not isinstance(self.context, ReviewerInput):
            raise ValueError(
                f"context type {type(self.context).__name__} does not match role {self.role.value}"
            )

        if self.role == AgentRole.REVIEWER and self.parent_execution_id is not None:
            raise ValueError("reviewer executions must be fresh and have no parent_execution_id")

        permitted_tools = _ALLOWED_ROLE_TOOLS.get(self.role, frozenset())
        for tool in self.allowed_tools:
            if tool not in permitted_tools:
                raise ValueError(f"tool {tool.value} is not permitted for role {self.role.value}")

        for content in _iter_untrusted_content(self.context):
            if content.content and content.content in self.system_instruction:
                raise ValueError("system_instruction must not contain untrusted context content")

        _validate_context_size(_iter_untrusted_content(self.context))

        validate_durable_payload(self.model_dump(mode="json"))

        return self


class AgentFinishStatus(StrEnum):
    """Closed finish status for agent gateway executions."""

    SUCCEEDED = "succeeded"
    BUDGET_EXCEEDED = "budget_exceeded"
    INVALID_OUTPUT = "invalid_output"
    TIMED_OUT = "timed_out"
    TOOL_DENIED = "tool_denied"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AgentResult(BaseModel):
    """Immutable outcome produced by an agent gateway execution."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    execution_id: UUID
    role: AgentRole
    finish_status: AgentFinishStatus
    output: PlanOutput | DeveloperOutput | ReviewOutput | None
    parent_execution_id: UUID | None = None
    provider: str = Field(min_length=1, max_length=96)
    model: str = Field(min_length=1, max_length=255)
    instruction_digest: str = Field(min_length=64, max_length=64)
    usage: UsageRecord
    tool_call_count: int = Field(ge=0)
    duration_ms: int = Field(ge=0)

    @field_validator("execution_id")
    @classmethod
    def validate_execution_id(cls, value: UUID) -> UUID:
        return _validate_non_nil_uuid(value, "execution_id")

    @field_validator("parent_execution_id")
    @classmethod
    def validate_parent_execution_id(cls, value: UUID | None) -> UUID | None:
        if value is not None:
            return _validate_non_nil_uuid(value, "parent_execution_id")
        return value

    @field_validator("provider", "model")
    @classmethod
    def validate_metadata_text(cls, value: str) -> str:
        return _validate_non_blank_text(value, "metadata field", max_length=255)

    @field_validator("instruction_digest")
    @classmethod
    def validate_instruction_digest(cls, value: str) -> str:
        return _validate_sha256_digest(value, "instruction_digest")

    @model_validator(mode="after")
    def validate_result_invariants(self) -> Self:
        if self.role == AgentRole.REVIEWER and self.parent_execution_id is not None:
            raise ValueError("reviewer results must be fresh and have no parent_execution_id")

        if self.provider != self.usage.provider:
            raise ValueError("result provider must agree with usage provider")
        if self.model != self.usage.model:
            raise ValueError("result model must agree with usage model")
        if self.tool_call_count != self.usage.tool_call_count:
            raise ValueError("result tool_call_count must agree with usage tool_call_count")
        if self.duration_ms != self.usage.duration_ms:
            raise ValueError("result duration_ms must agree with usage duration_ms")

        if self.finish_status == AgentFinishStatus.SUCCEEDED:
            if self.output is None:
                raise ValueError("successful agent execution requires a non-null output")
            if self.role == AgentRole.PLANNER and not isinstance(self.output, PlanOutput):
                raise ValueError("planner result output must be a PlanOutput")
            if self.role == AgentRole.DEVELOPER and not isinstance(self.output, DeveloperOutput):
                raise ValueError("developer result output must be a DeveloperOutput")
            if self.role == AgentRole.REVIEWER and not isinstance(self.output, ReviewOutput):
                raise ValueError("reviewer result output must be a ReviewOutput")
        elif self.output is not None:
            if self.role == AgentRole.PLANNER and not isinstance(self.output, PlanOutput):
                raise ValueError("planner result output must be a PlanOutput")
            if self.role == AgentRole.DEVELOPER and not isinstance(self.output, DeveloperOutput):
                raise ValueError("developer result output must be a DeveloperOutput")
            if self.role == AgentRole.REVIEWER and not isinstance(self.output, ReviewOutput):
                raise ValueError("reviewer result output must be a ReviewOutput")

        validate_durable_payload(self.model_dump(mode="json"))

        return self


def _iter_untrusted_content(
    context: PlannerInput | DeveloperInput | ReviewerInput,
) -> tuple[UntrustedContent, ...]:
    if isinstance(context, PlannerInput):
        return (context.original_task, context.repository_tree, *context.relevant_instructions)
    if isinstance(context, DeveloperInput):
        return (context.original_task, *context.relevant_instructions)
    return (
        context.original_task,
        context.current_diff,
        *context.check_evidence,
        *context.relevant_instructions,
    )


def _validate_context_size(envelopes: Sequence[UntrustedContent]) -> None:
    total_bytes = sum(len(envelope.content.encode("utf-8")) for envelope in envelopes)
    if total_bytes > _MAX_CONTEXT_BYTES:
        raise ValueError("agent context exceeds maximum byte size of 4194304")


__all__ = [
    "AgentBudget",
    "AgentFinishStatus",
    "AgentRequest",
    "AgentResult",
    "DeveloperInput",
    "DeveloperOutput",
    "PlannerInput",
    "PolicySummary",
    "ReviewDecision",
    "ReviewOutput",
    "ReviewerInput",
    "UntrustedContent",
    "UntrustedSourceKind",
]
