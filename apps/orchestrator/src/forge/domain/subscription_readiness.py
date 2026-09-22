"""Safe, versioned worker readiness snapshots.

These records are deliberately diagnostic: they never confer runtime authority.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from forge.domain.subscription import RouteSpec


class ReadinessReason(StrEnum):
    READY = "ready"
    OPERATOR_TRUSTED = "operator_trusted"
    MISSING_EXECUTABLE = "missing_executable"
    EXECUTABLE_DIGEST_MISMATCH = "executable_digest_mismatch"
    VERSION_MISMATCH = "version_mismatch"
    UNSUPPORTED_MODEL_OR_EFFORT = "unsupported_model_or_effort"
    SIGNED_OUT = "signed_out"
    ACCOUNT_AUTHENTICATION_UNPROVED = "account_authentication_unproved"
    SUBSCRIPTION_ROUTE_UNBOUND = "subscription_route_unbound"
    ISOLATION_UNPROVED = "isolation_unproved"
    EVIDENCE_MISSING = "evidence_missing"
    EVIDENCE_STALE_OR_INVALID = "evidence_stale_or_invalid"
    PROVIDER_UNSUPPORTED = "provider_unsupported"
    CONFIGURATION_INVALID = "configuration_invalid"
    UNKNOWN = "unknown"
    STALE_WORKER = "stale_worker"
    QUOTA_EXHAUSTED = "quota_exhausted"


class ReadinessQuota(StrEnum):
    BLOCKED = "blocked"
    UNKNOWN = "unknown"
    ELIGIBLE = "eligible"


class ReadinessWarning(StrEnum):
    APPROVED_TOOLS_UNPROVED = "approved_tools_unproved"
    OPERATOR_TRUSTED = "operator_trusted"
    CLIENT_BUILD_CHANGED = "client_build_changed"


@dataclass(frozen=True, slots=True)
class EvidenceReference:
    scope: str
    evidence_id: str
    revision: int
    observed_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class SubscriptionRouteReadiness:
    """A persistence-safe projection, with no paths, identities, or proof bytes."""

    route: RouteSpec
    configured: bool
    admitted: bool
    reason: ReadinessReason
    quota: ReadinessQuota = ReadinessQuota.UNKNOWN
    evidence: tuple[EvidenceReference, ...] = field(default=())
    quota_revision: int | None = None
    quota_reset_at: datetime | None = None
    quota_next_probe_at: datetime | None = None
    warnings: tuple[ReadinessWarning, ...] = field(default=())

    def __post_init__(self) -> None:
        if type(self.warnings) is not tuple or any(
            not isinstance(warning, ReadinessWarning) for warning in self.warnings
        ):
            raise TypeError("subscription readiness warnings are invalid")
        if len(set(self.warnings)) != len(self.warnings):
            raise ValueError("subscription readiness warnings must be unique")
        if (
            self.reason is ReadinessReason.OPERATOR_TRUSTED
            and ReadinessWarning.OPERATOR_TRUSTED not in self.warnings
        ):
            object.__setattr__(
                self, "warnings", (*self.warnings, ReadinessWarning.OPERATOR_TRUSTED)
            )
        if (
            self.route.client == "antigravity_cli"
            and ReadinessWarning.APPROVED_TOOLS_UNPROVED not in self.warnings
        ):
            object.__setattr__(
                self, "warnings", (*self.warnings, ReadinessWarning.APPROVED_TOOLS_UNPROVED)
            )

    def wire(self) -> dict[str, object]:
        return {
            "schema_version": 2,
            "provider": self.route.provider,
            "client": self.route.client,
            "model": self.route.model,
            "effort": self.route.effort.value,
            "auth_mode": self.route.auth_mode.value,
            "billing_mode": self.route.billing_mode.value,
            "configured": self.configured,
            "admitted": self.admitted,
            "reason": self.reason.value,
            "quota": self.quota.value,
            "evidence": [
                {
                    "scope": value.scope,
                    "evidence_id": value.evidence_id,
                    "revision": value.revision,
                    "observed_at": value.observed_at.isoformat(),
                    "expires_at": value.expires_at.isoformat(),
                }
                for value in self.evidence
            ],
            "quota_revision": self.quota_revision,
            "quota_reset_at": None
            if self.quota_reset_at is None
            else self.quota_reset_at.isoformat(),
            "quota_next_probe_at": None
            if self.quota_next_probe_at is None
            else self.quota_next_probe_at.isoformat(),
            "warnings": [warning.value for warning in self.warnings],
        }


__all__ = [
    "EvidenceReference",
    "ReadinessQuota",
    "ReadinessReason",
    "ReadinessWarning",
    "SubscriptionRouteReadiness",
]
