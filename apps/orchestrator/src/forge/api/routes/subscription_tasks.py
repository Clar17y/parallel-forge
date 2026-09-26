"""Authenticated task inspection and versioned operator controls."""
# ruff: noqa: B008, BLE001

from typing import cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from forge.api.dependencies import (
    require_idempotency_key,
    require_operator,
    require_operator_mutation,
)
from forge.api.errors import translate_error
from forge.api.schemas.subscription_tasks import (
    SubscriptionAttemptPage,
    SubscriptionTaskPage,
    TaskControlRequest,
    TaskFeedbackRequest,
)
from forge.application.services.auth import AuthenticatedActor
from forge.application.services.subscription_feedback import SubscriptionTaskFeedbackService
from forge.application.services.subscription_task_controls import SubscriptionTaskControlService
from forge.domain.subscription_feedback import (
    SubscriptionTaskFeedbackRequest,
    TaskFeedbackReceipt,
)
from forge.domain.subscription_task_controls import (
    SubscriptionTaskControlRequest,
    TaskControlReceipt,
)


def router_for() -> APIRouter:
    router = APIRouter()

    @router.post(
        "/runs/{run_id}/subscription-tasks/{task_id}/feedback",
        response_model=TaskFeedbackReceipt,
    )
    async def submit_feedback(
        run_id: UUID,
        task_id: UUID,
        body: TaskFeedbackRequest,
        request: Request,
        idempotency_key: str = Depends(require_idempotency_key),
        actor: AuthenticatedActor = Depends(require_operator_mutation),
    ) -> TaskFeedbackReceipt:
        service = cast(
            SubscriptionTaskFeedbackService | None,
            getattr(request.app.state, "subscription_task_feedback_service", None),
        )
        if service is None:
            raise HTTPException(503, "task feedback unavailable")
        try:
            return await service.submit(
                run_id=run_id,
                task_id=task_id,
                actor=actor,
                idempotency_key=idempotency_key,
                request=SubscriptionTaskFeedbackRequest.model_validate(body.model_dump()),
            )
        except Exception as error:
            raise translate_error(error) from None

    @router.post(
        "/runs/{run_id}/subscription-tasks/{task_id}/controls", response_model=TaskControlReceipt
    )
    async def control_task(
        run_id: UUID,
        task_id: UUID,
        body: TaskControlRequest,
        request: Request,
        idempotency_key: str = Depends(require_idempotency_key),
        actor: AuthenticatedActor = Depends(require_operator_mutation),
    ) -> TaskControlReceipt:
        service = cast(
            SubscriptionTaskControlService | None,
            getattr(request.app.state, "subscription_task_control_service", None),
        )
        if service is None:
            raise HTTPException(503, "task controls unavailable")
        try:
            return await service.control(
                run_id=run_id,
                task_id=task_id,
                actor=actor,
                idempotency_key=idempotency_key,
                request=SubscriptionTaskControlRequest.model_validate(body.model_dump()),
            )
        except Exception as error:
            raise translate_error(error) from None

    @router.get("/runs/{run_id}/subscription-tasks", response_model=SubscriptionTaskPage)
    async def tasks(
        run_id: UUID,
        request: Request,
        offset: int = Query(default=0, ge=0, le=1_000_000),
        limit: int = Query(default=25, ge=1, le=100),
        _actor: AuthenticatedActor = Depends(require_operator),
    ) -> SubscriptionTaskPage:
        query = request.app.state.subscription_task_query
        if query is None:
            raise HTTPException(503, "subscription inspection unavailable")
        result = await query.tasks(run_id, offset=offset, limit=limit)
        if result is None:
            raise HTTPException(404, "run not found")
        return SubscriptionTaskPage.model_validate(result)

    @router.get(
        "/runs/{run_id}/subscription-tasks/{task_id}/attempts",
        response_model=SubscriptionAttemptPage,
    )
    async def attempts(
        run_id: UUID,
        task_id: UUID,
        request: Request,
        offset: int = Query(default=0, ge=0, le=1_000_000),
        limit: int = Query(default=25, ge=1, le=100),
        _actor: AuthenticatedActor = Depends(require_operator),
    ) -> SubscriptionAttemptPage:
        query = request.app.state.subscription_task_query
        if query is None:
            raise HTTPException(503, "subscription inspection unavailable")
        result = await query.attempts(run_id, task_id, offset=offset, limit=limit)
        if result is None:
            raise HTTPException(404, "task not found")
        return SubscriptionAttemptPage.model_validate(result)

    return router
