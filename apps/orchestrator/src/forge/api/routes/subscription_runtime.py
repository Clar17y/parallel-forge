"""Authenticated reads of worker-published subscription registration status."""

# ruff: noqa: B008

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from forge.api.dependencies import require_operator
from forge.api.schemas.subscription_runtime import SubscriptionRuntimeStatusPage
from forge.application.services.auth import AuthenticatedActor


def router_for() -> APIRouter:
    router = APIRouter()

    @router.get("/subscription-runtime", response_model=SubscriptionRuntimeStatusPage)
    async def runtime_status(
        request: Request,
        offset: int = Query(0, ge=0, le=1_000_000),
        limit: int = Query(25, ge=1, le=100),
        _actor: AuthenticatedActor = Depends(require_operator),
    ) -> SubscriptionRuntimeStatusPage:
        query = request.app.state.subscription_runtime_status
        if query is None:
            raise HTTPException(503, "subscription runtime status unavailable")
        return SubscriptionRuntimeStatusPage.model_validate(
            await query.status(offset=offset, limit=limit)
        )

    return router
