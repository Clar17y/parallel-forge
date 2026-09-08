"""Bounded PostgreSQL reads used by the dashboard list screens."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge.application.services.public_data import public_payload
from forge.domain.actor import AgentRole
from forge.domain.agent import _ALLOWED_ROLE_TOOLS
from forge.domain.policy import ProjectPolicy
from forge.persistence.models import (
    AgentExecution,
    EvaluationCase,
    EvaluationSuite,
    ModelUsage,
    OperatorAuditEvent,
    Project,
    ProjectPolicyVersion,
    Run,
    Task,
)


class DashboardListQuery:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = session_factory

    async def approvals(
        self, offset: int = 0, limit: int = 50
    ) -> tuple[list[dict[str, object]], bool]:
        async with self._factory() as s:
            rows = (
                await s.execute(
                    select(Run, Task)
                    .join(Task, Task.id == Run.task_id)
                    .where(Run.pending_gate.is_not(None))
                    .order_by(Run.updated_at.desc(), Run.id)
                    .offset(offset)
                    .limit(limit + 1)
                )
            ).all()
        return [
            {
                "run_id": r.id,
                "task_id": t.id,
                "gate": r.pending_gate,
                "evidence_digest": r.pending_evidence_digest,
                "run_version": r.version,
                "policy_version": r.policy_version,
            }
            for r, t in rows[:limit]
        ], len(rows) > limit

    async def policy(self, project_id: UUID) -> dict[str, object] | None:
        async with self._factory() as s:
            p = await s.get(Project, project_id)
            if p is None or p.current_policy_version is None:
                return None
            row = await s.get(ProjectPolicyVersion, (project_id, p.current_policy_version))
        if row is None:
            return None
        parsed = ProjectPolicy.model_validate(row.document)
        return {
            "project_id": project_id,
            "version": row.version,
            "policy_digest": row.policy_digest,
            "runner_mode": parsed.runner_mode.value,
            "database_enabled": parsed.database.enabled,
            "limits": {
                "local_remediation_limit": parsed.local_remediation_limit,
                "remote_remediation_limit": parsed.remote_remediation_limit,
            },
        }

    async def audit(self, offset: int = 0, limit: int = 50) -> tuple[list[dict[str, object]], bool]:
        async with self._factory() as s:
            rows = list(
                await s.scalars(
                    select(OperatorAuditEvent)
                    .order_by(OperatorAuditEvent.created_at.desc(), OperatorAuditEvent.id)
                    .offset(offset)
                    .limit(limit + 1)
                )
            )
        return [
            {
                "id": x.id,
                "source": "operator",
                "actor_id": x.actor_id,
                "event_type": x.event_type,
                "subject_type": x.subject_type,
                "subject_id": x.subject_id,
                "created_at": x.created_at,
                "payload": public_payload(x.payload),
            }
            for x in rows[:limit]
        ], len(rows) > limit

    async def usage(self, offset: int = 0, limit: int = 50) -> tuple[list[dict[str, object]], bool]:
        async with self._factory() as s:
            rows = (
                await s.execute(
                    select(
                        ModelUsage.currency,
                        func.sum(ModelUsage.input_tokens),
                        func.sum(ModelUsage.output_tokens),
                        func.sum(ModelUsage.estimated_cost_minor),
                        func.count().filter(ModelUsage.estimated_cost_minor.is_(None)),
                        func.count(),
                    )
                    .group_by(ModelUsage.currency)
                    .order_by(ModelUsage.currency)
                    .offset(offset)
                    .limit(limit + 1)
                )
            ).all()
        return [
            {
                "currency": r[0],
                "input_tokens": r[1] or 0,
                "output_tokens": r[2] or 0,
                "known_cost_minor": r[3] or 0,
                "unpriced_calls": r[4],
                "model_calls": r[5],
            }
            for r in rows[:limit]
        ], len(rows) > limit

    async def agents(
        self, offset: int = 0, limit: int = 50
    ) -> tuple[list[dict[str, object]], bool]:
        async with self._factory() as s:
            rows = list(
                await s.scalars(
                    select(AgentExecution)
                    .order_by(AgentExecution.created_at.desc(), AgentExecution.id)
                    .offset(offset)
                    .limit(limit + 1)
                )
            )
        return [
            {
                "id": x.id,
                "run_id": x.run_id,
                "role": x.role,
                "provider": x.provider,
                "model": x.model,
                "status": x.status,
                "instruction_version": x.instruction_version,
            }
            for x in rows[:limit]
        ], len(rows) > limit

    async def tool_permissions(self) -> list[dict[str, object]]:
        return [
            {"role": role.value, "tools": sorted(tool.value for tool in _ALLOWED_ROLE_TOOLS[role])}
            for role in AgentRole
        ]

    async def evaluations(
        self, offset: int = 0, limit: int = 50
    ) -> tuple[list[dict[str, object]], bool]:
        async with self._factory() as s:
            rows = (
                await s.execute(
                    select(EvaluationSuite, EvaluationCase)
                    .outerjoin(EvaluationCase, EvaluationCase.suite_id == EvaluationSuite.id)
                    .order_by(
                        EvaluationSuite.created_at.desc(),
                        EvaluationSuite.id,
                        EvaluationCase.created_at,
                        EvaluationCase.id,
                    )
                    .offset(offset)
                    .limit(limit + 1)
                )
            ).all()
        return [
            {
                "suite_id": suite.id,
                "suite_status": suite.status,
                "fixture_version": suite.fixture_version,
                "metric_version": suite.metric_version,
                "case_id": case.id if case else None,
                "case_key": case.case_key if case else None,
                "role": case.role if case else None,
                "status": case.status if case else None,
                "metrics": public_payload(case.metrics) if case else None,
                "model_usage_id": case.model_usage_id if case else None,
                "input_artifact_digest": case.input_artifact_digest if case else None,
                "output_artifact_digest": case.output_artifact_digest if case else None,
                "created_at": case.created_at if case else suite.created_at,
                "completed_at": case.completed_at if case else None,
            }
            for suite, case in rows[:limit]
        ], len(rows) > limit
