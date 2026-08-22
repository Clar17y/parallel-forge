"""Hashed operator credentials and single-use approval challenges."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from forge.persistence.models.base import Base


class OperatorSession(Base):
    """A bootstrap credential or authenticated local operator session."""

    __tablename__ = "operator_sessions"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_operator_sessions_token_hash"),
        CheckConstraint("credential_kind IN ('bootstrap','session')", name="credential_kind"),
        CheckConstraint(
            "(credential_kind = 'bootstrap' AND actor_id IS NULL AND csrf_hash IS NULL "
            "AND idle_expires_at IS NULL AND absolute_expires_at IS NULL "
            "AND revoked_at IS NULL) OR "
            "(credential_kind = 'session' AND actor_id IS NOT NULL AND csrf_hash IS NOT NULL "
            "AND idle_expires_at IS NOT NULL AND absolute_expires_at IS NOT NULL "
            "AND used_at IS NULL)",
            name="credential_shape",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    credential_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    actor_id: Mapped[UUID | None] = mapped_column(Uuid)
    csrf_hash: Mapped[str | None] = mapped_column(String(64))
    idle_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    absolute_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ApprovalChallenge(Base):
    """A short-lived, single-use challenge bound to exact approval evidence."""

    __tablename__ = "approval_challenges"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_approval_challenges_token_hash"),
        CheckConstraint("gate IN ('plan','pr','merge')", name="gate"),
        CheckConstraint("run_version >= 0 AND policy_version >= 1", name="versions"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    session_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("operator_sessions.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    gate: Mapped[str] = mapped_column(String(16), nullable=False)
    run_version: Mapped[int] = mapped_column(nullable=False)
    policy_version: Mapped[int] = mapped_column(nullable=False)
    evidence_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
