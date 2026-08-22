"""Evidence-bound approval challenge and authorization routes."""

from __future__ import annotations

from typing import cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from forge.api.dependencies import require_operator_mutation
from forge.application.services.approvals import (
    ApprovalAuthorizationService,
    ApprovalChallengeService,
    AuthorizationError,
)
from forge.application.services.auth import AuthenticatedActor


class ApprovalChallengeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    gate: str = Field(min_length=1)
    run_version: int = Field(ge=0)
    evidence_digest: str = Field(min_length=1)
    policy_version: int | None = Field(default=None, ge=1)


class ApprovalChallengeResponse(BaseModel):
    token: str
    expires_at: str


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    gate: str = Field(min_length=1)
    run_version: int = Field(ge=0)
    evidence_digest: str = Field(min_length=1)
    challenge_token: str | None = Field(default=None, min_length=1)
    challenge: str | None = Field(default=None, min_length=1)

    def token(self) -> str | None:
        return self.challenge_token or self.challenge


class ApprovalResponse(BaseModel):
    approval_id: str


def router_for() -> APIRouter:
    router = APIRouter()

    @router.post(
        "/runs/{run_id}/approval-challenges",
        response_model=ApprovalChallengeResponse,
        status_code=status.HTTP_200_OK,
    )
    async def issue_challenge(
        run_id: UUID,
        body: ApprovalChallengeRequest,
        request: Request,
        actor: AuthenticatedActor = Depends(require_operator_mutation),  # noqa: B008
    ) -> ApprovalChallengeResponse:
        try:
            challenge_service = cast(
                ApprovalChallengeService, request.app.state.approval_challenge_service
            )
            challenge = await challenge_service.issue(
                session_id=actor.session_id,
                actor_id=actor.actor_id,
                run_id=run_id,
                gate=body.gate,
                run_version=body.run_version,
                policy_version=body.policy_version,
                evidence_digest=body.evidence_digest,
            )
        except AuthorizationError as error:
            if str(error) == "invalid or expired session":
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="authentication required",
                ) from None
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="approval challenge not issued",
            ) from None
        return ApprovalChallengeResponse(
            token=challenge.token,
            expires_at=challenge.expires_at.isoformat(),
        )

    @router.post(
        "/runs/{run_id}/approvals",
        response_model=ApprovalResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def authorize(
        run_id: UUID,
        body: ApprovalRequest,
        request: Request,
        actor: AuthenticatedActor = Depends(require_operator_mutation),  # noqa: B008
    ) -> ApprovalResponse:
        service = cast(
            ApprovalAuthorizationService, request.app.state.approval_authorization_service
        )
        token = body.token()
        if token is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="approval not authorized"
            )
        try:
            approval = await service.authorize(
                actor=actor,
                run_id=run_id,
                gate=body.gate,
                run_version=body.run_version,
                evidence_digest=body.evidence_digest,
                challenge_token=token,
            )
        except AuthorizationError as error:
            detail = str(error)
            if detail == "invalid or expired session":
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="authentication required",
                ) from None
            if detail == "operator authorization required":
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="operator authorization required",
                ) from None
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="approval not authorized",
            ) from None
        if approval.id is None:
            raise HTTPException(status_code=500, detail="approval persistence is not configured")
        return ApprovalResponse(approval_id=str(approval.id))

    return router


__all__ = ["router_for"]
