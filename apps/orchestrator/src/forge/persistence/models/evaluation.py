"""Durable versioned evaluation suite and case records."""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from forge.persistence.models.base import Base


class EvaluationSuite(Base):
    __tablename__ = "evaluation_suites"
    __table_args__ = (
        UniqueConstraint(
            "id", "fixture_version", "metric_version", name="uq_evaluation_suite_binding"
        ),
        UniqueConstraint("idempotency_key", name="uq_evaluation_suite_idempotency"),
        CheckConstraint("btrim(name) <> ''", name="evaluation_suite_name_nonempty"),
        CheckConstraint(
            "btrim(fixture_version) <> '' AND btrim(metric_version) <> ''",
            name="evaluation_suite_versions_nonempty",
        ),
        CheckConstraint(
            "status IN ('pending','running','passed','failed','cancelled')",
            name="evaluation_suite_status",
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    fixture_version: Mapped[str] = mapped_column(String(64), nullable=False)
    metric_version: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class EvaluationCase(Base):
    __tablename__ = "evaluation_cases"
    __table_args__ = (
        UniqueConstraint("suite_id", "case_key", name="uq_evaluation_cases_suite_key"),
        CheckConstraint("btrim(case_key) <> ''", name="evaluation_case_key_nonempty"),
        CheckConstraint(
            "fixture_version <> '' AND metric_version <> ''",
            name="evaluation_case_versions_nonempty",
        ),
        CheckConstraint("role IN ('planner','developer','reviewer')", name="evaluation_case_role"),
        CheckConstraint(
            "status IN ('pending','running','passed','failed','skipped')",
            name="evaluation_case_status",
        ),
        CheckConstraint(
            "input_artifact_digest IS NULL OR input_artifact_digest ~ '^[0-9a-f]{64}$'",
            name="evaluation_case_input_digest",
        ),
        CheckConstraint(
            "output_artifact_digest IS NULL OR output_artifact_digest ~ '^[0-9a-f]{64}$'",
            name="evaluation_case_output_digest",
        ),
        CheckConstraint("metrics_schema_version >= 1", name="evaluation_case_metrics_version"),
        ForeignKeyConstraint(
            ["suite_id", "fixture_version", "metric_version"],
            [
                "evaluation_suites.id",
                "evaluation_suites.fixture_version",
                "evaluation_suites.metric_version",
            ],
            ondelete="RESTRICT",
            name="fk_evaluation_case_suite_binding",
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    suite_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    case_key: Mapped[str] = mapped_column(String(255), nullable=False)
    fixture_version: Mapped[str] = mapped_column(String(64), nullable=False)
    metric_version: Mapped[str] = mapped_column(String(64), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    metrics_schema_version: Mapped[int] = mapped_column(nullable=False, default=1)
    model_usage_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("model_usage.id", ondelete="RESTRICT")
    )
    input_artifact_digest: Mapped[str | None] = mapped_column(String(64))
    output_artifact_digest: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EvaluationBaseline(Base):
    __tablename__ = "evaluation_baselines"
    __table_args__ = (
        UniqueConstraint(
            "name", "fixture_version", "metric_version", name="uq_evaluation_baseline_name_versions"
        ),
        UniqueConstraint("suite_id", name="uq_evaluation_baseline_suite"),
        CheckConstraint("btrim(name) <> ''", name="evaluation_baseline_name_nonempty"),
        CheckConstraint(
            "btrim(fixture_version) <> '' AND btrim(metric_version) <> ''",
            name="evaluation_baseline_versions_nonempty",
        ),
        CheckConstraint(
            "snapshot_schema_version = 1",
            name="evaluation_baseline_snapshot_version",
        ),
        ForeignKeyConstraint(
            ["suite_id", "fixture_version", "metric_version"],
            [
                "evaluation_suites.id",
                "evaluation_suites.fixture_version",
                "evaluation_suites.metric_version",
            ],
            ondelete="RESTRICT",
            name="fk_evaluation_baseline_suite_binding",
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    suite_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    fixture_version: Mapped[str] = mapped_column(String(64), nullable=False)
    metric_version: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_schema_version: Mapped[int] = mapped_column(
        nullable=False, default=1, server_default=text("1")
    )
    cases: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    floors: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    ceilings: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    promoted_by: Mapped[str] = mapped_column(String(128), nullable=False, default="operator")
    promoted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
