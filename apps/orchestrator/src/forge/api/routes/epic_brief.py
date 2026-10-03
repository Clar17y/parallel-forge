"""Authenticated epic and brief routes."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from forge.api.dependencies import (
    require_idempotency_key,
    require_operator,
    require_operator_mutation,
)
from forge.api.errors import translate_error
from forge.api.schemas.epic_brief import (
    AcceptedBriefResponse,
    BriefAdoptionRequest,
    BriefRevisionCreateRequest,
    BriefRevisionResponse,
    EpicCreateRequest,
    EpicDraftUpdateRequest,
    EpicResponse,
)
from forge.application.services.auth import AuthenticatedActor


def router_for() -> APIRouter:
    """Build epic brief routes against services supplied by the application factory."""

    router = APIRouter()

    @router.post(
        "/epics",
        response_model=EpicResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_epic(
        body: EpicCreateRequest,
        request: Request,
        idempotency_key: str = Depends(require_idempotency_key),
        actor: AuthenticatedActor = Depends(require_operator_mutation),  # noqa: B008
    ) -> EpicResponse:
        service = _service(request)
        try:
            record = await service.create(
                actor=actor,
                idempotency_key=idempotency_key,
                request=body,
            )
        except Exception as error:  # noqa: BLE001 - translate service-boundary failures
            raise translate_error(error) from None
        return EpicResponse.from_record(record)

    @router.get("/epics", response_model=list[EpicResponse])
    async def list_epics(
        request: Request,
        project_id: UUID = Query(...),  # noqa: B008
        _actor: AuthenticatedActor = Depends(require_operator),  # noqa: B008
    ) -> list[EpicResponse]:
        service = _service(request)
        try:
            records = await service.list(project_id)
        except Exception as error:  # noqa: BLE001 - translate service-boundary failures
            raise translate_error(error) from None
        return [EpicResponse.from_record(record) for record in records]

    @router.get("/epics/{epic_id}", response_model=EpicResponse)
    async def get_epic(
        epic_id: UUID,
        request: Request,
        _actor: AuthenticatedActor = Depends(require_operator),  # noqa: B008
    ) -> EpicResponse:
        service = _service(request)
        try:
            record = await service.get(epic_id)
        except Exception as error:  # noqa: BLE001 - translate service-boundary failures
            raise translate_error(error) from None
        return EpicResponse.from_record(record)

    @router.patch("/epics/{epic_id}", response_model=EpicResponse)
    async def update_epic_draft(
        epic_id: UUID,
        body: EpicDraftUpdateRequest,
        request: Request,
        idempotency_key: str = Depends(require_idempotency_key),
        actor: AuthenticatedActor = Depends(require_operator_mutation),  # noqa: B008
    ) -> EpicResponse:
        service = _service(request)
        try:
            record = await service.update_draft(
                actor=actor,
                epic_id=epic_id,
                idempotency_key=idempotency_key,
                request=body,
            )
        except Exception as error:  # noqa: BLE001 - translate service-boundary failures
            raise translate_error(error) from None
        return EpicResponse.from_record(record)

    @router.post(
        "/epics/{epic_id}/brief-revisions",
        response_model=BriefRevisionResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_brief_revision(
        epic_id: UUID,
        body: BriefRevisionCreateRequest,
        request: Request,
        idempotency_key: str = Depends(require_idempotency_key),
        actor: AuthenticatedActor = Depends(require_operator_mutation),  # noqa: B008
    ) -> BriefRevisionResponse:
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
        return BriefRevisionResponse.from_record(record)

    @router.get("/epics/{epic_id}/brief-revisions", response_model=list[BriefRevisionResponse])
    async def list_brief_revisions(
        epic_id: UUID,
        request: Request,
        _actor: AuthenticatedActor = Depends(require_operator),  # noqa: B008
    ) -> list[BriefRevisionResponse]:
        service = _service(request)
        try:
            records = await service.list_revisions(epic_id)
        except Exception as error:  # noqa: BLE001 - translate service-boundary failures
            raise translate_error(error) from None
        return [BriefRevisionResponse.from_record(record) for record in records]

    @router.get(
        "/epics/{epic_id}/brief-revisions/{brief_revision_id}",
        response_model=BriefRevisionResponse,
    )
    async def get_brief_revision(
        epic_id: UUID,
        brief_revision_id: UUID,
        request: Request,
        _actor: AuthenticatedActor = Depends(require_operator),  # noqa: B008
    ) -> BriefRevisionResponse:
        service = _service(request)
        try:
            record = await service.get_revision(epic_id, brief_revision_id)
        except Exception as error:  # noqa: BLE001 - translate service-boundary failures
            raise translate_error(error) from None
        return BriefRevisionResponse.from_record(record)

    @router.post(
        "/epics/{epic_id}/brief-adoptions",
        response_model=EpicResponse,
    )
    async def adopt_brief_revision(
        epic_id: UUID,
        body: BriefAdoptionRequest,
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

    @router.get("/epics/{epic_id}/accepted-brief", response_model=AcceptedBriefResponse)
    async def get_accepted_brief(
        epic_id: UUID,
        request: Request,
        _actor: AuthenticatedActor = Depends(require_operator),  # noqa: B008
    ) -> AcceptedBriefResponse:
        service = _service(request)
        try:
            record = await service.accepted(epic_id)
        except Exception as error:  # noqa: BLE001 - translate service-boundary failures
            raise translate_error(error) from None
        return AcceptedBriefResponse.from_record(record)

    return router


def _service(request: Request) -> Any:
    service = getattr(request.app.state, "epic_brief_service", None)
    if service is None:
        raise HTTPException(status_code=500, detail="API service is not configured")
    return service


__all__ = ["router_for"]
