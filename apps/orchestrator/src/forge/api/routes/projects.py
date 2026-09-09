"""Authenticated project and immutable policy routes."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Request, status

from forge.api.dependencies import (
    require_idempotency_key,
    require_operator,
    require_operator_mutation,
)
from forge.api.errors import translate_error
from forge.api.schemas.projects import (
    ProjectCreateRequest,
    ProjectPolicyResponse,
    ProjectPolicyUpdateRequest,
    ProjectResponse,
)
from forge.application.services.auth import AuthenticatedActor


def router_for() -> APIRouter:
    """Build project routes against services supplied by the application factory."""

    router = APIRouter()

    @router.get("/projects", response_model=list[ProjectResponse])
    async def list_projects(
        request: Request,
        _actor: AuthenticatedActor = Depends(require_operator),  # noqa: B008
    ) -> list[ProjectResponse]:
        service = _service(request, "project_service")
        try:
            records = await service.list()
        except Exception as error:  # noqa: BLE001 - translate service-boundary failures
            raise translate_error(error) from None
        return [
            ProjectResponse.from_record(
                record, issue_import_available=_issue_import_available(request)
            )
            for record in records
        ]

    @router.get(
        "/projects/{project_id}/policy-versions/{version}", response_model=ProjectPolicyResponse
    )
    async def get_project_policy(
        project_id: UUID,
        request: Request,
        version: int = Path(ge=1),
        _actor: AuthenticatedActor = Depends(require_operator),  # noqa: B008
    ) -> ProjectPolicyResponse:
        try:
            policy = await _service(request, "project_service").get_policy(project_id, version)
        except Exception as error:  # noqa: BLE001 - translate service-boundary failures
            raise translate_error(error) from None
        return ProjectPolicyResponse.from_record(policy)

    @router.post(
        "/projects",
        response_model=ProjectResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def register_project(
        body: ProjectCreateRequest,
        request: Request,
        idempotency_key: str = Depends(require_idempotency_key),
        actor: AuthenticatedActor = Depends(require_operator_mutation),  # noqa: B008
    ) -> ProjectResponse:
        service = _service(request, "project_service")
        try:
            project = await service.register(
                actor=actor,
                idempotency_key=idempotency_key,
                request=body,
            )
        except Exception as error:  # noqa: BLE001 - translate service-boundary failures
            raise translate_error(error) from None
        return ProjectResponse.from_record(
            project, issue_import_available=_issue_import_available(request)
        )

    @router.get("/projects/{project_id}", response_model=ProjectResponse)
    async def get_project(
        project_id: UUID,
        request: Request,
        _actor: AuthenticatedActor = Depends(require_operator),  # noqa: B008
    ) -> ProjectResponse:
        service = _service(request, "project_service")
        try:
            project = await service.get(project_id)
        except Exception as error:  # noqa: BLE001 - translate service-boundary failures
            raise translate_error(error) from None
        return ProjectResponse.from_record(
            project, issue_import_available=_issue_import_available(request)
        )

    @router.post(
        "/projects/{project_id}/policy-versions",
        response_model=ProjectPolicyResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def update_project_policy(
        project_id: UUID,
        body: ProjectPolicyUpdateRequest,
        request: Request,
        idempotency_key: str = Depends(require_idempotency_key),
        actor: AuthenticatedActor = Depends(require_operator_mutation),  # noqa: B008
    ) -> ProjectPolicyResponse:
        service = _service(request, "project_service")
        try:
            policy = await service.update_policy(
                actor=actor,
                project_id=project_id,
                idempotency_key=idempotency_key,
                request=body,
            )
        except Exception as error:  # noqa: BLE001 - translate service-boundary failures
            raise translate_error(error) from None
        return ProjectPolicyResponse.from_record(policy)

    return router


def _service(request: Request, name: str) -> Any:
    service = getattr(request.app.state, name, None)
    if service is None:
        raise HTTPException(status_code=500, detail="API service is not configured")
    return service


def _issue_import_available(request: Request) -> bool:
    return getattr(request.app.state, "github_issue_import_service", None) is not None


__all__ = ["router_for"]
