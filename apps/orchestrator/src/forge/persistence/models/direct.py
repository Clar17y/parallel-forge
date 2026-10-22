"""Direct autonomous run and session rows (Slice A)."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from forge.persistence.models.base import Base, TimestampMixin

_RUN_STATES = "('QUEUED','RUNNING','FINALIZING','DONE','FAILED','CANCELLED','NEEDS_DECISION')"
_SESSION_STATES = "('RUNNING','SUCCEEDED','FAILED','KILLED','TIMED_OUT')"


class DirectRun(Base, TimestampMixin):
    __tablename__ = "direct_runs"
    __table_args__ = (
        CheckConstraint(f"state IN {_RUN_STATES}", name="direct_runs_state"),
        CheckConstraint("btrim(task_text) <> ''", name="direct_runs_task_nonblank"),
        Index("ix_direct_runs_state_created", "state", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    task_text: Mapped[str] = mapped_column(Text, nullable=False)
    client: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    effort: Mapped[str | None] = mapped_column(String(32))
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="QUEUED")
    branch: Mapped[str | None] = mapped_column(String(255))
    worktree_path: Mapped[str | None] = mapped_column(Text)
    base_sha: Mapped[str | None] = mapped_column(String(64))
    head_sha: Mapped[str | None] = mapped_column(String(64))
    summary: Mapped[str | None] = mapped_column(Text)
    question: Mapped[str | None] = mapped_column(Text)
    options: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    failure_reason: Mapped[str | None] = mapped_column(String(512))
    result_degraded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class DirectSession(Base):
    __tablename__ = "direct_sessions"
    __table_args__ = (
        CheckConstraint(f"state IN {_SESSION_STATES}", name="direct_sessions_state"),
        Index("ix_direct_sessions_run", "run_id", "started_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("direct_runs.id", ondelete="CASCADE"), nullable=False
    )
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="RUNNING")
    client: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    effort: Mapped[str | None] = mapped_column(String(32))
    pid: Mapped[int | None] = mapped_column(Integer)
    transcript_path: Mapped[str | None] = mapped_column(Text)
    last_output_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    output_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    exit_code: Mapped[int | None] = mapped_column(Integer)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_reason: Mapped[str | None] = mapped_column(String(512))
