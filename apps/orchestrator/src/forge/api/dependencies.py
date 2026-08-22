"""FastAPI dependency boundaries for authenticated operator requests."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request, status

from forge.api.security import (
    CSRF_HEADER,
    SESSION_COOKIE,
    RequestSecurityError,
    require_same_origin,
)
from forge.application.services.auth import (
    AuthenticatedActor,
    AuthenticationError,
    CsrfError,
)
from forge.settings import Settings


def _settings(request: Request) -> Settings:
    value = getattr(request.app.state, "settings", None)
    if not isinstance(value, Settings):
        raise HTTPException(status_code=500, detail="API security is not configured")
    return value


def _auth_service(request: Request) -> Any:
    value = getattr(request.app.state, "auth_service", None)
    if value is None:
        raise HTTPException(status_code=500, detail="API security is not configured")
    return value


async def require_operator(request: Request) -> AuthenticatedActor:
    """Require Host and a current server-side operator session."""

    try:
        require_same_origin(request, _settings(request), require_origin=False)
    except RequestSecurityError:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="request not allowed")
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="authentication required"
        )
    try:
        actor = await _auth_service(request).require_session(token)
    except AuthenticationError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="authentication required"
        )
    if not isinstance(actor, AuthenticatedActor) or actor.actor_class != "operator":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="operator authorization required"
        )
    request.state.authenticated_actor = actor
    return actor


async def require_operator_mutation(request: Request) -> AuthenticatedActor:
    """Require exact same-origin, session cookie, and matching CSRF header."""

    try:
        require_same_origin(request, _settings(request), require_origin=True)
    except RequestSecurityError:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="request not allowed")
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="authentication required"
        )
    csrf = request.headers.get(CSRF_HEADER)
    try:
        actor = await _auth_service(request).require_session(
            token,
            csrf_token=csrf,
            require_csrf=True,
        )
    except CsrfError:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="request not allowed")
    except AuthenticationError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="authentication required"
        )
    if not isinstance(actor, AuthenticatedActor) or actor.actor_class != "operator":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="operator authorization required"
        )
    request.state.authenticated_actor = actor
    return actor


__all__ = ["require_operator", "require_operator_mutation"]
