"""HTTP boundary tests for bootstrap/session security."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from forge.api.app import create_app
from forge.application.services.auth import (
    AuthenticatedActor,
    AuthenticatedSession,
    AuthenticationError,
    CsrfError,
    SessionInfo,
)
from forge.settings import Settings
from httpx import ASGITransport, AsyncClient


class FakeAuthService:
    def __init__(self) -> None:
        self.session = AuthenticatedSession(
            session_id=uuid4(),
            actor_id=uuid4(),
            actor_class="operator",
            expires_at=datetime(2026, 1, 1, 12, tzinfo=UTC),
            idle_expires_at=datetime(2026, 1, 1, 0, 30, tzinfo=UTC),
            absolute_expires_at=datetime(2026, 1, 1, 12, tzinfo=UTC),
            session_token="session-raw",
            csrf_token="csrf-raw",
        )
        self.require_error: Exception | None = None
        self.require_calls = 0
        self.logout_calls = 0

    async def exchange_bootstrap(self, token: str) -> AuthenticatedSession:
        assert token == "bootstrap-raw"
        return self.session

    async def require_session(
        self,
        token: str,
        *,
        csrf_token: str | None = None,
        require_csrf: bool = False,
    ) -> AuthenticatedActor:
        self.require_calls += 1
        if self.require_error is not None:
            raise self.require_error
        if token != self.session.session_token:
            raise AuthenticationError("invalid or expired session")
        if require_csrf and csrf_token != self.session.csrf_token:
            raise CsrfError("invalid csrf token")
        return self.session.actor

    async def session_info(self, actor: AuthenticatedActor) -> SessionInfo:
        return SessionInfo(
            actor=actor,
            expires_at=self.session.expires_at,
            idle_expires_at=self.session.idle_expires_at,
            absolute_expires_at=self.session.absolute_expires_at,
        )

    async def logout(self, actor: AuthenticatedActor) -> None:
        del actor
        self.logout_calls += 1


@pytest.mark.asyncio
async def test_bootstrap_sets_http_only_strict_cookie_and_returns_csrf_once() -> None:
    settings = Settings(web_origin="http://127.0.0.1:3000")
    app = create_app(settings, auth_service=FakeAuthService())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url=settings.web_origin
    ) as client:
        response = await client.post(
            "/api/auth/bootstrap",
            headers={"Origin": settings.web_origin, "Host": "127.0.0.1:3000"},
            json={"token": "bootstrap-raw"},
        )

    assert response.status_code == 200
    assert response.json()["csrf_token"] == "csrf-raw"
    assert "session_token" not in response.text
    cookie = response.headers["set-cookie"].lower()
    assert "forge_session=session-raw" in cookie
    assert "httponly" in cookie
    assert "samesite=strict" in cookie
    assert "secure" not in cookie


@pytest.mark.asyncio
async def test_bootstrap_rejects_wrong_origin_and_host() -> None:
    settings = Settings(web_origin="http://127.0.0.1:3000")
    app = create_app(settings, auth_service=FakeAuthService())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url=settings.web_origin
    ) as client:
        wrong_origin = await client.post(
            "/api/auth/bootstrap",
            headers={"Origin": "http://localhost:3000", "Host": "127.0.0.1:3000"},
            json={"token": "bootstrap-raw"},
        )
        wrong_host = await client.post(
            "/api/auth/bootstrap",
            headers={"Origin": settings.web_origin, "Host": "127.0.0.1:3001"},
            json={"token": "bootstrap-raw"},
        )

    assert wrong_origin.status_code == 403
    assert wrong_host.status_code == 403


@pytest.mark.asyncio
async def test_session_requires_cookie_and_maps_expired_session_to_401() -> None:
    settings = Settings(web_origin="http://127.0.0.1:3000")
    auth = FakeAuthService()
    app = create_app(settings, auth_service=auth)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url=settings.web_origin
    ) as client:
        missing = await client.get("/api/auth/session", headers={"Host": "127.0.0.1:3000"})
        client.cookies.set("forge_session", auth.session.session_token)
        auth.require_error = AuthenticationError("invalid or expired session")
        expired = await client.get("/api/auth/session", headers={"Host": "127.0.0.1:3000"})

    assert missing.status_code == 401
    assert expired.status_code == 401
    assert auth.require_calls == 1


@pytest.mark.asyncio
async def test_logout_rejects_wrong_origin_host_and_csrf() -> None:
    settings = Settings(web_origin="http://127.0.0.1:3000")
    auth = FakeAuthService()
    app = create_app(settings, auth_service=auth)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url=settings.web_origin
    ) as client:
        client.cookies.set("forge_session", auth.session.session_token)
        base = {"Host": "127.0.0.1:3000", "Origin": settings.web_origin}
        wrong_origin = await client.post(
            "/api/auth/logout", headers={**base, "Origin": "http://localhost:3000"}
        )
        wrong_host = await client.post(
            "/api/auth/logout", headers={**base, "Host": "127.0.0.1:3001"}
        )
        missing_csrf = await client.post("/api/auth/logout", headers=base)
        wrong_csrf = await client.post(
            "/api/auth/logout", headers={**base, "X-CSRF-Token": "wrong-csrf"}
        )

    assert wrong_origin.status_code == 403
    assert wrong_host.status_code == 403
    assert missing_csrf.status_code == 403
    assert wrong_csrf.status_code == 403
    assert auth.logout_calls == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_auth_routes_return_safe_session_and_enforce_csrf(session_factory) -> None:
    settings = Settings(web_origin="http://127.0.0.1:3000")
    app = create_app(settings, session_factory=session_factory)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url=settings.web_origin
    ) as client:
        bootstrap_service = app.state.auth_service
        bootstrap = await bootstrap_service.issue_bootstrap()
        exchange = await client.post(
            "/api/auth/bootstrap",
            headers={"Origin": settings.web_origin, "Host": "127.0.0.1:3000"},
            json={"token": bootstrap},
        )
        csrf = exchange.json()["csrf_token"]
        session_response = await client.get(
            "/api/auth/session",
            headers={"Host": "127.0.0.1:3000"},
        )
        missing_csrf = await client.post(
            "/api/auth/logout",
            headers={"Origin": settings.web_origin, "Host": "127.0.0.1:3000"},
        )
        logout = await client.post(
            "/api/auth/logout",
            headers={
                "Origin": settings.web_origin,
                "Host": "127.0.0.1:3000",
                "X-CSRF-Token": csrf,
            },
        )

    assert exchange.status_code == 200
    assert session_response.status_code == 200
    assert set(session_response.json()) == {
        "actor_id",
        "actor_class",
        "expires_at",
        "idle_expires_at",
        "absolute_expires_at",
    }
    assert "token_hash" not in session_response.text
    assert missing_csrf.status_code == 403
    assert logout.status_code == 204
    clear_cookie = logout.headers["set-cookie"].lower()
    assert "forge_session=" in clear_cookie
    assert "max-age=0" in clear_cookie
    assert "httponly" in clear_cookie
    assert "samesite=strict" in clear_cookie
    assert "secure" not in clear_cookie
