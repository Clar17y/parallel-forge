"""HTTP boundary tests for bootstrap/session security."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from forge.api.app import create_app
from forge.application.services.auth import AuthenticatedActor, AuthenticatedSession, SessionInfo
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
        assert token == self.session.session_token
        return self.session.actor

    async def session_info(self, actor: AuthenticatedActor) -> SessionInfo:
        return SessionInfo(
            actor=actor,
            expires_at=self.session.expires_at,
            idle_expires_at=self.session.idle_expires_at,
            absolute_expires_at=self.session.absolute_expires_at,
        )

    async def logout(self, actor: AuthenticatedActor) -> None:
        pass


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
