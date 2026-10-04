"""Authenticated epic work-item launch and attempt projections."""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status

from forge.api.dependencies import (
    require_idempotency_key,
    require_operator,
    require_operator_mutation,
)
from forge.api.errors import translate_error
from forge.api.schemas.epic_run_bridge import EpicAttemptResponse, EpicLaunchRequest
from forge.application.services.auth import AuthenticatedActor
from forge.domain.epic_run_bridge import (
    EpicAttemptNotFound,
    EpicExecutionBindingConflict,
    EpicExecutionNotFound,
    EpicLaunchConflict,
)


def router_for() -> APIRouter:
    router = APIRouter()

    @router.post(
        "/epics/{epic_id}/work-item-runs",
        response_model=EpicAttemptResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def launch(
        epic_id: UUID,
        body: EpicLaunchRequest,
        request: Request,
        idempotency_key: str = Depends(require_idempotency_key),
        actor: AuthenticatedActor = Depends(require_operator_mutation),  # noqa: B008
    ) -> EpicAttemptResponse:
        service = _service(request)
        try:
            attempt = await service.launch(
                actor=actor, epic_id=epic_id, idempotency_key=idempotency_key, request=body
            )
        except EpicLaunchConflict as error:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "epic_launch_blocked",
                    "blocker_codes": list(error.blocker_codes),
                    "actual_epic_version": error.actual_epic_version,
                    "owner_action": "retry_with_owner_override",
                },
            ) from None
        except EpicAttemptNotFound, EpicExecutionNotFound:
            raise HTTPException(status_code=404, detail="resource not found") from None
        except EpicExecutionBindingConflict:
            raise HTTPException(
                status_code=409, detail="execution source binding does not match"
            ) from None
        except Exception as error:  # noqa: BLE001
            raise translate_error(error) from None
        return EpicAttemptResponse(**dict(attempt))

    @router.get("/epics/{epic_id}/work-item-runs", response_model=list[EpicAttemptResponse])
    async def list_attempts(
        epic_id: UUID,
        request: Request,
        _actor: AuthenticatedActor = Depends(require_operator),  # noqa: B008
    ) -> list[EpicAttemptResponse]:
        service = _service(request)
        try:
            records = await service.list(epic_id)
        except Exception as error:  # noqa: BLE001
            raise translate_error(error) from None
        return [EpicAttemptResponse(**dict(value)) for value in records]

    @router.get("/epics/{epic_id}/work-item-runs/{attempt_id}", response_model=EpicAttemptResponse)
    async def get_attempt(
        epic_id: UUID,
        attempt_id: UUID,
        request: Request,
        _actor: AuthenticatedActor = Depends(require_operator),  # noqa: B008
    ) -> EpicAttemptResponse:
        service = _service(request)
        try:
            record = await service.get(epic_id, attempt_id)
        except EpicAttemptNotFound:
            raise HTTPException(status_code=404, detail="resource not found") from None
        except Exception as error:  # noqa: BLE001
            raise translate_error(error) from None
        return EpicAttemptResponse(**dict(record))

    return router


def _service(request: Request) -> Any:
    service = getattr(request.app.state, "epic_run_bridge_service", None)
    if service is None:
        raise HTTPException(status_code=500, detail="API service is not configured")
    return service
