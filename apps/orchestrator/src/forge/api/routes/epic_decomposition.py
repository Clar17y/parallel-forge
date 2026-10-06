"""Authenticated operator routes for epic decomposition authoring and adoption."""
# ruff: noqa: B008

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from forge.api.dependencies import (
    require_idempotency_key,
    require_operator,
    require_operator_mutation,
)
from forge.api.errors import translate_error
from forge.api.schemas.epic_decomposition import (
    DecompositionAdoptionResponse,
    DecompositionConversationCreate,
    DecompositionJobControl,
    DecompositionJobRetry,
    DecompositionJobSubmit,
    DecompositionProposalAdopt,
    DecompositionTurnAppend,
)
from forge.application.services.auth import AuthenticatedActor
from forge.application.services.epic_decomposition import EpicDecompositionService
from forge.domain.epic_brainstorm import (
    AuthoringOutcome,
    AuthoringReceipt,
    BrainstormConflict,
    BrainstormNotFound,
)
from forge.domain.epic_brief import BriefBindingConflict, BriefNotAccepted, EpicVersionConflict
from forge.domain.epic_decomposition import (
    DecompositionConflict,
    DecompositionNotFound,
    DecompositionValidationError,
)
from forge.domain.epic_items import GraphBindingConflict, GraphValidationError


def _service(request: Request) -> EpicDecompositionService:
    service = getattr(request.app.state, "epic_decomposition_service", None)
    if not isinstance(service, EpicDecompositionService):
        raise HTTPException(status_code=503, detail="epic decomposition unavailable")
    return service


def _error(error: Exception) -> HTTPException:
    if isinstance(error, (DecompositionNotFound, BrainstormNotFound)):
        return HTTPException(status_code=404, detail=str(error))
    if isinstance(
        error,
        (
            DecompositionConflict,
            BrainstormConflict,
            EpicVersionConflict,
            BriefBindingConflict,
            BriefNotAccepted,
            GraphBindingConflict,
        ),
    ):
        return HTTPException(status_code=409, detail=str(error))
    if isinstance(error, (DecompositionValidationError, GraphValidationError)):
        return HTTPException(status_code=422, detail=str(error))
    return translate_error(error)


def router_for() -> APIRouter:
    router = APIRouter()

    @router.post("/epics/{epic_id}/decomposition-conversations")
    async def create(
        epic_id: UUID,
        body: DecompositionConversationCreate,
        request: Request,
        key: str = Depends(require_idempotency_key),
        actor: AuthenticatedActor = Depends(require_operator_mutation),
    ) -> dict[str, UUID | int]:
        try:
            conversation_id, version = await _service(request).create(
                epic_id=epic_id, project_id=body.project_id, actor=actor, key=key, text=body.text
            )
            return {"conversation_id": conversation_id, "version": version}
        except Exception as error:  # noqa: BLE001
            raise _error(error) from None

    @router.get("/epics/{epic_id}/decomposition-conversations/{conversation_id}/turns")
    async def turns(
        epic_id: UUID,
        conversation_id: UUID,
        request: Request,
        project_id: UUID = Query(),
        _actor: AuthenticatedActor = Depends(require_operator),
    ) -> Any:
        try:
            return await _service(request).turns(
                epic_id=epic_id, project_id=project_id, conversation_id=conversation_id
            )
        except Exception as error:  # noqa: BLE001
            raise _error(error) from None

    @router.get("/epics/{epic_id}/decomposition-conversations")
    async def threads(
        epic_id: UUID,
        request: Request,
        project_id: UUID = Query(),
        _actor: AuthenticatedActor = Depends(require_operator),
    ) -> Any:
        try:
            return await _service(request).threads(epic_id=epic_id, project_id=project_id)
        except Exception as error:  # noqa: BLE001
            raise _error(error) from None

    @router.post("/epics/{epic_id}/decomposition-conversations/{conversation_id}/turns")
    async def append(
        epic_id: UUID,
        conversation_id: UUID,
        body: DecompositionTurnAppend,
        request: Request,
        key: str = Depends(require_idempotency_key),
        actor: AuthenticatedActor = Depends(require_operator_mutation),
    ) -> dict[str, int]:
        try:
            version = await _service(request).append(
                epic_id=epic_id,
                project_id=body.project_id,
                conversation_id=conversation_id,
                expected_version=body.expected_conversation_version,
                actor=actor,
                key=key,
                text=body.text,
                pending=body.pending,
            )
            return {"version": version}
        except Exception as error:  # noqa: BLE001
            raise _error(error) from None

    @router.post("/epics/{epic_id}/decomposition-conversations/{conversation_id}/jobs")
    async def submit(
        epic_id: UUID,
        conversation_id: UUID,
        body: DecompositionJobSubmit,
        request: Request,
        key: str = Depends(require_idempotency_key),
        actor: AuthenticatedActor = Depends(require_operator_mutation),
    ) -> AuthoringReceipt:
        try:
            return await _service(request).submit(
                epic_id=epic_id,
                project_id=body.project_id,
                conversation_id=conversation_id,
                prompt_turn_id=body.prompt_turn_id,
                expected_epic_version=body.expected_epic_version,
                expected_conversation_version=body.expected_conversation_version,
                actor=actor,
                key=key,
            )
        except Exception as error:  # noqa: BLE001
            raise _error(error) from None

    @router.get("/epics/{epic_id}/decomposition-jobs/{job_id}")
    async def observe(
        epic_id: UUID,
        job_id: UUID,
        request: Request,
        project_id: UUID = Query(),
        _actor: AuthenticatedActor = Depends(require_operator),
    ) -> AuthoringOutcome:
        try:
            return await _service(request).observe(
                epic_id=epic_id, project_id=project_id, job_id=job_id
            )
        except Exception as error:  # noqa: BLE001
            raise _error(error) from None

    @router.post("/epics/{epic_id}/decomposition-jobs/{job_id}/cancel")
    async def cancel(
        epic_id: UUID,
        job_id: UUID,
        body: DecompositionJobControl,
        request: Request,
        key: str = Depends(require_idempotency_key),
        actor: AuthenticatedActor = Depends(require_operator_mutation),
    ) -> AuthoringReceipt:
        try:
            return await _service(request).cancel(
                epic_id=epic_id,
                project_id=body.project_id,
                job_id=job_id,
                expected_job_version=body.expected_job_version,
                actor=actor,
                key=key,
            )
        except Exception as error:  # noqa: BLE001
            raise _error(error) from None

    @router.post("/epics/{epic_id}/decomposition-jobs/{job_id}/retry")
    async def retry(
        epic_id: UUID,
        job_id: UUID,
        body: DecompositionJobRetry,
        request: Request,
        key: str = Depends(require_idempotency_key),
        actor: AuthenticatedActor = Depends(require_operator_mutation),
    ) -> AuthoringReceipt:
        try:
            return await _service(request).retry(
                epic_id=epic_id,
                project_id=body.project_id,
                job_id=job_id,
                expected_job_version=body.expected_job_version,
                actor=actor,
                key=key,
                owner_override=body.owner_override,
                override_note=body.override_note,
            )
        except Exception as error:  # noqa: BLE001
            raise _error(error) from None

    @router.post("/epics/{epic_id}/decomposition-jobs/{job_id}/adopt")
    async def adopt(
        epic_id: UUID,
        job_id: UUID,
        body: DecompositionProposalAdopt,
        request: Request,
        key: str = Depends(require_idempotency_key),
        actor: AuthenticatedActor = Depends(require_operator_mutation),
    ) -> DecompositionAdoptionResponse:
        try:
            result = await _service(request).adopt(
                epic_id=epic_id,
                project_id=body.project_id,
                job_id=job_id,
                proposal_digest=body.proposal_digest,
                expected_job_version=body.expected_job_version,
                expected_epic_version=body.expected_epic_version,
                actor=actor,
                key=key,
                items=body.items,
            )
            return DecompositionAdoptionResponse(
                graph_revision_id=result.graph_revision_id,
                graph_digest=result.graph_digest,
                epic_version=result.epic_version,
                job_version=result.job_version,
            )
        except Exception as error:  # noqa: BLE001
            raise _error(error) from None

    return router


__all__ = ["router_for"]
