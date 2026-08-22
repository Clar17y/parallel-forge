"""Durable commands, audit events, executions, evidence, and operation intents."""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
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


class RunCommand(Base, TimestampMixin):
    """An idempotent durable command leased by one worker at a time."""

    __tablename__ = "run_commands"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_run_commands_idempotency_key"),
        CheckConstraint(
            "status IN ('PENDING','LEASED','SUCCEEDED','FAILED','CANCELLED')", name="status"
        ),
        CheckConstraint(
            "expected_run_version >= 0 AND attempt_count >= 0", name="versions_and_attempts"
        ),
        CheckConstraint("payload_schema_version >= 1", name="payload_schema_version_positive"),
        Index("ix_run_commands_status", "status"),
        Index("ix_run_commands_available_at", "available_at"),
        Index("ix_run_commands_lease_expires_at", "lease_expires_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    command_type: Mapped[str] = mapped_column(String(96), nullable=False)
    expected_run_version: Mapped[int] = mapped_column(Integer, nullable=False)
    actor_id: Mapped[UUID | None] = mapped_column(Uuid)
    payload_schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="PENDING")
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    lease_owner: Mapped[str | None] = mapped_column(String(255))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_summary: Mapped[str | None] = mapped_column(Text)


class RunEvent(Base):
    """Append-only causal audit event with a resumable global sequence."""

    __tablename__ = "run_events"
    __table_args__ = (
        UniqueConstraint("sequence", name="uq_run_events_sequence"),
        CheckConstraint("run_version >= 0", name="run_version_nonnegative"),
        CheckConstraint("payload_schema_version >= 1", name="payload_schema_version_positive"),
        Index("ix_run_events_run_id_sequence", "run_id", "sequence"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    sequence: Mapped[int] = mapped_column(BigInteger, Identity(), nullable=False)
    run_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    run_version: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(96), nullable=False)
    actor_id: Mapped[UUID | None] = mapped_column(Uuid)
    payload_schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Step(Base, TimestampMixin):
    """One workflow step attempt and its transition outcome."""

    __tablename__ = "steps"
    __table_args__ = (
        UniqueConstraint("run_id", "kind", "attempt", name="uq_steps_run_kind_attempt"),
        CheckConstraint(
            "status IN ('PENDING','RUNNING','SUCCEEDED','FAILED','CANCELLED')", name="status"
        ),
        CheckConstraint("attempt >= 1", name="attempt_positive"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(96), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="PENDING")
    transition_from: Mapped[str | None] = mapped_column(String(48))
    transition_to: Mapped[str | None] = mapped_column(String(48))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[str | None] = mapped_column(Text)
    output_artifact_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("artifacts.id", ondelete="SET NULL", use_alter=True)
    )


class Approval(Base):
    """A human approval bound to exact run and evidence versions."""

    __tablename__ = "approvals"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "gate",
            "evidence_digest",
            "run_version",
            name="uq_approvals_gate_evidence_run_version",
        ),
        CheckConstraint("gate IN ('plan','pr','merge')", name="gate"),
        CheckConstraint("run_version >= 0 AND policy_version >= 1", name="versions"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    gate: Mapped[str] = mapped_column(String(16), nullable=False)
    evidence_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    run_version: Mapped[int] = mapped_column(Integer, nullable=False)
    policy_version: Mapped[int] = mapped_column(Integer, nullable=False)
    authenticated_actor_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    invalidation_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AgentExecution(Base, TimestampMixin):
    """One structured model execution under a versioned role contract."""

    __tablename__ = "agent_executions"
    __table_args__ = (
        CheckConstraint("role IN ('planner','developer','reviewer')", name="role"),
        CheckConstraint(
            "status IN ('PENDING','RUNNING','SUCCEEDED','FAILED','CANCELLED')", name="status"
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    step_id: Mapped[UUID | None] = mapped_column(Uuid, ForeignKey("steps.id", ondelete="SET NULL"))
    role: Mapped[str] = mapped_column(String(24), nullable=False)
    instruction_version: Mapped[str] = mapped_column(String(96), nullable=False)
    provider: Mapped[str] = mapped_column(String(96), nullable=False)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="PENDING")
    input_artifact_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("artifacts.id", ondelete="SET NULL", use_alter=True)
    )
    output_artifact_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("artifacts.id", ondelete="SET NULL", use_alter=True)
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ToolCall(Base):
    """An audited typed-tool invocation and its redacted result metadata."""

    __tablename__ = "tool_calls"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING','RUNNING','SUCCEEDED','FAILED','DENIED','CANCELLED')",
            name="status",
        ),
        CheckConstraint("arguments_schema_version >= 1", name="arguments_schema_version_positive"),
        CheckConstraint(
            "(result_metadata IS NULL AND result_metadata_schema_version IS NULL) OR "
            "(result_metadata IS NOT NULL AND result_metadata_schema_version >= 1)",
            name="result_metadata_shape",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    agent_execution_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("agent_executions.id", ondelete="CASCADE"), nullable=False
    )
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    arguments_schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    normalized_arguments: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    authorized: Mapped[bool] = mapped_column(Boolean, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    result_metadata_schema_version: Mapped[int | None] = mapped_column(Integer)
    result_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ModelUsage(Base):
    """Provider usage and cost evidence for one agent execution."""

    __tablename__ = "model_usage"
    __table_args__ = (
        CheckConstraint(
            "input_tokens >= 0 AND output_tokens >= 0 AND estimated_cost_minor >= 0 "
            "AND latency_ms >= 0",
            name="usage_nonnegative",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    agent_execution_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("agent_executions.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(96), nullable=False)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    estimated_cost_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Artifact(Base):
    """Content-addressed artifact metadata and lineage."""

    __tablename__ = "artifacts"
    __table_args__ = (
        UniqueConstraint("digest", name="uq_artifacts_digest"),
        CheckConstraint(
            "size_bytes >= 0 AND metadata_schema_version >= 1", name="size_and_schema_version"
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    digest: Mapped[str] = mapped_column(String(64), nullable=False)
    media_type: Mapped[str] = mapped_column(String(255), nullable=False)
    storage_pointer: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    producer_kind: Mapped[str] = mapped_column(String(96), nullable=False)
    producer_id: Mapped[UUID | None] = mapped_column(Uuid)
    parent_artifact_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("artifacts.id", ondelete="SET NULL")
    )
    metadata_schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    artifact_metadata: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ValidationResult(Base):
    """A named, versioned validation command result."""

    __tablename__ = "validation_results"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING','RUNNING','PASSED','FAILED','SKIPPED','CANCELLED')",
            name="status",
        ),
        CheckConstraint("command_version >= 1", name="command_version_positive"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    step_id: Mapped[UUID | None] = mapped_column(Uuid, ForeignKey("steps.id", ondelete="SET NULL"))
    check_name: Mapped[str] = mapped_column(String(255), nullable=False)
    command_name: Mapped[str] = mapped_column(String(255), nullable=False)
    command_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    exit_code: Mapped[int | None] = mapped_column(Integer)
    output_artifact_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("artifacts.id", ondelete="SET NULL")
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Review(Base, TimestampMixin):
    """A structured review finding and decision."""

    __tablename__ = "reviews"
    __table_args__ = (
        UniqueConstraint("run_id", "finding_id", name="uq_reviews_run_finding_id"),
        CheckConstraint("severity IN ('blocker','major','minor','suggestion')", name="severity"),
        CheckConstraint("status IN ('OPEN','RESOLVED','DISMISSED')", name="status"),
        CheckConstraint("decision IS NULL OR decision IN ('PASS','FAIL')", name="decision"),
        CheckConstraint("start_line IS NULL OR start_line >= 1", name="start_line_positive"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    step_id: Mapped[UUID | None] = mapped_column(Uuid, ForeignKey("steps.id", ondelete="SET NULL"))
    reviewer_execution_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("agent_executions.id", ondelete="SET NULL")
    )
    finding_id: Mapped[str] = mapped_column(String(255), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    path: Mapped[str | None] = mapped_column(Text)
    start_line: Mapped[int | None] = mapped_column(Integer)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[str] = mapped_column(Text, nullable=False)
    proposed_resolution: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="OPEN")
    decision: Mapped[str | None] = mapped_column(String(24))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OperationIntent(Base, TimestampMixin):
    """A durable side-effect intent and reconciliation outcome."""

    __tablename__ = "operation_intents"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_operation_intents_idempotency_key"),
        CheckConstraint(
            "status IN ('PENDING','IN_PROGRESS','SUCCEEDED','FAILED','RECONCILING')",
            name="status",
        ),
        CheckConstraint("attempt_count >= 0", name="attempt_count_nonnegative"),
        CheckConstraint("request_schema_version >= 1", name="request_schema_version_positive"),
        CheckConstraint(
            "(outcome_payload IS NULL AND outcome_schema_version IS NULL) OR "
            "(outcome_payload IS NOT NULL AND outcome_schema_version >= 1)",
            name="outcome_shape",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    operation_kind: Mapped[str] = mapped_column(String(96), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    request_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="PENDING")
    provider_request_id: Mapped[str | None] = mapped_column(String(512))
    repository_id: Mapped[str | None] = mapped_column(String(512))
    resource_identity: Mapped[str | None] = mapped_column(String(1024))
    outcome_schema_version: Mapped[int | None] = mapped_column(Integer)
    outcome_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
