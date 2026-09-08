from typing import cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request

from forge.api.dependencies import require_operator
from forge.api.schemas.projections import DashboardSummary, RunProjection
from forge.application.services.auth import AuthenticatedActor
from forge.application.services.projections import ProjectionService
from forge.persistence.repositories.runs import PersistenceDataError


def _service(request: Request) -> ProjectionService:
    service = request.app.state.projection_service
    if service is None:
        raise HTTPException(503, "projection unavailable")
    return cast(ProjectionService, service)


def router_for() -> APIRouter:
    router = APIRouter()

    @router.get("/dashboard/summary", response_model=DashboardSummary)
    async def summary(
        request: Request,
        _actor: AuthenticatedActor = Depends(require_operator),  # noqa: B008
    ) -> DashboardSummary:
        return DashboardSummary.model_validate(await _service(request).summary())

    @router.get("/runs/{run_id}/projection", response_model=RunProjection)
    async def projection(
        run_id: UUID,
        request: Request,
        _actor: AuthenticatedActor = Depends(require_operator),  # noqa: B008
    ) -> RunProjection:
        try:
            value = await _service(request).run_projection(run_id, _actor)
            if value is None:
                raise HTTPException(404, "run not found")
            return RunProjection.model_validate(value)
        except ValueError, PersistenceDataError:
            raise HTTPException(503, "run projection unavailable") from None

    return router
