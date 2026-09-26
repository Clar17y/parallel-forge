"""Authenticated aggregate subscription measurements."""

# ruff: noqa: B008

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from forge.api.dependencies import require_operator
from forge.api.schemas.subscription_usage import SubscriptionUsagePage
from forge.application.services.auth import AuthenticatedActor


def router_for() -> APIRouter:
    router = APIRouter()

    @router.get(
        "/subscription-usage",
        response_model=SubscriptionUsagePage,
        response_model_exclude_unset=True,
    )
    async def usage(
        request: Request,
        run_id: UUID | None = None,
        offset: int = Query(0, ge=0, le=1_000_000),
        limit: int = Query(25, ge=1, le=100),
        include_assessment: bool = False,
        _actor: AuthenticatedActor = Depends(require_operator),
    ) -> SubscriptionUsagePage:
        query = request.app.state.subscription_usage_query
        if query is None:
            raise HTTPException(503, "subscription usage unavailable")
        value = (
            await query.usage(run_id=run_id, offset=offset, limit=limit, include_assessment=True)
            if include_assessment
            else await query.usage(run_id=run_id, offset=offset, limit=limit)
        )
        if value is None:
            raise HTTPException(404, "run not found")
        return SubscriptionUsagePage.model_validate(value)

    return router
