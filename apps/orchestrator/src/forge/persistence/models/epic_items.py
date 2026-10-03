"""Append-only graph revisions."""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from forge.persistence.models.base import Base


class EpicGraphRevision(Base):
    __tablename__ = "epic_graph_revisions"
    __table_args__ = (
        UniqueConstraint("epic_id", "revision_number", name="uq_epic_graph_revisions_number"),
        UniqueConstraint("epic_id", "id", "graph_digest", name="uq_epic_graph_revisions_binding"),
        ForeignKeyConstraint(
            ("epic_id", "brief_revision_id", "brief_digest"),
            (
                "epic_brief_revisions.epic_id",
                "epic_brief_revisions.id",
                "epic_brief_revisions.content_digest",
            ),
            name="fk_epic_graph_revisions_brief",
            ondelete="RESTRICT",
        ),
        CheckConstraint("revision_number >= 1", name="revision_number_positive"),
        CheckConstraint("epic_version >= 1", name="epic_version_positive"),
        CheckConstraint("document_schema_version = 1", name="document_schema_version"),
        CheckConstraint("graph_digest ~ '^[0-9a-f]{64}$'", name="graph_digest"),
        CheckConstraint("brief_digest ~ '^[0-9a-f]{64}$'", name="brief_digest"),
        CheckConstraint(
            "jsonb_typeof(content) = 'object' AND octet_length(content::text) <= 524288",
            name="content_bounded",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    epic_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("epics.id", ondelete="RESTRICT"), nullable=False
    )
    brief_revision_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    brief_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    epic_version: Mapped[int] = mapped_column(Integer, nullable=False)
    document_schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    content: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    graph_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
