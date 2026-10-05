"""Additive discovery tables; registration and migration are integration-owned."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from forge.persistence.models.base import Base, TimestampMixin


class BrainstormConversation(Base, TimestampMixin):
    __tablename__ = "epic_brainstorm_conversations"
    __table_args__ = (UniqueConstraint("id", "epic_id", "project_id"),)
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    epic_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    project_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class BrainstormTurnRow(Base, TimestampMixin):
    __tablename__ = "epic_brainstorm_turns"
    __table_args__ = (UniqueConstraint("conversation_id", "ordinal"),)
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    conversation_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("epic_brainstorm_conversations.id", ondelete="RESTRICT"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    pending: Mapped[bool] = mapped_column(nullable=False)


class BrainstormJobRow(Base, TimestampMixin):
    __tablename__ = "epic_brainstorm_jobs"
    __table_args__ = (
        Index("ix_epic_brainstorm_jobs_queued", "state", "created_at"),
        ForeignKeyConstraint(
            ("conversation_id", "epic_id", "project_id"),
            (
                "epic_brainstorm_conversations.id",
                "epic_brainstorm_conversations.epic_id",
                "epic_brainstorm_conversations.project_id",
            ),
            ondelete="RESTRICT",
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    epic_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    project_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    conversation_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    snapshot: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    proposal: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    proposal_digest: Mapped[str | None] = mapped_column(String(64))
    failure: Mapped[str | None] = mapped_column(String(64))
    current_attempt_id: Mapped[UUID | None] = mapped_column(Uuid)
    adopted_revision_id: Mapped[UUID | None] = mapped_column(Uuid)
    next_eligible_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    wait_pool_revision: Mapped[int | None] = mapped_column(Integer)


class BrainstormAttemptRow(Base, TimestampMixin):
    __tablename__ = "epic_brainstorm_attempts"
    __table_args__ = (UniqueConstraint("job_id", "number"),)
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    job_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("epic_brainstorm_jobs.id", ondelete="RESTRICT"), nullable=False
    )
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    owner: Mapped[str] = mapped_column(String(128), nullable=False)
    fence: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    launch_intent: Mapped[bool] = mapped_column(nullable=False, default=False)
    launch_id: Mapped[str | None] = mapped_column(String(64))
    origin_host: Mapped[str | None] = mapped_column(String(64))
    process_started: Mapped[bool] = mapped_column(nullable=False, default=False)
    process_pid: Mapped[int | None] = mapped_column(Integer)
    process_identity: Mapped[str | None] = mapped_column(String(256))
    process_settled: Mapped[bool] = mapped_column(nullable=False, default=False)
    terminal_proof: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    usage: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    reservation: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, default=dict)
    usage_known: Mapped[bool | None] = mapped_column()
    tool_calls_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failure: Mapped[str | None] = mapped_column(String(64))


class BrainstormReceiptRow(Base, TimestampMixin):
    __tablename__ = "epic_brainstorm_receipts"
    __table_args__ = (UniqueConstraint("epic_id", "key"),)
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    epic_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    response: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)


class BrainstormAuditRow(Base, TimestampMixin):
    __tablename__ = "epic_brainstorm_audit"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    epic_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    actor_id: Mapped[UUID | None] = mapped_column(Uuid)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    detail: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)


class BrainstormQuotaAdmission(Base, TimestampMixin):
    __tablename__ = "epic_brainstorm_quota_admissions"
    attempt_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("epic_brainstorm_attempts.id", ondelete="RESTRICT"), primary_key=True
    )
    provider: Mapped[str] = mapped_column(String(96), nullable=False)
    account: Mapped[str] = mapped_column(String(96), nullable=False)
    pool: Mapped[str] = mapped_column(String(96), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    probe: Mapped[bool] = mapped_column(nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BrainstormBudgetLedger(Base, TimestampMixin):
    __tablename__ = "epic_brainstorm_budget_ledgers"
    epic_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    project_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    ceiling: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)


__all__ = [
    "BrainstormAttemptRow",
    "BrainstormAuditRow",
    "BrainstormBudgetLedger",
    "BrainstormConversation",
    "BrainstormJobRow",
    "BrainstormQuotaAdmission",
    "BrainstormReceiptRow",
    "BrainstormTurnRow",
]
