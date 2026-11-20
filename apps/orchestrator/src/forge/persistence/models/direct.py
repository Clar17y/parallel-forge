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
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from forge.persistence.models.base import Base, TimestampMixin

_RUN_STATES = (
    "('QUEUED','PLANNING','AWAITING_PLAN_APPROVAL','DECOMPOSING',"
    "'AWAITING_DECOMPOSITION_APPROVAL','RUNNING','FINALIZING',"
    "'CHECKING','REVIEWING','REPAIRING','PUBLISHING','PR_OPEN','DONE',"
    "'FAILED','CANCELLED','NEEDS_DECISION')"
)
_RUN_SHAPES = "('quick','planned','epic')"
_SESSION_STATES = "('RUNNING','SUCCEEDED','FAILED','KILLED','TIMED_OUT')"
_CHECK_STATUSES = "('PASSED','FAILED','ERROR','CACHED')"
_SESSION_KINDS = "('implement','repair','review','plan','decompose')"
_REVIEW_VERDICTS = "('clean','blocking')"
_TASK_SHAPES = "('quick','planned')"
_TASK_STATES = (
    "('PENDING','PLANNING','RUNNING','CHECKING','REVIEWING','REPAIRING','DONE',"
    "'FAILED','CANCELLED','NEEDS_DECISION','AWAITING_PLAN_APPROVAL')"
)


class DirectRun(Base, TimestampMixin):
    __tablename__ = "direct_runs"
    __table_args__ = (
        CheckConstraint(f"state IN {_RUN_STATES}", name="direct_runs_state"),
        CheckConstraint(f"shape IN {_RUN_SHAPES}", name="direct_runs_shape"),
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
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="QUEUED")
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
    pr_number: Mapped[int | None] = mapped_column(Integer)
    pr_url: Mapped[str | None] = mapped_column(String(1024))
    pr_rounds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    shape: Mapped[str] = mapped_column(String(16), nullable=False, default="quick")
    review_plan_first: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    plan_text: Mapped[str | None] = mapped_column(Text)
    plan_approved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    control_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    approval_task_id: Mapped[UUID | None] = mapped_column(Uuid)
    owner_actions: Mapped[list[dict[str, object]]] = mapped_column(JSONB, nullable=False, default=list)


class DirectTask(Base, TimestampMixin):
    __tablename__ = "direct_tasks"
    __table_args__ = (
        CheckConstraint(f"shape IN {_TASK_SHAPES}", name="direct_tasks_shape"),
        CheckConstraint(f"state IN {_TASK_STATES}", name="direct_tasks_state"),
        Index("ix_direct_tasks_run_ordinal", "run_id", "ordinal"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("direct_runs.id", ondelete="CASCADE"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    task_text: Mapped[str] = mapped_column(Text, nullable=False)
    shape: Mapped[str] = mapped_column(String(16), nullable=False, default="quick")
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="PENDING")
    plan_text: Mapped[str | None] = mapped_column(Text)
    head_sha: Mapped[str | None] = mapped_column(String(64))
    summary: Mapped[str | None] = mapped_column(Text)
    plan_approved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class DirectSession(Base):
    __tablename__ = "direct_sessions"
    __table_args__ = (
        CheckConstraint(f"state IN {_SESSION_STATES}", name="direct_sessions_state"),
        CheckConstraint(f"kind IN {_SESSION_KINDS}", name="direct_sessions_kind"),
        Index("ix_direct_sessions_run", "run_id", "started_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="implement")
    run_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("direct_runs.id", ondelete="CASCADE"), nullable=False
    )
    task_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("direct_tasks.id", ondelete="SET NULL")
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


class DirectReview(Base):
    __tablename__ = "direct_reviews"
    __table_args__ = (
        CheckConstraint(f"verdict IN {_REVIEW_VERDICTS}", name="direct_reviews_verdict"),
        Index("ix_direct_reviews_run_round", "run_id", "round", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("direct_runs.id", ondelete="CASCADE"), nullable=False
    )
    task_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("direct_tasks.id", ondelete="SET NULL")
    )
    session_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("direct_sessions.id", ondelete="CASCADE"), nullable=False
    )
    round: Mapped[int] = mapped_column(Integer, nullable=False)
    client: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    verdict: Mapped[str] = mapped_column(String(16), nullable=False)
    findings: Mapped[list[object]] = mapped_column(JSONB, nullable=False, default=list)
    summary: Mapped[str | None] = mapped_column(Text)
    degraded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reviewer_modified_worktree: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DirectCheck(Base):
    __tablename__ = "direct_checks"
    __table_args__ = (
        CheckConstraint(f"status IN {_CHECK_STATUSES}", name="direct_checks_status"),
        Index("ix_direct_checks_run_round", "run_id", "round", "created_at"),
        Index(
            "ix_direct_checks_cache", "run_id", "tree_sha", "command_digest", "policy_version"
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("direct_runs.id", ondelete="CASCADE"), nullable=False
    )
    task_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("direct_tasks.id", ondelete="SET NULL")
    )
    round: Mapped[int] = mapped_column(Integer, nullable=False)
    command_name: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    tree_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    command_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    exit_code: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    failure_excerpt: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
