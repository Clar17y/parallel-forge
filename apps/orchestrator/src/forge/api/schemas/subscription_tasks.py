"""Closed operator projections; never expose raw provider or authority payloads."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from forge.api.schemas.subscription_quota import QuotaStatusResponse
from forge.domain.subscription_task_controls import (
    SubscriptionTaskControlRequest,
    TaskControlAction,
    TaskControlStatus,
)


class ProjectionModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TaskControlRequest(SubscriptionTaskControlRequest):
    """Parse JSON UUIDs while preserving strict optimistic-concurrency versions."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=False)
    expected_run_version: int = Field(ge=0, strict=True)
    expected_task_version: int = Field(ge=0, strict=True)


class AttemptRoute(ProjectionModel):
    provider: str
    client: str
    model: str
    effort: str
    auth_mode: str
    billing_mode: str


class SubscriptionTaskControlView(ProjectionModel):
    receipt_id: UUID
    action: TaskControlAction
    status: TaskControlStatus
    reason: str
    observed_at: datetime
    pause_receipt_id: UUID | None = None


class SubscriptionTaskView(ProjectionModel):
    task_id: UUID
    parent_task_id: UUID | None
    dependency_task_ids: list[UUID]
    purpose: str
    owned_paths: list[str]
    state: str
    pause_requested: bool
    cancel_requested: bool
    version: int
    repairs: int | None
    unsettled_effects: int
    control: SubscriptionTaskControlView | None = None
    quota_status: QuotaStatusResponse | None = None
    requested_route: AttemptRoute | None = None
    effective_route: AttemptRoute | None = None
    fallback_selected: bool = False
    capacity_waits: list[Literal["host", "run", "provider"]] = Field(
        default_factory=list, max_length=3
    )


class CapacityLimitView(ProjectionModel):
    active: int = Field(ge=0)
    limit: int = Field(ge=1)


class ProviderCapacityView(CapacityLimitView):
    provider: str


class SubscriptionCapacityView(ProjectionModel):
    observed_at: datetime
    policy_version: int = Field(ge=1)
    host: CapacityLimitView
    run: CapacityLimitView
    providers: list[ProviderCapacityView] = Field(max_length=100)
    queue_order: Literal["least_recently_served_run_then_oldest_task"]


class SubscriptionTaskPage(ProjectionModel):
    run_id: UUID
    subscription: bool
    run_version: int | None = None
    run_allows_execution: bool = False
    run_is_terminal: bool = False
    candidate_epoch: int | None = None
    candidate_state: str | None = None
    tasks: list[SubscriptionTaskView] = Field(max_length=100)
    has_more: bool
    quota_statuses: list[QuotaStatusResponse] = Field(default_factory=list, max_length=100)
    capacity: SubscriptionCapacityView | None = None


class SubscriptionAttemptView(ProjectionModel):
    attempt_id: UUID
    attempt_number: int
    state: str
    requested_route: AttemptRoute
    effective_route: AttemptRoute
    input_tokens: int | None
    output_tokens: int | None
    cached_tokens: int | None
    duration_ms: int | None
    tool_calls: int | None
    named_checks: int | None
    estimated_api_cost_minor: int | None
    currency: str | None
    quota_status: str


class SubscriptionAttemptPage(ProjectionModel):
    run_id: UUID
    task_id: UUID
    attempts: list[SubscriptionAttemptView] = Field(max_length=100)
    has_more: bool
