"""Current safe result of an explicitly authorized capability probe."""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from forge.persistence.models.base import Base

_REASONS = (
    "'ready','missing_executable','executable_digest_mismatch','version_mismatch',"
    "'unsupported_model_or_effort','signed_out','account_authentication_unproved',"
    "'subscription_route_unbound','isolation_unproved','evidence_stale_or_invalid',"
    "'configuration_invalid','unknown'"
)


class CapabilityProbeDiagnosticRecord(Base):
    __tablename__ = "capability_probe_diagnostics"
    __table_args__ = (
        CheckConstraint(
            "identity_digest ~ '^[0-9a-f]{64}$'",
            name="identity",
        ),
        CheckConstraint("revision >= 1", name="revision"),
        CheckConstraint(f"reason IN ({_REASONS})", name="reason"),
        CheckConstraint("expires_at > observed_at", name="window"),
    )

    identity_digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    reason: Mapped[str] = mapped_column(String(48), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


__all__ = ["CapabilityProbeDiagnosticRecord"]
