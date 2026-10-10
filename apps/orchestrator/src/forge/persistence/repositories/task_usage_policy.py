"""Append-only monitoring policy revisions with project-row compare-and-swap."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forge.domain.task_usage_contract import MonitoringMode, MonitoringPolicy
from forge.persistence.models import Project, TaskUsagePolicyRevision
from forge.persistence.repositories.projects import PolicyVersionConflict, ProjectNotFound


class MonitoringPolicyConflict(PolicyVersionConflict):
    """The owner supplied a stale monitoring revision."""


def policy_from_row(row: TaskUsagePolicyRevision) -> MonitoringPolicy:
    """Decode one persisted revision without changing its policy semantics."""
    return MonitoringPolicy(
        project_id=row.project_id,
        revision=row.revision,
        mode=MonitoringMode(row.mode),
        warning_multiplier=row.warning_multiplier,
        checkpoint_multiplier=row.checkpoint_multiplier,
        history_limit=row.history_limit,
        window_days=row.window_days,
        minimum_comparables=row.minimum_comparables,
        hard_token_cap=row.hard_token_cap,
        hard_context_cap=row.hard_context_cap,
        manual_estimate_tokens=row.manual_estimate_tokens,
        allow_cross_project_fallback=row.allow_cross_project_fallback,
    )


class PostgresTaskUsagePolicyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, project_id: UUID, revision: int | None = None) -> MonitoringPolicy | None:
        if revision is not None and (type(revision) is not int or revision < 1):
            raise ValueError("monitoring revision must be positive")
        query = select(TaskUsagePolicyRevision).where(
            TaskUsagePolicyRevision.project_id == project_id
        )
        if revision is None:
            query = query.order_by(TaskUsagePolicyRevision.revision.desc()).limit(1)
        else:
            query = query.where(TaskUsagePolicyRevision.revision == revision)
        row = (await self._session.execute(query)).scalar_one_or_none()
        if row is None:
            return None
        return policy_from_row(row)

    async def append_policy_revision(
        self, policy: MonitoringPolicy, *, expected_revision: int, actor_id: UUID
    ) -> None:
        if (
            not isinstance(actor_id, UUID)
            or type(expected_revision) is not int
            or expected_revision < 0
        ):
            raise ValueError("monitoring policy actor and expected revision are required")
        project = (
            await self._session.execute(
                select(Project.id).where(Project.id == policy.project_id).with_for_update()
            )
        ).scalar_one_or_none()
        if project is None:
            raise ProjectNotFound("project was not found")
        current = await self.get(policy.project_id)
        actual_revision = 0 if current is None else current.revision
        if actual_revision != expected_revision or policy.revision != expected_revision + 1:
            raise MonitoringPolicyConflict("expected monitoring revision is stale")
        self._session.add(
            TaskUsagePolicyRevision(
                project_id=policy.project_id,
                revision=policy.revision,
                mode=policy.mode.value,
                warning_multiplier=policy.warning_multiplier,
                checkpoint_multiplier=policy.checkpoint_multiplier,
                history_limit=policy.history_limit,
                window_days=policy.window_days,
                minimum_comparables=policy.minimum_comparables,
                hard_token_cap=policy.hard_token_cap,
                hard_context_cap=policy.hard_context_cap,
                manual_estimate_tokens=policy.manual_estimate_tokens,
                allow_cross_project_fallback=policy.allow_cross_project_fallback,
                actor_id=actor_id,
            )
        )
        await self._session.flush()
