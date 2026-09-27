"""Safe, versioned Jev operator report."""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class JevReportResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    run_id: UUID
    requested_mode: Literal["off", "shadow", "on"]
    effective_mode: Literal["off", "shadow", "on", "not_yet_observed"]
    requested_model: str | None
    actual_model: str | None
    calls: int
    attempts: int
    cache_hits: int
    unknown: int
    actual_input_units: int
    actual_output_units: int
    reserved_input_units: int
    duration_ms: int
    remaining_requests: int
    remaining_input_units: int
    by_kind: dict[str, int]
    by_status: dict[str, int]
    by_diagnostic: dict[str, int] = Field(default_factory=dict)
    review_focus_available: bool
    availability: Literal["off", "not_enabled", "no_samples", "sampled", "degraded"]

    @classmethod
    def from_report(cls, value: dict[str, Any]) -> JevReportResponse:
        return cls.model_validate(value)
