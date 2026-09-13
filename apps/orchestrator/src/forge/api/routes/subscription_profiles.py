"""Authenticated operator profile endpoints."""
# ruff: noqa: B008, BLE001

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
from forge.api.schemas.subscription_profiles import (
    ProfileAppendRequest,
    ProfileCreateRequest,
    ProfileResponse,
    ProjectProfileSelectRequest,
)
from forge.application.services.auth import AuthenticatedActor


def router_for() -> APIRouter:
    router = APIRouter()

    @router.get("/subscription-profiles", response_model=list[ProfileResponse])
    async def list_profiles(
        request: Request, _actor: AuthenticatedActor = Depends(require_operator)
    ) -> list[ProfileResponse]:
        try:
            return [ProfileResponse.from_profile(value) for value in await _service(request).list()]
        except Exception as error:
            raise translate_error(error) from None

    @router.get(
        "/subscription-profiles/{profile_id}/versions/{version}", response_model=ProfileResponse
    )
    async def get_profile(
        profile_id: UUID,
        request: Request,
        version: int = Path(ge=1),
        _actor: AuthenticatedActor = Depends(require_operator),
    ) -> ProfileResponse:
        try:
            return ProfileResponse.from_profile(await _service(request).get(profile_id, version))
        except Exception as error:
            raise translate_error(error) from None

    @router.post(
        "/subscription-profiles",
        response_model=ProfileResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_profile(
        body: ProfileCreateRequest,
        request: Request,
        idempotency_key: str = Depends(require_idempotency_key),
        actor: AuthenticatedActor = Depends(require_operator_mutation),
    ) -> ProfileResponse:
        try:
            return ProfileResponse.from_profile(
                await _service(request).create(
                    actor=actor, idempotency_key=idempotency_key, request=body
                )
            )
        except Exception as error:
            raise translate_error(error) from None

    @router.post(
        "/subscription-profiles/{profile_id}/versions",
        response_model=ProfileResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def append_profile(
        profile_id: UUID,
        body: ProfileAppendRequest,
        request: Request,
        idempotency_key: str = Depends(require_idempotency_key),
        actor: AuthenticatedActor = Depends(require_operator_mutation),
    ) -> ProfileResponse:
        try:
            return ProfileResponse.from_profile(
                await _service(request).append(
                    actor=actor,
                    profile_id=profile_id,
                    idempotency_key=idempotency_key,
                    request=body,
                )
            )
        except Exception as error:
            raise translate_error(error) from None

    @router.get(
        "/projects/{project_id}/subscription-profile", response_model=ProfileResponse | None
    )
    async def selected_profile(
        project_id: UUID, request: Request, _actor: AuthenticatedActor = Depends(require_operator)
    ) -> ProfileResponse | None:
        try:
            value = await _service(request).selected(project_id)
            return None if value is None else ProfileResponse.from_profile(value)
        except Exception as error:
            raise translate_error(error) from None

    @router.put(
        "/projects/{project_id}/subscription-profile",
        response_model=ProfileResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def select_profile(
        project_id: UUID,
        body: ProjectProfileSelectRequest,
        request: Request,
        idempotency_key: str = Depends(require_idempotency_key),
        actor: AuthenticatedActor = Depends(require_operator_mutation),
    ) -> ProfileResponse:
        try:
            return ProfileResponse.from_profile(
                await _service(request).select(
                    actor=actor,
                    project_id=project_id,
                    idempotency_key=idempotency_key,
                    request=body,
                )
            )
        except Exception as error:
            raise translate_error(error) from None

    return router


def _service(request: Request) -> Any:
    value = getattr(request.app.state, "subscription_profile_service", None)
    if value is None:
        raise HTTPException(status_code=500, detail="API service is not configured")
    return value


__all__ = ["router_for"]
