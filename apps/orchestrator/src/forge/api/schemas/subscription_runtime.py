"""Bounded diagnostic snapshots, separate from quota and capability approval."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field

from forge.api.schemas.subscription_tasks import AttemptRoute, ProjectionModel


class SubscriptionWorkerStatusView(ProjectionModel):
    worker_instance_id: UUID
    last_seen_at: datetime
    stopped_at: datetime | None
    state: Literal["current", "stale", "stopped"]
    routes: list[AttemptRoute] = Field(max_length=64)


class SubscriptionRuntimeStatusPage(ProjectionModel):
    observed_at: datetime
    fresh_for_seconds: int = Field(gt=0)
    workers: list[SubscriptionWorkerStatusView] = Field(max_length=100)
    has_more: bool
