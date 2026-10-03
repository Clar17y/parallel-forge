"""Authenticated epic work-item graph routes."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status

from forge.api.dependencies import (
    require_idempotency_key,
    require_operator,
    require_operator_mutation,
)
from forge.api.errors import translate_error
from forge.api.schemas.epic_brief import EpicResponse
from forge.api.schemas.epic_items import (
    AcceptedGraphResponse,
    GraphAdoptionRequest,
    GraphRevisionCreateRequest,
    GraphRevisionResponse,
)
from forge.application.services.auth import AuthenticatedActor


def router_for() -> APIRouter:
    """Build epic graph routes against services supplied by the application factory."""

    router = APIRouter()

    @router.post(
        "/epics/{epic_id}/graph-revisions",
        response_model=GraphRevisionResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_graph_revision(
        epic_id: UUID,
        body: GraphRevisionCreateRequest,
        request: Request,
        idempotency_key: str = Depends(require_idempotency_key),
        actor: AuthenticatedActor = Depends(require_operator_mutation),  # noqa: B008
    ) -> GraphRevisionResponse:
        service = _service(request)
        try:
            record = await service.save_revision(
                actor=actor,
                epic_id=epic_id,
                idempotency_key=idempotency_key,
                request=body,
            )
        except Exception as error:  # noqa: BLE001 - translate service-boundary failures
            raise translate_error(error) from None
        return GraphRevisionResponse.from_record(record)

    @router.get(
        "/epics/{epic_id}/graph-revisions",
        response_model=list[GraphRevisionResponse],
    )
    async def list_graph_revisions(
        epic_id: UUID,
        request: Request,
        _actor: AuthenticatedActor = Depends(require_operator),  # noqa: B008
    ) -> list[GraphRevisionResponse]:
        service = _service(request)
        try:
            records = await service.list_revisions(epic_id)
        except Exception as error:  # noqa: BLE001 - translate service-boundary failures
            raise translate_error(error) from None
        return [GraphRevisionResponse.from_record(record) for record in records]

    @router.get(
        "/epics/{epic_id}/graph-revisions/{graph_revision_id}",
        response_model=GraphRevisionResponse,
    )
    async def get_graph_revision(
        epic_id: UUID,
        graph_revision_id: UUID,
        request: Request,
        _actor: AuthenticatedActor = Depends(require_operator),  # noqa: B008
    ) -> GraphRevisionResponse:
        service = _service(request)
        try:
            record = await service.get_revision(epic_id, graph_revision_id)
        except Exception as error:  # noqa: BLE001 - translate service-boundary failures
            raise translate_error(error) from None
        return GraphRevisionResponse.from_record(record)

    @router.post(
        "/epics/{epic_id}/graph-adoptions",
        response_model=EpicResponse,
    )
    async def adopt_graph_revision(
        epic_id: UUID,
        body: GraphAdoptionRequest,
        request: Request,
        idempotency_key: str = Depends(require_idempotency_key),
        actor: AuthenticatedActor = Depends(require_operator_mutation),  # noqa: B008
    ) -> EpicResponse:
        service = _service(request)
        try:
            record = await service.adopt_revision(
                actor=actor,
                epic_id=epic_id,
                idempotency_key=idempotency_key,
                request=body,
            )
        except Exception as error:  # noqa: BLE001 - translate service-boundary failures
            raise translate_error(error) from None
        return EpicResponse.from_record(record)

    @router.get(
        "/epics/{epic_id}/accepted-graph",
        response_model=AcceptedGraphResponse,
    )
    async def get_accepted_graph(
        epic_id: UUID,
        request: Request,
        _actor: AuthenticatedActor = Depends(require_operator),  # noqa: B008
    ) -> AcceptedGraphResponse:
        service = _service(request)
        try:
            record = await service.accepted(epic_id)
        except Exception as error:  # noqa: BLE001 - translate service-boundary failures
            raise translate_error(error) from None
        return AcceptedGraphResponse.from_record(record)

    return router


def _service(request: Request) -> Any:
    service = getattr(request.app.state, "epic_items_service", None)
    if service is None:
        raise HTTPException(status_code=500, detail="API service is not configured")
    return service


__all__ = ["router_for"]
