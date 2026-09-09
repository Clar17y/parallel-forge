"""Project identity, immutable policy versions, and normalized tasks."""

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
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from forge.persistence.models.base import Base, TimestampMixin


class Project(Base, TimestampMixin):
    """A canonical local repository and its current immutable policy."""

    __tablename__ = "projects"
    __table_args__ = (
        UniqueConstraint("github_repository", name="uq_projects_github_repository"),
        UniqueConstraint("canonical_path", name="uq_projects_canonical_path"),
        UniqueConstraint("canonical_path_key", name="uq_projects_canonical_path_key"),
        Index("ix_projects_canonical_path_key", "canonical_path_key"),
        CheckConstraint("btrim(name) <> ''", name="name_nonblank"),
        CheckConstraint("btrim(canonical_path_key) <> ''", name="canonical_path_key_nonblank"),
        ForeignKeyConstraint(
            ("id", "current_policy_version"),
            ("project_policy_versions.project_id", "project_policy_versions.version"),
            name="fk_projects_current_policy_version_project_policy_versions",
            ondelete="RESTRICT",
            use_alter=True,
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False, default="Project")
    canonical_path: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_path_key: Mapped[str] = mapped_column(Text, nullable=False)
    github_repository: Mapped[str] = mapped_column(String(512), nullable=False)
    default_branch: Mapped[str] = mapped_column(String(255), nullable=False)
    instructions_path: Mapped[str | None] = mapped_column(Text)
    current_policy_version: Mapped[int | None] = mapped_column(Integer)


class ProjectPolicyVersion(Base):
    """An immutable, content-bound project policy document."""

    __tablename__ = "project_policy_versions"
    __table_args__ = (
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint("document_schema_version >= 1", name="document_schema_version_positive"),
    )

    project_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("projects.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    policy_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    document_schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    document: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Task(Base, TimestampMixin):
    """Normalized operator task text and optional external identity."""

    __tablename__ = "tasks"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "external_source",
            "external_id",
            name="uq_tasks_project_external_identity",
        ),
        UniqueConstraint("id", "project_id", name="uq_tasks_id_project_id"),
        CheckConstraint(
            "(external_source IS NULL) = (external_id IS NULL)",
            name="external_identity_shape",
        ),
        CheckConstraint(
            "(external_source IS NULL AND external_id IS NULL AND untrusted_external_content = FALSE) "
            "OR (external_source IS NOT NULL AND external_id IS NOT NULL "
            "AND untrusted_external_content = TRUE)",
            name="external_source_trust_shape",
        ),
        CheckConstraint("btrim(title) <> ''", name="title_nonblank"),
        CheckConstraint("octet_length(title) <= 512", name="title_bounded"),
        CheckConstraint("octet_length(body) <= 1048576", name="body_bounded"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(512), nullable=False, default="Imported task")
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    source_url: Mapped[str | None] = mapped_column(String(2048))
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    untrusted_external_content: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    normalized_text: Mapped[str] = mapped_column(Text, nullable=False)
    task_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    external_source: Mapped[str | None] = mapped_column(String(64))
    external_id: Mapped[str | None] = mapped_column(String(255))
