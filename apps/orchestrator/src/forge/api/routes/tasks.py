"""Authenticated immutable task routes."""

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
from forge.api.schemas.tasks import TaskCreateRequest, TaskResponse
from forge.application.services.auth import AuthenticatedActor


def router_for() -> APIRouter:
    """Build task routes against services supplied by the application factory."""

    router = APIRouter()

    @router.get("/tasks", response_model=list[TaskResponse])
    async def list_tasks(
        request: Request,
        project_id: UUID = Query(...),  # noqa: B008
        _actor: AuthenticatedActor = Depends(require_operator),  # noqa: B008
    ) -> list[TaskResponse]:
        service = _service(request)
        try:
            records = await service.list(project_id)
        except Exception as error:  # noqa: BLE001 - translate service-boundary failures
            raise translate_error(error) from None
        return [TaskResponse.from_record(record) for record in records]

    @router.post("/tasks", response_model=TaskResponse, status_code=status.HTTP_201_CREATED)
    async def create_task(
        body: TaskCreateRequest,
        request: Request,
        idempotency_key: str = Depends(require_idempotency_key),
        actor: AuthenticatedActor = Depends(require_operator_mutation),  # noqa: B008
    ) -> TaskResponse:
        service = _service(request)
        try:
            task = await service.create_plain_text(
                actor=actor,
                idempotency_key=idempotency_key,
                request=body,
            )
        except Exception as error:  # noqa: BLE001 - translate service-boundary failures
            raise translate_error(error) from None
        return TaskResponse.from_record(task)

    @router.get("/tasks/{task_id}", response_model=TaskResponse)
    async def get_task(
        task_id: UUID,
        request: Request,
        _actor: AuthenticatedActor = Depends(require_operator),  # noqa: B008
    ) -> TaskResponse:
        service = _service(request)
        try:
            task = await service.get(task_id)
        except Exception as error:  # noqa: BLE001 - translate service-boundary failures
            raise translate_error(error) from None
        return TaskResponse.from_record(task)

    return router


def _service(request: Request) -> Any:
    service = getattr(request.app.state, "task_service", None)
    if service is None:
        raise HTTPException(status_code=500, detail="API service is not configured")
    return service


__all__ = ["router_for"]
