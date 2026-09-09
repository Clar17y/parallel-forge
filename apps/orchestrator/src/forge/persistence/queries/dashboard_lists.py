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
    Approval,
    EvaluationCase,
    EvaluationSuite,
    ModelUsage,
    Project,
    ProjectPolicyVersion,
    Run,
    Task,
)


class DashboardListQuery:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = session_factory

    async def approval_history(
        self, run_id: UUID, offset: int = 0, limit: int = 50
    ) -> tuple[list[dict[str, object]], bool] | None:
        async with self._factory() as session:
            await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            if await session.get(Run, run_id) is None:
                return None
            rows = list(
                await session.scalars(
                    select(Approval)
                    .where(Approval.run_id == run_id)
                    .order_by(Approval.created_at.desc(), Approval.id)
                    .offset(offset)
                    .limit(limit + 1)
                )
            )
            return [
                {
                    **{
                        name: getattr(row, name)
                        for name in (
                            "id",
                            "gate",
                            "evidence_digest",
                            "run_version",
                            "policy_version",
                            "authenticated_actor_id",
                            "created_at",
                            "invalidated_at",
                        )
                    },
                    "invalidation_reason": public_payload({"reason": row.invalidation_reason})[
                        "reason"
                    ],
                }
                for row in rows[:limit]
            ], len(rows) > limit

    async def run_usage(
        self, run_id: UUID, offset: int = 0, limit: int = 50
    ) -> tuple[list[dict[str, object]], bool] | None:
        async with self._factory() as session:
            await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            if await session.get(Run, run_id) is None:
                return None
            rows = (
                await session.execute(
                    select(ModelUsage, AgentExecution.role, AgentExecution.instruction_digest)
                    .join(
                        AgentExecution,
                        (AgentExecution.id == ModelUsage.agent_execution_id)
                        & (AgentExecution.run_id == ModelUsage.run_id),
                    )
                    .where(ModelUsage.run_id == run_id)
                    .order_by(ModelUsage.created_at.desc(), ModelUsage.id)
                    .offset(offset)
                    .limit(limit + 1)
                )
            ).all()
            return [
                {
                    **{
                        name: getattr(usage, name)
                        for name in (
                            "id",
                            "agent_execution_id",
                            "provider",
                            "model",
                            "prompt_version",
                            "input_tokens",
                            "output_tokens",
                            "cached_input_tokens",
                            "duration_ms",
                            "tool_call_count",
                            "pricing_version",
                            "estimated_cost_minor",
                            "currency",
                            "created_at",
                        )
                    },
                    "role": role,
                    "instruction_digest": digest,
                    "unknown_price_reason": public_payload({"reason": usage.unknown_price_reason})[
                        "reason"
                    ],
                }
                for usage, role, digest in rows[:limit]
            ], len(rows) > limit

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

    async def audit(
        self,
        offset: int = 0,
        limit: int = 50,
        *,
        run_id: UUID | None = None,
        project_id: UUID | None = None,
        actor_id: UUID | None = None,
        operation_id: UUID | None = None,
        operation_status: str | None = None,
    ) -> tuple[list[dict[str, object]], bool]:
        from forge.persistence.queries.audit import AuditQuery

        return await AuditQuery(self._factory).page(
            offset,
            limit,
            run_id=run_id,
            project_id=project_id,
            actor_id=actor_id,
            operation_id=operation_id,
            operation_status=operation_status,
        )

    async def audit_detail(
        self, event_id: UUID, source: str = "operator"
    ) -> dict[str, object] | None:
        from forge.persistence.queries.audit import AuditQuery

        return await AuditQuery(self._factory).detail(source, event_id)

    async def check_history(
        self, run_id: UUID, offset: int = 0, limit: int = 50
    ) -> tuple[list[dict[str, object]], bool] | None:
        from forge.persistence.queries.check_history import check_history

        return await check_history(self._factory, run_id, offset, limit)

    async def usage(self, offset: int = 0, limit: int = 50) -> tuple[list[dict[str, object]], bool]:
        async with self._factory() as s:
            rows = (
                await s.execute(
                    select(
                        Run.project_id,
                        ModelUsage.run_id,
                        ModelUsage.provider,
                        ModelUsage.model,
                        ModelUsage.currency,
                        func.sum(ModelUsage.input_tokens),
                        func.sum(ModelUsage.output_tokens),
                        func.sum(ModelUsage.duration_ms),
                        func.sum(ModelUsage.estimated_cost_minor),
                        func.count().filter(ModelUsage.estimated_cost_minor.is_(None)),
                        func.count(),
                    )
                    .join(Run, Run.id == ModelUsage.run_id)
                    .group_by(
                        Run.project_id,
                        ModelUsage.run_id,
                        ModelUsage.provider,
                        ModelUsage.model,
                        ModelUsage.currency,
                    )
                    .order_by(
                        Run.project_id,
                        ModelUsage.run_id,
                        ModelUsage.provider,
                        ModelUsage.model,
                        ModelUsage.currency,
                    )
                    .offset(offset)
                    .limit(limit + 1)
                )
            ).all()
        return [
            {
                "project_id": r[0],
                "run_id": r[1],
                "provider": r[2],
                "model": r[3],
                "currency": r[4],
                "input_tokens": r[5] or 0,
                "output_tokens": r[6] or 0,
                "duration_ms": r[7] or 0,
                "known_cost_minor": r[8] or 0,
                "unpriced_calls": r[9],
                "model_calls": r[10],
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
                    select(EvaluationSuite, EvaluationCase, ModelUsage)
                    .outerjoin(EvaluationCase, EvaluationCase.suite_id == EvaluationSuite.id)
                    .outerjoin(ModelUsage, ModelUsage.id == EvaluationCase.model_usage_id)
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
                "prompt_version": usage.prompt_version if usage else None,
                "provider": usage.provider if usage else None,
                "model": usage.model if usage else None,
                "input_tokens": usage.input_tokens if usage else None,
                "output_tokens": usage.output_tokens if usage else None,
                "duration_ms": usage.duration_ms if usage else None,
                "currency": usage.currency if usage else None,
                "estimated_cost_minor": usage.estimated_cost_minor if usage else None,
                "input_artifact_digest": case.input_artifact_digest if case else None,
                "output_artifact_digest": case.output_artifact_digest if case else None,
                "created_at": case.created_at if case else suite.created_at,
                "completed_at": case.completed_at if case else None,
            }
            for suite, case, usage in rows[:limit]
        ], len(rows) > limit
