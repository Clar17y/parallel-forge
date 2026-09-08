"""Authenticated dashboard list routes."""

from collections.abc import Awaitable, Callable
from typing import Literal, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from forge.api.dependencies import require_operator
from forge.api.schemas.projection_lists import (
    AgentItem,
    ApprovalHistoryItem,
    ApprovalItem,
    AuditEventEvidence,
    AuditItem,
    EvaluationItem,
    ListPage,
    PermissionItem,
    PolicyProjection,
    RunUsageItem,
    UsageItem,
)
from forge.api.schemas.projections import CheckHistoryItem
from forge.application.services.auth import AuthenticatedActor
from forge.persistence.queries.dashboard_lists import DashboardListQuery
from forge.persistence.repositories.runs import PersistenceDataError


def _query(request: Request) -> DashboardListQuery:
    query = request.app.state.dashboard_list_query
    if query is None:
        raise HTTPException(503, "projection unavailable")
    return cast(DashboardListQuery, query)


def _page_params(
    offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)
) -> tuple[int, int]:
    return offset, limit


def _listing_handler(
    read: Callable[[DashboardListQuery, int, int], Awaitable[tuple[list[dict[str, object]], bool]]],
) -> Callable[..., Awaitable[dict[str, object]]]:
    async def listing(
        request: Request,
        params: tuple[int, int] = Depends(_page_params),
        _actor: AuthenticatedActor = Depends(require_operator),  # noqa: B008
    ) -> dict[str, object]:
        items, truncated = await read(_query(request), *params)
        return {"items": items, "offset": params[0], "limit": params[1], "truncated": truncated}

    return listing


def router_for() -> APIRouter:
    router = APIRouter()
    for path, read, model in (
        ("/approvals", DashboardListQuery.approvals, ListPage[ApprovalItem]),
        ("/usage", DashboardListQuery.usage, ListPage[UsageItem]),
        ("/agents", DashboardListQuery.agents, ListPage[AgentItem]),
        ("/evaluations", DashboardListQuery.evaluations, ListPage[EvaluationItem]),
    ):
        router.add_api_route(
            path,
            _listing_handler(read),
            methods=["GET"],
            response_model=model,
            name=path.lstrip("/"),
        )

    @router.get("/audit", response_model=ListPage[AuditItem])
    async def audit(
        request: Request,
        run_id: UUID | None = None,
        project_id: UUID | None = None,
        actor_id: UUID | None = None,
        operation_id: UUID | None = None,
        operation_status: Literal["PENDING", "SUCCEEDED", "FAILED", "NEEDS_RECONCILIATION"]
        | None = None,
        params: tuple[int, int] = Depends(_page_params),
        _actor: AuthenticatedActor = Depends(require_operator),  # noqa: B008
    ) -> dict[str, object]:
        try:
            items, truncated = await _query(request).audit(
                *params,
                run_id=run_id,
                project_id=project_id,
                actor_id=actor_id,
                operation_id=operation_id,
                operation_status=operation_status,
            )
        except ValueError, PersistenceDataError:
            raise HTTPException(503, "audit unavailable") from None
        return {"items": items, "offset": params[0], "limit": params[1], "truncated": truncated}

    @router.get("/audit/run-events/{event_id}", response_model=AuditEventEvidence)
    async def run_audit_detail(
        event_id: UUID,
        request: Request,
        _actor: AuthenticatedActor = Depends(require_operator),  # noqa: B008
    ) -> AuditEventEvidence:
        try:
            value = await _query(request).audit_detail(event_id, "run")
        except ValueError, PersistenceDataError:
            raise HTTPException(503, "audit unavailable") from None
        if value is None:
            raise HTTPException(404, "audit event not found")
        return AuditEventEvidence.model_validate(value)

    @router.get("/runs/{run_id}/approval-history", response_model=ListPage[ApprovalHistoryItem])
    async def approval_history(
        run_id: UUID,
        request: Request,
        params: tuple[int, int] = Depends(_page_params),
        _actor: AuthenticatedActor = Depends(require_operator),  # noqa: B008
    ) -> dict[str, object]:
        value = await _query(request).approval_history(run_id, *params)
        if value is None:
            raise HTTPException(404, "run not found")
        items, truncated = value
        return {"items": items, "offset": params[0], "limit": params[1], "truncated": truncated}

    @router.get("/runs/{run_id}/usage", response_model=ListPage[RunUsageItem])
    async def run_usage(
        run_id: UUID,
        request: Request,
        params: tuple[int, int] = Depends(_page_params),
        _actor: AuthenticatedActor = Depends(require_operator),  # noqa: B008
    ) -> dict[str, object]:
        value = await _query(request).run_usage(run_id, *params)
        if value is None:
            raise HTTPException(404, "run not found")
        items, truncated = value
        return {"items": items, "offset": params[0], "limit": params[1], "truncated": truncated}

    @router.get("/runs/{run_id}/checks", response_model=ListPage[CheckHistoryItem])
    async def checks(
        run_id: UUID,
        request: Request,
        params: tuple[int, int] = Depends(_page_params),
        _actor: AuthenticatedActor = Depends(require_operator),  # noqa: B008
    ) -> dict[str, object]:
        try:
            value = await _query(request).check_history(run_id, *params)
        except ValueError, PersistenceDataError:
            raise HTTPException(503, "check history unavailable") from None
        if value is None:
            raise HTTPException(404, "run not found")
        items, truncated = value
        return {"items": items, "offset": params[0], "limit": params[1], "truncated": truncated}

    @router.get("/audit/{event_id}", response_model=AuditEventEvidence)
    async def audit_detail(
        event_id: UUID,
        request: Request,
        _actor: AuthenticatedActor = Depends(require_operator),  # noqa: B008
    ) -> AuditEventEvidence:
        try:
            value = await _query(request).audit_detail(event_id)
        except ValueError, PersistenceDataError:
            raise HTTPException(503, "audit unavailable") from None
        if value is None:
            raise HTTPException(404, "audit event not found")
        return AuditEventEvidence.model_validate(value)

    @router.get("/projects/{project_id}/policy-projection", response_model=PolicyProjection)
    async def policy(
        project_id: UUID,
        request: Request,
        _actor: AuthenticatedActor = Depends(require_operator),  # noqa: B008
    ) -> PolicyProjection:
        value = await _query(request).policy(project_id)
        if value is None:
            raise HTTPException(404, "project policy not found")
        return PolicyProjection.model_validate(value)

    @router.get("/tool-permissions", response_model=ListPage[PermissionItem])
    async def permissions(
        request: Request,
        params: tuple[int, int] = Depends(_page_params),
        _actor: AuthenticatedActor = Depends(require_operator),  # noqa: B008
    ) -> dict[str, object]:
        items = await _query(request).tool_permissions()
        return {
            "items": items[params[0] : params[0] + params[1]],
            "offset": params[0],
            "limit": params[1],
            "truncated": len(items) > params[0] + params[1],
        }

    return router
