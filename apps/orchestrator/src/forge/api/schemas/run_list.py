"""Closed schemas for the authenticated run list projection."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from forge.api.schemas.projections import ProjectionModel


class RunListPullRequest(ProjectionModel):
    number: int
    repository: str
    head_sha: str


class RunListCurrencyCost(ProjectionModel):
    currency: str
    known_cost_minor: int
    unpriced_calls: int


class RunListCostSummary(ProjectionModel):
    currencies: list[RunListCurrencyCost]
    unpriced_calls: int


class RunListItem(ProjectionModel):
    task_id: UUID
    task_title: str
    project_id: UUID
    project_name: str
    run_id: UUID
    state: str
    version: int
    pending_gate: Literal["plan", "pr", "merge"] | None
    next_gate: Literal["plan", "pr", "merge"] | None
    attention_required: bool
    local_remediation_count: int
    remote_remediation_count: int
    created_at: datetime
    updated_at: datetime
    elapsed_ms: int
    elapsed_seconds: float
    pull_request: RunListPullRequest | None
    cost_summary: RunListCostSummary


class RunListPage(ProjectionModel):
    items: list[RunListItem]
    offset: int
    limit: int
    truncated: bool
