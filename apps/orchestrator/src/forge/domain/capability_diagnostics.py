"""Safe, expiring diagnostics from explicitly authorized capability probes."""

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from forge.domain.subscription_readiness import ReadinessReason

_DIGEST = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
CAPABILITY_DIAGNOSTIC_REASONS = frozenset(
    {
        ReadinessReason.READY,
        ReadinessReason.MISSING_EXECUTABLE,
        ReadinessReason.EXECUTABLE_DIGEST_MISMATCH,
        ReadinessReason.VERSION_MISMATCH,
        ReadinessReason.UNSUPPORTED_MODEL_OR_EFFORT,
        ReadinessReason.SIGNED_OUT,
        ReadinessReason.ACCOUNT_AUTHENTICATION_UNPROVED,
        ReadinessReason.SUBSCRIPTION_ROUTE_UNBOUND,
        ReadinessReason.ISOLATION_UNPROVED,
        ReadinessReason.EVIDENCE_STALE_OR_INVALID,
        ReadinessReason.CONFIGURATION_INVALID,
        ReadinessReason.UNKNOWN,
    }
)


@dataclass(frozen=True, slots=True, kw_only=True)
class CapabilityProbeDiagnostic:
    """One identity-bound reason; no path, account value, or raw output."""

    identity_digest: str
    reason: ReadinessReason
    revision: int
    observed_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        if type(self.identity_digest) is not str or _DIGEST.fullmatch(self.identity_digest) is None:
            raise ValueError("capability diagnostic identity is invalid")
        if self.reason not in CAPABILITY_DIAGNOSTIC_REASONS:
            raise ValueError("capability diagnostic reason is invalid")
        if type(self.revision) is not int or self.revision < 1:
            raise ValueError("capability diagnostic revision is invalid")
        for name in ("observed_at", "expires_at"):
            value = getattr(self, name)
            if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("capability diagnostic time is invalid")
            object.__setattr__(self, name, value.astimezone(UTC))
        if self.expires_at <= self.observed_at:
            raise ValueError("capability diagnostic time is invalid")


__all__ = ["CAPABILITY_DIAGNOSTIC_REASONS", "CapabilityProbeDiagnostic"]
