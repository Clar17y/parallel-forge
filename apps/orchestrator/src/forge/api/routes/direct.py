"""Direct autonomous run routes."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from forge.api.dependencies import require_operator, require_operator_mutation
from forge.api.errors import translate_error
from forge.api.schemas.direct import (
    DirectDiffResponse,
    DirectRouteResponse,
    DirectRunCreateRequest,
    DirectRunResponse,
    DirectTranscriptResponse,
)
from forge.application.services.auth import AuthenticatedActor


def router_for() -> APIRouter:
    router = APIRouter()

    @router.get("/direct-runs/routes", response_model=list[DirectRouteResponse])
    async def list_routes(
        request: Request,
        _actor: AuthenticatedActor = Depends(require_operator),  # noqa: B008
    ) -> list[DirectRouteResponse]:
        service = _service(request)
        try:
            return [DirectRouteResponse(**item) for item in service.routes()]
        except Exception as error:  # noqa: BLE001
            raise translate_error(error) from None

    @router.post(
        "/direct-runs", response_model=DirectRunResponse, status_code=status.HTTP_201_CREATED
    )
    async def create_run(
        body: DirectRunCreateRequest,
        request: Request,
        _actor: AuthenticatedActor = Depends(require_operator_mutation),  # noqa: B008
    ) -> DirectRunResponse:
        service = _service(request)
        try:
            record = await service.create(
                project_id=body.project_id, task=body.task, client=body.client,
                model=body.model, effort=body.effort, shape=body.shape,
                review_plan_first=body.review_plan_first,
            )
        except Exception as error:  # noqa: BLE001
            raise translate_error(error) from None
        return DirectRunResponse.from_record(record)

    @router.get("/direct-runs", response_model=list[DirectRunResponse])
    async def list_runs(
        request: Request,
        limit: int = Query(default=50, ge=1, le=100),
        _actor: AuthenticatedActor = Depends(require_operator),  # noqa: B008
    ) -> list[DirectRunResponse]:
        service = _service(request)
        try:
            records = await service.list(limit)
        except Exception as error:  # noqa: BLE001
            raise translate_error(error) from None
        return [DirectRunResponse.from_record(record) for record in records]

    @router.get("/direct-runs/{run_id}", response_model=DirectRunResponse)
    async def get_run(
        run_id: UUID,
        request: Request,
        _actor: AuthenticatedActor = Depends(require_operator),  # noqa: B008
    ) -> DirectRunResponse:
        service = _service(request)
        try:
            record = await service.get(run_id)
            sessions = await service.sessions(run_id)
            checks = await service.checks(run_id)
            reviews = await service.reviews(run_id)
        except Exception as error:  # noqa: BLE001
            raise translate_error(error) from None
        return DirectRunResponse.from_record(record, sessions, checks, reviews)

    @router.get("/direct-runs/{run_id}/transcript", response_model=DirectTranscriptResponse)
    async def get_transcript(
        run_id: UUID,
        request: Request,
        session: UUID | None = None,
        tail_bytes: int = Query(default=32768, ge=0, le=65536),
        _actor: AuthenticatedActor = Depends(require_operator),  # noqa: B008
    ) -> DirectTranscriptResponse:
        service = _service(request)
        try:
            text = await service.transcript(run_id, session, tail_bytes)
        except Exception as error:  # noqa: BLE001
            raise translate_error(error) from None
        return DirectTranscriptResponse(text=text)

    @router.get("/direct-runs/{run_id}/diff", response_model=DirectDiffResponse)
    async def get_diff(
        run_id: UUID,
        request: Request,
        _actor: AuthenticatedActor = Depends(require_operator),  # noqa: B008
    ) -> DirectDiffResponse:
        service = _service(request)
        try:
            text = await service.diff(run_id)
        except Exception as error:  # noqa: BLE001
            raise translate_error(error) from None
        return DirectDiffResponse(diff=text)

    @router.post("/direct-runs/{run_id}/approve-plan", response_model=DirectRunResponse)
    async def approve_plan(
        run_id: UUID,
        request: Request,
        _actor: AuthenticatedActor = Depends(require_operator_mutation),  # noqa: B008
    ) -> DirectRunResponse:
        service = _service(request)
        try:
            record = await service.approve_plan(run_id)
        except Exception as error:  # noqa: BLE001
            raise translate_error(error) from None
        return DirectRunResponse.from_record(record)

    @router.post("/direct-runs/{run_id}/cancel", response_model=DirectRunResponse)
    async def cancel_run(
        run_id: UUID,
        request: Request,
        _actor: AuthenticatedActor = Depends(require_operator_mutation),  # noqa: B008
    ) -> DirectRunResponse:
        service = _service(request)
        try:
            record = await service.cancel(run_id)
        except Exception as error:  # noqa: BLE001
            raise translate_error(error) from None
        return DirectRunResponse.from_record(record)

    return router


def _service(request: Request) -> Any:
    service = getattr(request.app.state, "direct_run_service", None)
    if service is None:
        raise HTTPException(status_code=500, detail="API service is not configured")
    return service


__all__ = ["router_for"]
