"""Closed response items for bounded dashboard listings."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from forge.api.schemas.projections import ProjectionModel


class ListPage[T](ProjectionModel):
    items: list[T]
    offset: int
    limit: int
    truncated: bool


class PolicyLimits(ProjectionModel):
    local_remediation_limit: int
    remote_remediation_limit: int


class PolicyProjection(ProjectionModel):
    project_id: UUID
    version: int
    policy_digest: str
    runner_mode: str
    database_enabled: bool
    limits: PolicyLimits


class ApprovalItem(ProjectionModel):
    run_id: UUID
    task_id: UUID
    gate: Literal["plan", "pr", "merge"]
    evidence_digest: str
    run_version: int
    policy_version: int


class UsageItem(ProjectionModel):
    project_id: UUID
    run_id: UUID
    provider: str
    model: str
    currency: str
    input_tokens: int
    output_tokens: int
    duration_ms: int
    known_cost_minor: int
    unpriced_calls: int
    model_calls: int


class AuditOperation(ProjectionModel):
    id: UUID
    kind: str
    status: Literal["PENDING", "SUCCEEDED", "FAILED", "NEEDS_RECONCILIATION"]


class AuditEventEvidence(ProjectionModel):
    id: UUID
    source: Literal["operator", "run"]
    actor_class: str
    run_id: UUID | None
    project_id: UUID | None
    actor_id: UUID | None
    event_type: str
    subject_type: str
    subject_id: UUID | None
    created_at: datetime
    payload: dict[str, object]


class AuditItem(AuditEventEvidence):
    operations: list[AuditOperation]


class AgentItem(ProjectionModel):
    id: UUID
    run_id: UUID
    role: str
    provider: str
    model: str
    status: str
    instruction_version: str


class PermissionItem(ProjectionModel):
    role: str
    tools: list[str]


class EvaluationItem(ProjectionModel):
    suite_id: UUID
    suite_status: str
    fixture_version: str
    metric_version: str
    case_id: UUID | None
    case_key: str | None
    role: str | None
    status: str | None
    metrics: dict[str, object] | None
    model_usage_id: UUID | None
    prompt_version: str | None
    provider: str | None
    model: str | None
    input_tokens: int | None
    output_tokens: int | None
    duration_ms: int | None
    currency: str | None
    estimated_cost_minor: int | None
    input_artifact_digest: str | None
    output_artifact_digest: str | None
    created_at: datetime
    completed_at: datetime | None


class RunUsageItem(ProjectionModel):
    id: UUID
    agent_execution_id: UUID
    role: str
    provider: str
    model: str
    prompt_version: str
    instruction_digest: str | None
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    duration_ms: int
    tool_call_count: int
    pricing_version: str
    estimated_cost_minor: int | None
    currency: str
    unknown_price_reason: str | None
    created_at: datetime


class ApprovalHistoryItem(ProjectionModel):
    id: UUID
    gate: Literal["plan", "pr", "merge"]
    evidence_digest: str
    run_version: int
    policy_version: int
    authenticated_actor_id: UUID
    created_at: datetime
    invalidated_at: datetime | None
    invalidation_reason: str | None
