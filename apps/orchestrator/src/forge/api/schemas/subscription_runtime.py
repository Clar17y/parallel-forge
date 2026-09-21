"""Bounded diagnostic snapshots, separate from quota and capability approval."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field

from forge.api.schemas.subscription_tasks import ProjectionModel


class SubscriptionRuntimeRouteView(ProjectionModel):
    schema_version: Literal[1, 2]
    provider: str
    client: str
    model: str
    effort: str
    auth_mode: str
    billing_mode: str
    configured: bool
    admitted: bool
    reason: Literal[
        "ready",
        "missing_executable",
        "executable_digest_mismatch",
        "version_mismatch",
        "unsupported_model_or_effort",
        "signed_out",
        "account_authentication_unproved",
        "subscription_route_unbound",
        "isolation_unproved",
        "evidence_missing",
        "evidence_stale_or_invalid",
        "provider_unsupported",
        "configuration_invalid",
        "unknown",
    ]
    effective_reason: Literal[
        "ready",
        "missing_executable",
        "executable_digest_mismatch",
        "version_mismatch",
        "unsupported_model_or_effort",
        "signed_out",
        "account_authentication_unproved",
        "subscription_route_unbound",
        "isolation_unproved",
        "evidence_missing",
        "evidence_stale_or_invalid",
        "provider_unsupported",
        "configuration_invalid",
        "unknown",
        "stale_worker",
        "quota_exhausted",
    ]
    quota: Literal["blocked", "unknown", "eligible"]
    evidence: list[EvidenceReferenceView] = Field(default_factory=list, max_length=32)
    quota_revision: int | None = Field(default=None, ge=0)
    quota_reset_at: datetime | None = None
    quota_next_probe_at: datetime | None = None


class EvidenceReferenceView(ProjectionModel):
    scope: str
    evidence_id: UUID
    revision: int = Field(ge=1)
    observed_at: datetime
    expires_at: datetime


class SubscriptionWorkerStatusView(ProjectionModel):
    worker_instance_id: UUID
    last_seen_at: datetime
    stopped_at: datetime | None
    state: Literal["current", "stale", "stopped"]
    routes: list[SubscriptionRuntimeRouteView] = Field(max_length=64)


class SubscriptionRuntimeStatusPage(ProjectionModel):
    observed_at: datetime
    fresh_for_seconds: int = Field(gt=0)
    workers: list[SubscriptionWorkerStatusView] = Field(max_length=100)
    has_more: bool
