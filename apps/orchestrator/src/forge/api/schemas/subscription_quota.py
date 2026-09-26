"""Closed operator schemas for provider quota inspection and reporting."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from forge.application.services.subscription_quota import QuotaExhaustionReport


class QuotaStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    account: str
    pool: str
    status: str
    revision: int
    observed_at: datetime | None
    reason: str | None
    reset_at: datetime | None
    next_eligible_at: datetime | None
    retry_basis: str | None
    probe_attempt_id: UUID | None
    recovered_at: datetime | None


class QuotaExhaustionReportRequest(QuotaExhaustionReport):
    """HTTP alias for the application-owned report command."""


__all__ = ["QuotaExhaustionReportRequest", "QuotaStatusResponse"]
