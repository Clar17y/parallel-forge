"""Operator-only discovery routes; registration is integration-owned."""
# ruff: noqa: B008

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from forge.api.dependencies import (
    require_idempotency_key,
    require_operator,
    require_operator_mutation,
)
from forge.api.schemas.epic_brainstorm import (
    ConversationCreate,
    JobControl,
    JobRetry,
    JobSubmit,
    ProposalAdopt,
    TurnAppend,
)
from forge.application.services.auth import AuthenticatedActor
from forge.application.services.epic_brainstorm import EpicBrainstormService
from forge.domain.epic_brainstorm import (
    AuthoringOutcome,
    AuthoringReceipt,
    BrainstormConflict,
    BrainstormNotFound,
    BrainstormThread,
    BrainstormTurn,
)


def _service(request: Request) -> EpicBrainstormService:
    service = getattr(request.app.state, "epic_brainstorm_service", None)
    if not isinstance(service, EpicBrainstormService):
        raise HTTPException(status_code=503, detail="epic brainstorming unavailable")
    return service


def _error(error: Exception) -> HTTPException:
    if isinstance(error, BrainstormNotFound):
        return HTTPException(status_code=404, detail=str(error))
    if isinstance(error, BrainstormConflict):
        return HTTPException(status_code=409, detail=str(error))
    if isinstance(error, ValueError):
        return HTTPException(status_code=422, detail=str(error))
    raise error


def router_for() -> APIRouter:
    router = APIRouter()

    @router.post("/epics/{epic_id}/brainstorm-conversations")
    async def create(
        epic_id: UUID,
        body: ConversationCreate,
        request: Request,
        key: str = Depends(require_idempotency_key),
        actor: AuthenticatedActor = Depends(require_operator_mutation),
    ) -> dict[str, UUID | int]:
        try:
            conversation_id, version = await _service(request).create(
                epic_id=epic_id, project_id=body.project_id, actor=actor, key=key, text=body.text
            )
            return {"conversation_id": conversation_id, "version": version}
        except ValueError as error:
            raise _error(error) from None

    @router.get("/epics/{epic_id}/brainstorm-conversations/{conversation_id}/turns")
    async def turns(
        epic_id: UUID,
        conversation_id: UUID,
        request: Request,
        project_id: UUID = Query(),
        _actor: AuthenticatedActor = Depends(require_operator),
    ) -> tuple[BrainstormTurn, ...]:
        try:
            return await _service(request).turns(
                epic_id=epic_id, project_id=project_id, conversation_id=conversation_id
            )
        except ValueError as error:
            raise _error(error) from None

    @router.get("/epics/{epic_id}/brainstorm-conversations")
    async def threads(
        epic_id: UUID,
        request: Request,
        project_id: UUID = Query(),
        _actor: AuthenticatedActor = Depends(require_operator),
    ) -> tuple[BrainstormThread, ...]:
        return await _service(request).threads(
            epic_id=epic_id, project_id=project_id, kind="brainstorm"
        )

    @router.post("/epics/{epic_id}/brainstorm-conversations/{conversation_id}/turns")
    async def append(
        epic_id: UUID,
        conversation_id: UUID,
        body: TurnAppend,
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
        except ValueError as error:
            raise _error(error) from None

    @router.post("/epics/{epic_id}/brainstorm-conversations/{conversation_id}/jobs")
    async def submit(
        epic_id: UUID,
        conversation_id: UUID,
        body: JobSubmit,
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
                requested_route=body.requested_route.route() if body.requested_route else None,
            )
        except ValueError as error:
            raise _error(error) from None

    @router.get("/epics/{epic_id}/brainstorm-jobs/{job_id}")
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
        except ValueError as error:
            raise _error(error) from None

    @router.post("/epics/{epic_id}/brainstorm-jobs/{job_id}/cancel")
    async def cancel(
        epic_id: UUID,
        job_id: UUID,
        body: JobControl,
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
        except ValueError as error:
            raise _error(error) from None

    @router.post("/epics/{epic_id}/brainstorm-jobs/{job_id}/retry")
    async def retry(
        epic_id: UUID,
        job_id: UUID,
        body: JobRetry,
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
        except ValueError as error:
            raise _error(error) from None

    @router.post("/epics/{epic_id}/brainstorm-jobs/{job_id}/adopt")
    async def adopt(
        epic_id: UUID,
        job_id: UUID,
        body: ProposalAdopt,
        request: Request,
        key: str = Depends(require_idempotency_key),
        actor: AuthenticatedActor = Depends(require_operator_mutation),
    ) -> dict[str, UUID]:
        try:
            revision_id = await _service(request).adopt(
                epic_id=epic_id,
                project_id=body.project_id,
                job_id=job_id,
                proposal_digest=body.proposal_digest,
                expected_job_version=body.expected_job_version,
                expected_epic_version=body.expected_epic_version,
                actor=actor,
                key=key,
            )
            return {"brief_revision_id": revision_id}
        except ValueError as error:
            raise _error(error) from None

    return router
