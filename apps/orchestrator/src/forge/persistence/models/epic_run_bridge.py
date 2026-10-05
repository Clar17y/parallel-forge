"""Immutable epic execution and attempt bindings."""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from forge.persistence.models.base import Base


class EpicExecution(Base):
    __tablename__ = "epic_executions"
    __table_args__ = (
        UniqueConstraint(
            "id",
            "epic_id",
            "brief_revision_id",
            "brief_digest",
            "graph_revision_id",
            "graph_digest",
            name="uq_epic_executions_source",
        ),
        ForeignKeyConstraint(
            ("epic_id", "brief_revision_id", "brief_digest"),
            (
                "epic_brief_revisions.epic_id",
                "epic_brief_revisions.id",
                "epic_brief_revisions.content_digest",
            ),
            name="fk_epic_executions_brief",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ("epic_id", "graph_revision_id", "graph_digest"),
            (
                "epic_graph_revisions.epic_id",
                "epic_graph_revisions.id",
                "epic_graph_revisions.graph_digest",
            ),
            name="fk_epic_executions_graph",
            ondelete="RESTRICT",
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    epic_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("epics.id", ondelete="RESTRICT"), nullable=False
    )
    brief_revision_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    brief_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    graph_revision_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    graph_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class EpicItemAttempt(Base):
    __tablename__ = "epic_item_attempts"
    __table_args__ = (
        ForeignKeyConstraint(
            (
                "execution_id",
                "epic_id",
                "brief_revision_id",
                "brief_digest",
                "graph_revision_id",
                "graph_digest",
            ),
            (
                "epic_executions.id",
                "epic_executions.epic_id",
                "epic_executions.brief_revision_id",
                "epic_executions.brief_digest",
                "epic_executions.graph_revision_id",
                "epic_executions.graph_digest",
            ),
            name="fk_epic_item_attempts_execution_source",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("run_id", name="uq_epic_item_attempts_run"),
        UniqueConstraint("task_id", name="uq_epic_item_attempts_task"),
        UniqueConstraint(
            "execution_id", "item_id", "attempt_number", name="uq_epic_item_attempts_number"
        ),
        Index("ix_epic_item_attempts_execution", "execution_id", "created_at"),
        CheckConstraint("attempt_number >= 1", name="attempt_number_positive"),
        CheckConstraint(
            "expected_epic_version >= 1 AND actual_epic_version >= 1", name="epic_versions_positive"
        ),
        CheckConstraint("item_disposition IN ('required', 'deferred')", name="item_disposition"),
        CheckConstraint("context_digest ~ '^[0-9a-f]{64}$'", name="context_digest"),
        CheckConstraint("task_digest ~ '^[0-9a-f]{64}$'", name="task_digest"),
        CheckConstraint("item_digest ~ '^[0-9a-f]{64}$'", name="item_digest"),
        CheckConstraint("base_sha ~ '^[0-9a-f]{40}$'", name="base_sha"),
        CheckConstraint(
            "jsonb_typeof(blocker_codes) = 'array' AND jsonb_typeof(dependency_evidence) = 'array'",
            name="evidence_shape",
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    execution_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    epic_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("epics.id", ondelete="RESTRICT"), nullable=False
    )
    item_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    item_disposition: Mapped[str] = mapped_column(String(8), nullable=False)
    actor_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    expected_epic_version: Mapped[int] = mapped_column(Integer, nullable=False)
    actual_epic_version: Mapped[int] = mapped_column(Integer, nullable=False)
    task_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("tasks.id", ondelete="RESTRICT"), nullable=False
    )
    run_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("runs.id", ondelete="RESTRICT"), nullable=False
    )
    brief_revision_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    brief_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    graph_revision_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    graph_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    item_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    context_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    task_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    base_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    base_sha: Mapped[str] = mapped_column(String(40), nullable=False)
    owner_override: Mapped[bool] = mapped_column(Boolean, nullable=False)
    override_note: Mapped[str | None] = mapped_column(String(2048))
    blocker_codes: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    dependency_evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
