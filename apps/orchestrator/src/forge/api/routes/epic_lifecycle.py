"""Authenticated epic execution, control, and ceiling actions."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status

from forge.api.dependencies import (
    require_idempotency_key,
    require_operator,
    require_operator_mutation,
)
from forge.api.errors import translate_error
from forge.application.services.auth import AuthenticatedActor
from forge.application.services.epic_budget import (
    EpicBudgetConflict,
    EpicBudgetEdit,
    EpicBudgetPermitReceipt,
    EpicBudgetPermitRequest,
    EpicBudgetProjection,
    EpicBudgetReceipt,
)
from forge.application.services.epic_dispatch import (
    EpicDispatchConflict,
    EpicDispatchProjection,
    EpicDispatchRequest,
)
from forge.application.services.epic_lifecycle import (
    EpicControlReceipt,
    EpicControlRequest,
    EpicExecutionProjection,
)
from forge.domain.epic_run_bridge import (
    EpicExecutionBindingConflict,
    EpicExecutionNotFound,
    EpicExecutionSnapshot,
    EpicLaunchConflict,
    ExecutionStartRequest,
)


def router_for() -> APIRouter:
    router = APIRouter()

    @router.get("/epics/{epic_id}/executions", response_model=tuple[EpicExecutionProjection, ...])
    async def list_executions(
        epic_id: UUID,
        request: Request,
        _actor: AuthenticatedActor = Depends(require_operator),  # noqa: B008
    ) -> tuple[EpicExecutionProjection, ...]:
        try:
            result = await request.app.state.epic_lifecycle_service.list(epic_id)
            return tuple(EpicExecutionProjection.model_validate(value) for value in result)
        except Exception as error:  # noqa: BLE001
            raise translate_error(error) from None

    @router.get(
        "/epics/{epic_id}/executions/{execution_id}", response_model=EpicExecutionProjection
    )
    async def get_execution(
        epic_id: UUID,
        execution_id: UUID,
        request: Request,
        _actor: AuthenticatedActor = Depends(require_operator),  # noqa: B008
    ) -> EpicExecutionProjection:
        try:
            result = await request.app.state.epic_lifecycle_service.get(epic_id, execution_id)
            return EpicExecutionProjection.model_validate(result)
        except EpicExecutionNotFound:
            raise HTTPException(status_code=404, detail="execution not found") from None
        except Exception as error:  # noqa: BLE001
            raise translate_error(error) from None

    @router.post(
        "/epics/{epic_id}/executions",
        response_model=EpicExecutionSnapshot,
        status_code=status.HTTP_201_CREATED,
    )
    async def start(
        epic_id: UUID,
        body: ExecutionStartRequest,
        request: Request,
        idempotency_key: str = Depends(require_idempotency_key),
        actor: AuthenticatedActor = Depends(require_operator_mutation),  # noqa: B008
    ) -> EpicExecutionSnapshot:
        service = request.app.state.epic_run_bridge_service
        try:
            result = await service.start(
                actor=actor,
                epic_id=epic_id,
                idempotency_key=idempotency_key,
                **body.model_dump(exclude={"schema_version"}),
            )
            return EpicExecutionSnapshot.model_validate(result)
        except (EpicLaunchConflict, EpicExecutionBindingConflict) as error:
            raise HTTPException(status_code=409, detail=str(error)) from None
        except Exception as error:  # noqa: BLE001
            raise translate_error(error) from None

    @router.post(
        "/epics/{epic_id}/executions/{execution_id}/commands",
        response_model=EpicControlReceipt,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def control(
        epic_id: UUID,
        execution_id: UUID,
        body: EpicControlRequest,
        request: Request,
        idempotency_key: str = Depends(require_idempotency_key),
        actor: AuthenticatedActor = Depends(require_operator_mutation),  # noqa: B008
    ) -> EpicControlReceipt:
        service = request.app.state.epic_lifecycle_service
        if service is None:
            raise HTTPException(status_code=503, detail="epic lifecycle unavailable")
        try:
            result = await service.request(
                actor=actor,
                epic_id=epic_id,
                execution_id=execution_id,
                idempotency_key=idempotency_key,
                request=body,
            )
            return EpicControlReceipt.model_validate(result)
        except EpicExecutionNotFound:
            raise HTTPException(status_code=404, detail="execution not found") from None
        except EpicLaunchConflict as error:
            raise HTTPException(
                status_code=409, detail={"blocker_codes": error.blocker_codes}
            ) from None
        except Exception as error:  # noqa: BLE001
            raise translate_error(error) from None

    @router.get(
        "/epics/{epic_id}/executions/{execution_id}/dispatch",
        response_model=EpicDispatchProjection,
    )
    async def get_dispatch(
        epic_id: UUID,
        execution_id: UUID,
        request: Request,
        _actor: AuthenticatedActor = Depends(require_operator),  # noqa: B008
    ) -> EpicDispatchProjection:
        try:
            return EpicDispatchProjection.model_validate(
                await request.app.state.epic_dispatch_service.get(epic_id, execution_id)
            )
        except EpicExecutionNotFound:
            raise HTTPException(status_code=404, detail="execution not found") from None
        except Exception as error:  # noqa: BLE001
            raise translate_error(error) from None

    @router.put(
        "/epics/{epic_id}/executions/{execution_id}/dispatch",
        response_model=EpicDispatchProjection,
    )
    async def configure_dispatch(
        epic_id: UUID,
        execution_id: UUID,
        body: EpicDispatchRequest,
        request: Request,
        idempotency_key: str = Depends(require_idempotency_key),
        actor: AuthenticatedActor = Depends(require_operator_mutation),  # noqa: B008
    ) -> EpicDispatchProjection:
        try:
            return EpicDispatchProjection.model_validate(
                await request.app.state.epic_dispatch_service.configure(
                    actor=actor, epic_id=epic_id, execution_id=execution_id,
                    idempotency_key=idempotency_key, request=body,
                )
            )
        except EpicExecutionNotFound:
            raise HTTPException(status_code=404, detail="execution not found") from None
        except EpicDispatchConflict:
            raise HTTPException(status_code=409, detail="dispatch version is stale") from None
        except Exception as error:  # noqa: BLE001
            raise translate_error(error) from None

    @router.put("/epics/{epic_id}/budget", response_model=EpicBudgetReceipt)
    async def edit_budget(
        epic_id: UUID,
        body: EpicBudgetEdit,
        request: Request,
        idempotency_key: str = Depends(require_idempotency_key),
        actor: AuthenticatedActor = Depends(require_operator_mutation),  # noqa: B008
    ) -> EpicBudgetReceipt:
        service = request.app.state.epic_budget_service
        if service is None:
            raise HTTPException(status_code=503, detail="epic budget unavailable")
        try:
            result = await service.edit(
                actor=actor, epic_id=epic_id, idempotency_key=idempotency_key, request=body
            )
            return EpicBudgetReceipt.model_validate(result)
        except EpicBudgetConflict as error:
            raise HTTPException(status_code=409, detail=str(error)) from None
        except Exception as error:  # noqa: BLE001
            raise translate_error(error) from None

    @router.get("/epics/{epic_id}/budget", response_model=EpicBudgetProjection)
    async def get_budget(
        epic_id: UUID,
        request: Request,
        _actor: AuthenticatedActor = Depends(require_operator),  # noqa: B008
    ) -> EpicBudgetProjection:
        service = request.app.state.epic_budget_service
        if service is None:
            raise HTTPException(status_code=503, detail="epic budget unavailable")
        try:
            result = await service.get(epic_id)
            return EpicBudgetProjection.model_validate(result)
        except Exception as error:  # noqa: BLE001
            raise translate_error(error) from None

    @router.post(
        "/epics/{epic_id}/budget/admissions",
        response_model=EpicBudgetPermitReceipt,
        status_code=status.HTTP_201_CREATED,
    )
    async def permit_budget_admission(
        epic_id: UUID,
        body: EpicBudgetPermitRequest,
        request: Request,
        idempotency_key: str = Depends(require_idempotency_key),
        actor: AuthenticatedActor = Depends(require_operator_mutation),  # noqa: B008
    ) -> EpicBudgetPermitReceipt:
        service = request.app.state.epic_budget_service
        if service is None:
            raise HTTPException(status_code=503, detail="epic budget unavailable")
        try:
            result = await service.permit(
                actor=actor, epic_id=epic_id, idempotency_key=idempotency_key, request=body
            )
            return EpicBudgetPermitReceipt.model_validate(result)
        except EpicBudgetConflict as error:
            raise HTTPException(status_code=409, detail=str(error)) from None
        except Exception as error:  # noqa: BLE001
            raise translate_error(error) from None

    return router
