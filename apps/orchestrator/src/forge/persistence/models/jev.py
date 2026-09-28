"""Durable conservative Jev admissions and score-only evidence."""

from typing import Literal
from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from forge.persistence.models.base import Base, TimestampMixin


class JevEvaluation(Base, TimestampMixin):
    __tablename__ = "jev_evaluations"
    __table_args__ = (
        CheckConstraint(
            "reserved_input_units >= 0 AND actual_input_units >= 0 AND output_units >= 0 AND duration_ms >= 0 AND cache_hits >= 0",
            name="usage_nonnegative",
        ),
        CheckConstraint(
            "status IN ('pending','ranked','succeeded','unavailable','unknown','budget_exhausted')", name="status"
        ),
        Index("ix_jev_evaluations_run_kind", "run_id", "kind"),
    )

    run_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True
    )
    operation_digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    policy_version: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    requested_mode: Mapped[str] = mapped_column(String(8), nullable=False)
    effective_mode: Mapped[str] = mapped_column(String(8), nullable=False)
    requested_model: Mapped[str] = mapped_column(String(128), nullable=False)
    actual_model: Mapped[str | None] = mapped_column(String(128))
    worktree_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    candidate_digest: Mapped[str | None] = mapped_column(String(64))
    scope_digest: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[Literal["pending", "ranked", "succeeded", "unavailable", "unknown", "budget_exhausted"]] = (
        mapped_column(String(16), nullable=False, default="pending")
    )
    reserved_input_units: Mapped[int] = mapped_column(Integer, nullable=False)
    actual_input_units: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_units: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cache_hits: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    request_id_digest: Mapped[str | None] = mapped_column(String(64))
    cache_source_digest: Mapped[str | None] = mapped_column(String(64))
    diagnostic: Mapped[str | None] = mapped_column(String(64))
    scores: Mapped[dict[str, dict[str, float | int]] | None] = mapped_column(JSONB(none_as_null=True))
