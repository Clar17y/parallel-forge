# ruff: noqa: B008
"""Authenticated learned usage reporting for accessible Forge projects."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from forge.api.dependencies import require_operator
from forge.api.schemas.task_usage import TaskUsageHistoryResponse
from forge.application.services.auth import AuthenticatedActor
from forge.application.services.task_usage_baselines import (
    TaskUsageBaselineService,
    UsageReportFilters,
)
from forge.domain.task_usage_baselines import SamplingRules


def router_for() -> APIRouter:
    router = APIRouter()

    @router.get("/task-usage-report", response_model=TaskUsageHistoryResponse)
    async def task_usage_report(
        request: Request,
        project_id: list[UUID] | None = Query(None),
        subject_kind: str | None = Query(None, pattern="^(run|authoring_job)$"),
        task_kind: str | None = Query(None, min_length=1, max_length=255),
        provider: str | None = Query(None, min_length=1, max_length=96),
        model: str | None = Query(None, min_length=1, max_length=255),
        effort: str | None = Query(None, min_length=1, max_length=96),
        window_days: int | None = Query(None, ge=1, le=2**63 - 1),
        _actor: AuthenticatedActor = Depends(require_operator),
    ) -> dict[str, object]:
        service = getattr(request.app.state, "task_usage_baseline_service", None)
        if not isinstance(service, TaskUsageBaselineService):
            raise HTTPException(503, "task usage reporting unavailable")
        try:
            filters = UsageReportFilters(
                project_ids=None if project_id is None else frozenset(project_id),
                subject_kind=subject_kind,
                task_kind=task_kind,
                provider=provider,
                model=model,
                effort=effort,
                rules=SamplingRules(window_days=window_days),
            )
        except (TypeError, ValueError) as error:
            raise HTTPException(422, str(error)) from None
        return await service.report(filters)

    return router
