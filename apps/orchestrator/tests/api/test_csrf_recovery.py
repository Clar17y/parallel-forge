"""CSRF recovery permits reloads without persistent browser token storage."""

from datetime import timedelta

import pytest
from forge.api.app import create_app
from forge.api.security import SESSION_COOKIE
from forge.application.services.auth import AuthenticationError, hash_token
from forge.persistence.models import OperatorSession
from forge.settings import Settings
from httpx import ASGITransport, AsyncClient

pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)
pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_csrf_recovery_is_stable_session_bound_uncached_and_usable(session_factory, tmp_path):
    settings = Settings(data_root=tmp_path)
    app = create_app(settings, session_factory=session_factory)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url=settings.web_origin
    ) as client:
        assert (await client.get("/api/auth/csrf")).status_code == 401
        bootstrap = await app.state.auth_service.issue_bootstrap()
        exchange = await client.post(
            "/api/auth/bootstrap",
            json={"token": bootstrap},
            headers={"Origin": settings.web_origin},
        )
        assert exchange.status_code == 200
        expected = exchange.json()["csrf_token"]
        cookie = client.cookies.get(SESSION_COOKIE)
        for _ in range(2):
            recovered = await client.get("/api/auth/csrf")
            assert recovered.status_code == 200
            assert recovered.json() == {"csrf_token": expected}
            assert recovered.headers["cache-control"] == "no-store"
            assert recovered.headers["cross-origin-resource-policy"] == "same-origin"
            assert cookie not in recovered.text
        assert (
            await client.get("/api/auth/csrf", headers={"Origin": "http://127.0.0.1:4000"})
        ).status_code == 403
        assert (
            await client.get("/api/auth/csrf", headers={"Host": "evil.example"})
        ).status_code == 403
        logout = await client.post(
            "/api/auth/logout", headers={"Origin": settings.web_origin, "X-CSRF-Token": expected}
        )
        assert logout.status_code == 204
        assert (await client.get("/api/auth/csrf")).status_code == 401


async def test_csrf_recovery_revalidates_actor_and_does_not_rewrite_legacy_hash(
    session_factory, tmp_path
):
    app = create_app(Settings(data_root=tmp_path), session_factory=session_factory)
    auth = app.state.auth_service
    first = await auth.exchange_bootstrap(await auth.issue_bootstrap())
    second = await auth.exchange_bootstrap(await auth.issue_bootstrap())
    assert first.csrf_token != second.csrf_token
    with pytest.raises(AuthenticationError):
        await auth.recover_csrf(first.actor, second.session_token)
    async with session_factory() as session, session.begin():
        row = await session.get(OperatorSession, first.session_id)
        row.csrf_hash = hash_token("legacy-csrf")
    with pytest.raises(AuthenticationError):
        await auth.recover_csrf(first.actor, first.session_token)
    async with session_factory() as session:
        row = await session.get(OperatorSession, first.session_id)
        assert row.csrf_hash == hash_token("legacy-csrf")
    await auth.logout(second.actor)
    with pytest.raises(AuthenticationError):
        await auth.recover_csrf(second.actor, second.session_token)


async def test_csrf_recovery_does_not_extend_expiry(session_factory, tmp_path):
    from datetime import UTC, datetime

    class Clock:
        value = datetime.now(UTC)

        def now(self):
            return self.value

    clock = Clock()
    app = create_app(Settings(data_root=tmp_path), session_factory=session_factory, clock=clock)
    auth = app.state.auth_service
    issued = await auth.exchange_bootstrap(await auth.issue_bootstrap())
    clock.value += timedelta(minutes=10)
    assert await auth.recover_csrf(issued.actor, issued.session_token) == issued.csrf_token
    async with session_factory() as session:
        row = await session.get(OperatorSession, issued.session_id)
        assert row.idle_expires_at == issued.idle_expires_at
        assert row.csrf_hash == hash_token(issued.csrf_token)
    clock.value += timedelta(minutes=21)
    with pytest.raises(AuthenticationError):
        await auth.recover_csrf(issued.actor, issued.session_token)
