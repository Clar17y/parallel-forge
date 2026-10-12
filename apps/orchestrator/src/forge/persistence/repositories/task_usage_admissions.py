"""Immutable subject-admission context captured before provider work can be delayed."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forge.domain.task_usage_contract import (
    RouteCohort,
    SubjectKey,
    TaskUsageSubjectAdmission,
)
from forge.persistence.models.project import Project
from forge.persistence.models.task_usage import (
    TaskUsagePolicyRevision,
    TaskUsageSubjectAdmissionRow,
)
from forge.persistence.repositories.task_usage_observations import _member, _members


class SubjectAdmissionConflict(ValueError):
    """A subject is missing or its immutable configured composition changed."""


class PostgresTaskUsageAdmissionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, subject: SubjectKey) -> TaskUsageSubjectAdmission | None:
        row = await self._session.get(
            TaskUsageSubjectAdmissionRow,
            (subject.project_id, subject.kind, subject.subject_id),
        )
        if row is None:
            return None
        return TaskUsageSubjectAdmission(
            subject=subject,
            configured_route=RouteCohort(
                fingerprint=row.configured_route_digest,
                members=tuple(_member(item) for item in row.configured_route_members),
            ),
            policy_revision=row.policy_revision,
            admitted_at=row.admitted_at,
        )

    async def admit(
        self, subject: SubjectKey, configured_route: RouteCohort
    ) -> TaskUsageSubjectAdmission:
        if not isinstance(subject, SubjectKey) or not isinstance(configured_route, RouteCohort):
            raise TypeError("subject and configured route are required")
        project = (
            await self._session.execute(
                select(Project.id).where(Project.id == subject.project_id).with_for_update()
            )
        ).scalar_one_or_none()
        if project is None:
            raise SubjectAdmissionConflict("subject project does not exist")
        existing = await self.get(subject)
        if existing is not None:
            if existing.configured_route != configured_route:
                raise SubjectAdmissionConflict("subject configured route differs from admission")
            return existing
        policy_revision = (
            await self._session.execute(
                select(TaskUsagePolicyRevision.revision)
                .where(TaskUsagePolicyRevision.project_id == subject.project_id)
                .order_by(TaskUsagePolicyRevision.revision.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        admitted_at = datetime.now(UTC)
        self._session.add(
            TaskUsageSubjectAdmissionRow(
                project_id=subject.project_id,
                subject_kind=subject.kind,
                subject_id=subject.subject_id,
                run_id=subject.subject_id if subject.kind == "run" else None,
                authoring_job_id=subject.subject_id if subject.kind == "authoring_job" else None,
                policy_revision=policy_revision,
                configured_route_digest=configured_route.fingerprint,
                configured_route_members=_members(configured_route),
                admitted_at=admitted_at,
            )
        )
        await self._session.flush()
        return TaskUsageSubjectAdmission(
            subject=subject,
            configured_route=configured_route,
            policy_revision=policy_revision,
            admitted_at=admitted_at,
        )
