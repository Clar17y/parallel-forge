"""Persisted GitHub pull-request projection."""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from forge.persistence.models.base import Base, TimestampMixin


class PullRequest(Base, TimestampMixin):
    """Observed and managed pull-request identity and state."""

    __tablename__ = "pull_requests"
    __table_args__ = (
        UniqueConstraint(
            "repository",
            "pull_request_number",
            name="uq_pull_requests_repository_number",
        ),
        CheckConstraint("pull_request_number >= 1", name="number_positive"),
        CheckConstraint("state IN ('OPEN','CLOSED','MERGED')", name="state"),
        CheckConstraint(
            "(base_update_intent_id IS NULL) = (base_adoption_intent_id IS NULL)",
            name="base_update_receipt_pair",
        ),
        CheckConstraint(
            "(reviewed_push_intent_id IS NULL AND candidate_evidence_digest IS NULL) OR "
            "(reviewed_push_intent_id IS NOT NULL AND candidate_evidence_digest IS NOT NULL "
            "AND candidate_evidence_digest ~ '^[0-9a-f]{64}$')",
            name="reviewed_candidate_binding",
        ),
        CheckConstraint(
            "merge_method IS NULL OR merge_method IN ('squash','merge','rebase')",
            name="merge_method",
        ),
        CheckConstraint(
            "checks_schema_version >= 1 AND reviews_schema_version >= 1",
            name="payload_versions_positive",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    repository: Mapped[str] = mapped_column(String(512), nullable=False)
    branch: Mapped[str] = mapped_column(String(512), nullable=False)
    base_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    pull_request_number: Mapped[int] = mapped_column(Integer, nullable=False)
    head_sha: Mapped[str] = mapped_column(String(40), nullable=False)
    base_sha: Mapped[str] = mapped_column(String(40), nullable=False)
    checks_schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    checks: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    reviews_schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    review_state: Mapped[dict[str, Any]] = mapped_column("reviews", JSONB, nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    merge_state: Mapped[str | None] = mapped_column(String(48))
    merge_method: Mapped[str | None] = mapped_column(String(24))
    merged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    node_id: Mapped[str | None] = mapped_column(String(255))
    url: Mapped[str | None] = mapped_column(String(1024))
    head_repository: Mapped[str | None] = mapped_column(String(512))
    merge_sha: Mapped[str | None] = mapped_column(String(40))
    candidate_evidence_digest: Mapped[str | None] = mapped_column(String(64))
    base_update_intent_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("operation_intents.id", ondelete="RESTRICT")
    )
    base_adoption_intent_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("operation_intents.id", ondelete="RESTRICT")
    )
    reviewed_push_intent_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("operation_intents.id", ondelete="RESTRICT")
    )
    push_intent_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("operation_intents.id", ondelete="RESTRICT")
    )
    publication_intent_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("operation_intents.id", ondelete="RESTRICT")
    )
    merge_intent_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("operation_intents.id", ondelete="RESTRICT")
    )
