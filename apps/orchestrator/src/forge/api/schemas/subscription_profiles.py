"""Closed HTTP schemas for immutable operator subscription profiles."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict

from forge.application.services.subscription_profiles import (
    ProfileBody,
    ProfileVersionRequest,
    ProjectProfileSelectionRequest,
)
from forge.domain.subscription import OperatorProfile, RouteSpec


class ProfileCreateRequest(ProfileBody):
    pass


class ProfileAppendRequest(ProfileVersionRequest):
    pass


class ProjectProfileSelectRequest(ProjectProfileSelectionRequest):
    pass


class ProfileResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile_id: UUID
    version: int
    preferences: list[dict[str, object]]
    approved_mappings: list[dict[str, object]]
    default_billing_mode: str

    @classmethod
    def from_profile(cls, value: OperatorProfile) -> ProfileResponse:
        return cls.model_validate(
            {
                "profile_id": value.profile_id,
                "version": value.version,
                "preferences": [
                    {
                        "purpose": p.purpose.value,
                        "preferred_route": _route(p.preferred_route),
                        "fallback_routes": [_route(r) for r in p.fallback_routes],
                    }
                    for p in value.preferences
                ],
                "approved_mappings": [
                    {
                        "requested_model": m.requested_model,
                        "effective_model": m.effective_model,
                        "approved_by": m.approved_by,
                        "reason": m.reason,
                    }
                    for m in value.approved_mappings
                ],
                "default_billing_mode": value.default_billing_mode.value,
            }
        )


def _route(route: RouteSpec) -> dict[str, object]:
    return {
        "provider": route.provider,
        "client": route.client,
        "model": route.model,
        "effort": route.effort.value,
        "auth_mode": route.auth_mode.value,
        "billing_mode": route.billing_mode.value,
    }


__all__ = [
    "ProfileAppendRequest",
    "ProfileCreateRequest",
    "ProfileResponse",
    "ProjectProfileSelectRequest",
]
