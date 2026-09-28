"""Authenticated read-only Jev usage report."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request

from forge.api.dependencies import require_operator
from forge.api.schemas.jev import JevReportResponse
from forge.application.services.auth import AuthenticatedActor


def router_for() -> APIRouter:
    router = APIRouter()

    @router.get("/runs/{run_id}/jev", response_model=JevReportResponse)
    async def report(
        run_id: UUID,
        request: Request,
        _actor: AuthenticatedActor = Depends(require_operator),  # noqa: B008
    ) -> JevReportResponse:
        service = getattr(request.app.state, "jev_reporting_service", None)
        if service is None:
            raise HTTPException(503, "Jev report unavailable")
        value = await service.report(run_id)
        if value is None:
            raise HTTPException(404, "run not found")
        return JevReportResponse.from_report(value)

    return router
