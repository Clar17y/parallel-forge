"""Project identity, immutable policy versions, and normalized tasks."""

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
        ForeignKeyConstraint(
            ("id", "current_policy_version"),
            ("project_policy_versions.project_id", "project_policy_versions.version"),
            name="fk_projects_current_policy_version_project_policy_versions",
            ondelete="RESTRICT",
            use_alter=True,
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    canonical_path: Mapped[str] = mapped_column(Text, nullable=False)
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
        ForeignKey("projects.id", ondelete="CASCADE"),
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
        UniqueConstraint("external_source", "external_id", name="uq_tasks_external_identity"),
        UniqueConstraint("id", "project_id", name="uq_tasks_id_project_id"),
        CheckConstraint(
            "(external_source IS NULL) = (external_id IS NULL)",
            name="external_identity_shape",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    normalized_text: Mapped[str] = mapped_column(Text, nullable=False)
    task_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    external_source: Mapped[str | None] = mapped_column(String(64))
    external_id: Mapped[str | None] = mapped_column(String(255))
