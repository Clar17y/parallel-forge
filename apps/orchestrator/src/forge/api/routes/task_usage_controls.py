"""Authenticated run checkpoint inspection and owner continuation."""
# ruff: noqa: B008

from __future__ import annotations

from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request

from forge.api.dependencies import require_operator, require_operator_mutation
from forge.api.schemas.task_usage import (
    TaskUsageCheckpointResponse,
    TaskUsageOwnerCommandRequest,
    TaskUsageOwnerCommandResponse,
)
from forge.application.services.auth import AuthenticatedActor
from forge.application.services.task_usage_controls import RunTaskUsageControls
from forge.domain.task_usage_contract import OwnerCommand
from forge.persistence.repositories.task_usage_checkpoints import CheckpointConflict
from forge.persistence.repositories.task_usage_policy import MonitoringPolicyConflict


def router_for() -> APIRouter:
    router = APIRouter()

    def service(request: Request) -> RunTaskUsageControls:
        selected = getattr(request.app.state, "run_task_usage_controls", None)
        if not isinstance(selected, RunTaskUsageControls):
            raise HTTPException(503, "run usage controls unavailable")
        return selected

    @router.get("/runs/{run_id}/usage-checkpoint", response_model=TaskUsageCheckpointResponse)
    async def checkpoint(
        run_id: UUID,
        request: Request,
        _actor: AuthenticatedActor = Depends(require_operator),
    ) -> TaskUsageCheckpointResponse:
        try:
            checkpoint, report = await service(request).get_with_report(run_id)
            return TaskUsageCheckpointResponse(checkpoint=checkpoint, report=report)
        except ValueError as error:
            raise HTTPException(404, str(error)) from None

    @router.post(
        "/runs/{run_id}/usage-checkpoint/commands",
        response_model=TaskUsageOwnerCommandResponse,
    )
    async def owner_command(
        run_id: UUID,
        body: TaskUsageOwnerCommandRequest,
        request: Request,
        actor: AuthenticatedActor = Depends(require_operator_mutation),
    ) -> TaskUsageOwnerCommandResponse:
        try:
            command = OwnerCommand(
                command_id=uuid4(),
                checkpoint_id=body.checkpoint_id,
                expected_version=body.expected_version,
                idempotency_key=body.idempotency_key,
                action=body.action,
                actor_id=actor.actor_id,
                continue_mode=body.continue_mode,
                requested_policy=body.requested_policy,
                override_note=body.override_note,
            )
            receipt, checkpoint = await service(request).apply(
                run_id=run_id,
                actor=actor,
                command=command,
            )
            return TaskUsageOwnerCommandResponse(receipt=receipt, checkpoint=checkpoint)
        except (CheckpointConflict, MonitoringPolicyConflict) as error:
            raise HTTPException(409, str(error)) from None
        except (TypeError, ValueError) as error:
            raise HTTPException(422, str(error)) from None

    return router
