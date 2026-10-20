"""Authenticated run query, creation, and closed command routes."""

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
from forge.api.schemas.runs import (
    RunCommandRequest,
    RunCommandResponse,
    RunCreateRequest,
    RunResponse,
    RunWorkflowUpdateRequest,
)
from forge.application.services.auth import AuthenticatedActor
from forge.application.services.runs import WORKFLOW_OMITTED


def router_for() -> APIRouter:
    """Build run routes against services supplied by the application factory."""

    router = APIRouter()

    @router.get("/runs", response_model=list[RunResponse])
    async def list_runs(
        request: Request,
        project_id: UUID | None = Query(default=None),  # noqa: B008
        task_id: UUID | None = Query(default=None),  # noqa: B008
        _actor: AuthenticatedActor = Depends(require_operator),  # noqa: B008
    ) -> list[RunResponse]:
        service = _service(request, "run_service")
        try:
            records, selections = await service.list_with_profile_selections(
                project_id=project_id, task_id=task_id
            )
            return [
                RunResponse.from_snapshot(record, selections.get(record.id)) for record in records
            ]
        except Exception as error:  # noqa: BLE001 - translate service-boundary failures
            raise translate_error(error) from None

    @router.post("/runs", response_model=RunResponse, status_code=status.HTTP_201_CREATED)
    async def create_run(
        body: RunCreateRequest,
        request: Request,
        idempotency_key: str = Depends(require_idempotency_key),
        actor: AuthenticatedActor = Depends(require_operator_mutation),  # noqa: B008
    ) -> RunResponse:
        service = _service(request, "run_service")
        try:
            run = await service.create_run(
                actor=actor,
                idempotency_key=idempotency_key,
                task_id=body.task_id,
                profile_id=body.profile_id,
                profile_version=body.profile_version,
                workflow=(body.workflow.to_domain() if body.workflow is not None else None)
                if "workflow" in body.model_fields_set
                else WORKFLOW_OMITTED,
            )
            return RunResponse.from_snapshot(run, await _selection(service, run.id))
        except Exception as error:  # noqa: BLE001 - translate service-boundary failures
            raise translate_error(error) from None

    @router.put("/runs/{run_id}/workflow", response_model=RunResponse)
    async def update_run_workflow(
        run_id: UUID,
        body: RunWorkflowUpdateRequest,
        request: Request,
        idempotency_key: str = Depends(require_idempotency_key),
        actor: AuthenticatedActor = Depends(require_operator_mutation),  # noqa: B008
    ) -> RunResponse:
        service = _service(request, "run_service")
        try:
            run = await service.update_workflow(
                actor=actor,
                idempotency_key=idempotency_key,
                run_id=run_id,
                expected_run_version=body.expected_run_version,
                expected_workflow=(
                    body.expected_workflow.to_domain()
                    if body.expected_workflow is not None
                    else None
                ),
                workflow=body.workflow.to_domain(),
                note=body.note,
            )
            return RunResponse.from_snapshot(run, await _selection(service, run.id))
        except Exception as error:  # noqa: BLE001 - translate service-boundary failures
            raise translate_error(error) from None

    @router.get("/runs/{run_id}", response_model=RunResponse)
    async def get_run(
        run_id: UUID,
        request: Request,
        _actor: AuthenticatedActor = Depends(require_operator),  # noqa: B008
    ) -> RunResponse:
        service = _service(request, "run_service")
        try:
            run = await service.get(run_id)
            return RunResponse.from_snapshot(run, await _selection(service, run.id))
        except Exception as error:  # noqa: BLE001 - translate service-boundary failures
            raise translate_error(error) from None

    @router.post(
        "/runs/{run_id}/commands",
        response_model=RunCommandResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def enqueue_run_command(
        run_id: UUID,
        body: RunCommandRequest,
        request: Request,
        idempotency_key: str = Depends(require_idempotency_key),
        actor: AuthenticatedActor = Depends(require_operator_mutation),  # noqa: B008
    ) -> RunCommandResponse:
        service = _service(request, "run_command_service")
        try:
            command = await service.enqueue(
                actor=actor,
                run_id=run_id,
                idempotency_key=idempotency_key,
                request=body,
            )
        except Exception as error:  # noqa: BLE001 - translate service-boundary failures
            raise translate_error(error) from None
        return RunCommandResponse.from_command(command)

    return router


def _service(request: Request, name: str) -> Any:
    service = getattr(request.app.state, name, None)
    if service is None:
        raise HTTPException(status_code=500, detail="API service is not configured")
    return service


async def _selection(service: Any, run_id: UUID) -> Any:
    lookup = getattr(service, "profile_selection", None)
    return None if lookup is None else await lookup(run_id)


__all__ = ["router_for"]
