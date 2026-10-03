"""Durable epic drafts and append-only brief revisions."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
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

from forge.persistence.models.base import Base, TimestampMixin


class Epic(Base, TimestampMixin):
    __tablename__ = "epics"
    __table_args__ = (
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint("draft_schema_version = 1", name="draft_schema_version"),
        CheckConstraint(
            "jsonb_typeof(draft) = 'object' AND octet_length(draft::text) <= 262144",
            name="draft_bounded",
        ),
        CheckConstraint(
            "(accepted_brief_revision_id IS NULL) = (accepted_brief_digest IS NULL)",
            name="accepted_brief_pair",
        ),
        CheckConstraint(
            "(accepted_graph_revision_id IS NULL) = (accepted_graph_digest IS NULL)",
            name="accepted_graph_pair",
        ),
        CheckConstraint("btrim(title) <> ''", name="title_nonblank"),
        CheckConstraint("octet_length(title) <= 256", name="title_bounded"),
        CheckConstraint(
            "accepted_brief_digest IS NULL OR accepted_brief_digest ~ '^[0-9a-f]{64}$'",
            name="accepted_brief_digest",
        ),
        CheckConstraint(
            "accepted_graph_digest IS NULL OR accepted_graph_digest ~ '^[0-9a-f]{64}$'",
            name="accepted_graph_digest",
        ),
        ForeignKeyConstraint(
            ("id", "accepted_brief_revision_id", "accepted_brief_digest"),
            (
                "epic_brief_revisions.epic_id",
                "epic_brief_revisions.id",
                "epic_brief_revisions.content_digest",
            ),
            name="fk_epics_accepted_brief",
            ondelete="RESTRICT",
            use_alter=True,
            deferrable=True,
            initially="DEFERRED",
        ),
        Index("ix_epics_project_id", "project_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    draft_schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    draft: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    accepted_brief_revision_id: Mapped[UUID | None] = mapped_column(Uuid)
    accepted_brief_digest: Mapped[str | None] = mapped_column(String(64))
    accepted_graph_revision_id: Mapped[UUID | None] = mapped_column(Uuid)
    accepted_graph_digest: Mapped[str | None] = mapped_column(String(64))


class EpicBriefRevision(Base):
    __tablename__ = "epic_brief_revisions"
    __table_args__ = (
        UniqueConstraint("epic_id", "revision_number", name="uq_epic_brief_revisions_number"),
        UniqueConstraint("epic_id", "id", "content_digest", name="uq_epic_brief_revisions_binding"),
        CheckConstraint("revision_number >= 1", name="revision_number_positive"),
        CheckConstraint("epic_version >= 1", name="epic_version_positive"),
        CheckConstraint("document_schema_version = 1", name="document_schema_version"),
        CheckConstraint("content_digest ~ '^[0-9a-f]{64}$'", name="content_digest"),
        CheckConstraint(
            "jsonb_typeof(content) = 'object' AND octet_length(content::text) <= 262144",
            name="content_bounded",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    epic_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("epics.id", ondelete="RESTRICT"), nullable=False
    )
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    epic_version: Mapped[int] = mapped_column(Integer, nullable=False)
    document_schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    content: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    content_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    source_job_id: Mapped[UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
