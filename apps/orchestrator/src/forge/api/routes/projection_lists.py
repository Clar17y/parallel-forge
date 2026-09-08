"""Authenticated dashboard list routes."""

from collections.abc import Awaitable, Callable
from typing import cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from forge.api.dependencies import require_operator
from forge.api.schemas.projection_lists import (
    AgentItem,
    ApprovalItem,
    AuditItem,
    EvaluationItem,
    ListPage,
    PermissionItem,
    PolicyProjection,
    UsageItem,
)
from forge.application.services.auth import AuthenticatedActor
from forge.persistence.queries.dashboard_lists import DashboardListQuery


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
        ("/audit", DashboardListQuery.audit, ListPage[AuditItem]),
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
