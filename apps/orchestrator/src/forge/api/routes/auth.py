"""Operator bootstrap, session inspection, and logout routes."""

from __future__ import annotations

from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from forge.api.dependencies import require_operator, require_operator_mutation
from forge.api.security import (
    RequestSecurityError,
    clear_session_cookie,
    require_same_origin,
    set_session_cookie,
)
from forge.application.services.auth import (
    AuthenticatedActor,
    AuthenticatedSession,
    AuthenticationError,
    SessionInfo,
)


class BootstrapRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=1)


class BootstrapResponse(BaseModel):
    actor_id: str
    actor_class: str
    csrf_token: str
    expires_at: str
    idle_expires_at: str
    absolute_expires_at: str


class SessionResponse(BaseModel):
    actor_id: str
    actor_class: str
    expires_at: str
    idle_expires_at: str
    absolute_expires_at: str


def router_for() -> APIRouter:
    router = APIRouter()

    @router.post("/auth/bootstrap", response_model=BootstrapResponse)
    async def bootstrap(
        body: BootstrapRequest,
        request: Request,
        response: Response,
    ) -> BootstrapResponse:
        try:
            origin = require_same_origin(
                request,
                request.app.state.settings,
                require_origin=True,
            )
        except RequestSecurityError, ValueError:
            raise HTTPException(status_code=403, detail="request not allowed") from None
        try:
            session = await cast(Any, request.app.state.auth_service).exchange_bootstrap(body.token)
        except AuthenticationError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="authentication required",
            ) from None
        if not isinstance(session, AuthenticatedSession):
            raise HTTPException(status_code=500, detail="API security is not configured")
        set_session_cookie(response, session.session_token, origin)
        return BootstrapResponse(
            actor_id=str(session.actor_id),
            actor_class=session.actor_class,
            csrf_token=session.csrf_token,
            expires_at=session.expires_at.isoformat(),
            idle_expires_at=session.idle_expires_at.isoformat(),
            absolute_expires_at=session.absolute_expires_at.isoformat(),
        )

    @router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
    async def logout(
        request: Request,
        response: Response,
        actor: AuthenticatedActor = Depends(require_operator_mutation),  # noqa: B008
    ) -> Response:
        origin = require_same_origin(
            request,
            request.app.state.settings,
            require_origin=True,
        )
        await cast(Any, request.app.state.auth_service).logout(actor)
        clear_session_cookie(response, origin)
        response.status_code = status.HTTP_204_NO_CONTENT
        return response

    @router.get("/auth/session", response_model=SessionResponse)
    async def session(
        request: Request,
        actor: AuthenticatedActor = Depends(require_operator),  # noqa: B008
    ) -> SessionResponse:
        info = await cast(Any, request.app.state.auth_service).session_info(actor)
        if not isinstance(info, SessionInfo):
            raise HTTPException(status_code=500, detail="API security is not configured")
        return SessionResponse(
            actor_id=str(info.actor.actor_id),
            actor_class=info.actor.actor_class,
            expires_at=info.expires_at.isoformat(),
            idle_expires_at=info.idle_expires_at.isoformat(),
            absolute_expires_at=info.absolute_expires_at.isoformat(),
        )

    return router


__all__ = ["router_for"]
