"""Worker-local registration observations; never launch or quota authority."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, Index, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from forge.persistence.models.base import Base


class SubscriptionWorkerStatus(Base):
    __tablename__ = "subscription_worker_status"
    __table_args__ = (
        CheckConstraint(
            "jsonb_typeof(routes) = 'array' AND jsonb_array_length(routes) <= 64",
            name="runtime_routes_bound",
        ),
        CheckConstraint(
            "jsonb_typeof(model_catalogs) = 'array' AND jsonb_array_length(model_catalogs) <= 8",
            name="runtime_model_catalogs_bound",
        ),
        CheckConstraint(
            "stopped_at IS NULL OR stopped_at >= last_seen_at", name="runtime_stop_order"
        ),
        Index("ix_subscription_worker_status_last_seen_at", "last_seen_at"),
    )
    worker_instance_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    routes: Mapped[list[dict[str, str]]] = mapped_column(JSONB, nullable=False)
    model_catalogs: Mapped[list[dict[str, object]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
