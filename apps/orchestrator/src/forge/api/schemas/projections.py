"""Closed cockpit sections; persistence internals are never serialized."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from forge.api.schemas.runs import RunResponse
from forge.domain.actor import AgentRole
from forge.domain.resource import ResourceState


class ProjectionModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DashboardSummary(ProjectionModel):
    total_runs: int
    runs: dict[str, int]


class TaskSection(ProjectionModel):
    id: UUID
    title: str
    body: str
    external_source: str | None
    source_url: str | None
    untrusted_external_content: bool


class ProjectSection(ProjectionModel):
    id: UUID
    name: str
    github_repository: str
    policy_version: int
    policy_digest: str


class ResourceSection(ProjectionModel):
    worktree_path: str | None
    branch_name: str | None
    database_state: ResourceState
    database_name: str | None


class PlanSection(ProjectionModel):
    output_artifact_digest: str | None
    approval_id: UUID | None
    approval_evidence_digest: str | None


class CandidateSection(ProjectionModel):
    commit: str | None
    pending_evidence_digest: str | None
    validation_evidence_digest: str | None
    review_evidence_digest: str | None


class PullRequestSection(ProjectionModel):
    number: int
    repository: str
    branch: str
    base_ref: str
    head_sha: str
    base_sha: str
    state: str
    merge_state: str | None


class CheckItem(ProjectionModel):
    id: UUID
    name: str
    command_name: str
    command_version: int
    status: str
    exit_code: int | None
    output_artifact_digest: str | None
    completed_at: datetime | None
    head_sha: str | None


class FindingItem(ProjectionModel):
    id: str
    severity: str
    path: str | None
    start_line: int | None
    summary: str
    evidence: str
    proposed_resolution: str | None
    status: str


class ReviewSection(ProjectionModel):
    execution_id: UUID | None
    evidence_digest: str | None
    head_sha: str | None
    findings: list[FindingItem]


class AgentSection(ProjectionModel):
    role: AgentRole
    provider: str
    model: str
    execution_id: UUID | None
    status: str | None
    instruction_version: str | None
    input_artifact_digest: str | None
    output_artifact_digest: str | None
    validation_evidence_set_id: UUID | None
    independent: bool | None
    allowed_tools: list[str]
    started_at: datetime | None = None
    completed_at: datetime | None = None
    usage: UsageSummary | None = None


class BudgetSection(ProjectionModel):
    local_remediation_count: int
    local_remediation_limit: int
    local_remediation_remaining: int
    remote_remediation_count: int
    remote_remediation_limit: int
    remote_remediation_remaining: int
    token_limit: int
    cost_limit_minor: int
    duration_limit_seconds: int


class CurrencyUsage(ProjectionModel):
    currency: str
    known_cost_minor: int
    unpriced_calls: int


class UsageSummary(ProjectionModel):
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    duration_ms: int
    tool_calls: int
    model_calls: int
    currencies: list[CurrencyUsage]


class SecuritySection(ProjectionModel):
    runner_mode: str
    trusted_project: bool
    database_enabled: bool


class EventItem(ProjectionModel):
    sequence: int
    event_type: str
    run_version: int
    actor_class: str
    occurred_at: datetime
    payload: dict[str, object]


class AvailableCommand(ProjectionModel):
    name: str
    expected_run_version: int
    requires_feedback: bool
    gate: Literal["plan", "pr", "merge"] | None = None
    evidence_digest: str | None = None
    policy_version: int | None = None


class RunProjection(ProjectionModel):
    run: RunResponse
    task: TaskSection
    project: ProjectSection
    resource: ResourceSection
    plan: PlanSection
    candidate: CandidateSection
    pull_request: PullRequestSection | None
    checks: list[CheckItem]
    review: ReviewSection
    agents: dict[str, AgentSection]
    budgets: BudgetSection
    usage: UsageSummary
    security: SecuritySection
    latest_events: list[EventItem]
    available_commands: list[AvailableCommand]
    next_gate: Literal["plan", "pr", "merge"] | None
