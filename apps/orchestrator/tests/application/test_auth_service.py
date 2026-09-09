"""Unit coverage for the local operator credential lifecycle."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Self
from uuid import UUID, uuid4

import pytest
from forge.application.ports.clock import Clock
from forge.application.services.auth import AuthenticationError, AuthService, CsrfError


@dataclass
class FakeClock:
    value: datetime

    def now(self) -> datetime:
        return self.value

    def advance(self, **kwargs: int) -> None:
        self.value += timedelta(**kwargs)


class FakeAuthUnitOfWork:
    """Application-boundary fake; production has no in-memory fallback."""

    def __init__(self) -> None:
        self.auth = FakeAuthRepository()
        self.committed = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.committed = False


class FakeAuthRepository:
    def __init__(self) -> None:
        self.bootstraps: dict[str, dict[str, object]] = {}
        self.sessions: dict[str, dict[str, object]] = {}

    async def create_bootstrap(self, *, token_hash: str, expires_at: datetime) -> None:
        self.bootstraps[token_hash] = {"expires_at": expires_at, "used_at": None}

    async def consume_bootstrap(self, *, token_hash: str, now: datetime) -> dict[str, object]:
        row = self.bootstraps.get(token_hash)
        if row is None or row["used_at"] is not None or row["expires_at"] <= now:
            raise AuthenticationError("invalid or expired bootstrap token")
        row["used_at"] = now
        return row

    async def create_session(
        self,
        *,
        session_id: UUID,
        token_hash: str,
        csrf_hash: str,
        actor_id: UUID,
        expires_at: datetime,
        idle_expires_at: datetime,
        absolute_expires_at: datetime,
    ) -> None:
        self.sessions[token_hash] = {
            "id": session_id,
            "actor_id": actor_id,
            "expires_at": expires_at,
            "idle_expires_at": idle_expires_at,
            "absolute_expires_at": absolute_expires_at,
            "csrf_hash": csrf_hash,
            "revoked_at": None,
        }

    async def get_valid_session(
        self, *, token_hash: str, now: datetime, for_update: bool
    ) -> dict[str, object]:
        del for_update
        row = self.sessions.get(token_hash)
        if (
            row is None
            or row["revoked_at"] is not None
            or row["expires_at"] <= now
            or row["idle_expires_at"] <= now
        ):
            raise AuthenticationError("invalid or expired session")
        return row

    async def set_idle_expiry(
        self, *, session_id: UUID, idle_expires_at: datetime, maximum: datetime
    ) -> dict[str, object] | None:
        for row in self.sessions.values():
            if row["id"] == session_id:
                row["idle_expires_at"] = min(idle_expires_at, maximum)
                return row
        return None

    async def get_session_by_id(
        self, *, session_id: UUID, now: datetime, for_update: bool
    ) -> dict[str, object] | None:
        del now, for_update
        for row in self.sessions.values():
            if row["id"] == session_id and row["revoked_at"] is None:
                return row
        return None

    async def revoke_session(self, *, session_id: UUID, at: datetime) -> None:
        for row in self.sessions.values():
            if row["id"] == session_id:
                row["revoked_at"] = at

    async def revoke_all(self, *, at: datetime) -> None:
        for row in self.bootstraps.values():
            if row["used_at"] is None:
                row["used_at"] = at
        for row in self.sessions.values():
            row["revoked_at"] = at


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime(2026, 1, 1, tzinfo=UTC))


@pytest.mark.asyncio
async def test_bootstrap_token_is_single_use(clock: Clock) -> None:
    uow = FakeAuthUnitOfWork()
    service = AuthService(lambda: uow, clock=clock)

    token = await service.issue_bootstrap()
    session = await service.exchange_bootstrap(token)

    assert session.actor_class == "operator"
    assert session.expires_at == datetime(2026, 1, 1, 12, tzinfo=UTC)

    with pytest.raises(AuthenticationError, match="invalid or expired bootstrap token"):
        await service.exchange_bootstrap(token)


@pytest.mark.asyncio
async def test_bootstrap_expires_at_exact_five_minute_boundary(clock: Clock) -> None:
    uow = FakeAuthUnitOfWork()
    service = AuthService(lambda: uow, clock=clock)

    token = await service.issue_bootstrap()
    clock.advance(minutes=5)

    with pytest.raises(AuthenticationError, match="invalid or expired bootstrap token"):
        await service.exchange_bootstrap(token)


@pytest.mark.asyncio
async def test_idle_expiry_fails_at_exact_thirty_minute_boundary(clock: Clock) -> None:
    uow = FakeAuthUnitOfWork()
    service = AuthService(lambda: uow, clock=clock)
    session = await service.exchange_bootstrap(await service.issue_bootstrap())
    clock.advance(minutes=30)

    with pytest.raises(AuthenticationError, match="invalid or expired session"):
        await service.require_session(session.session_token)


@pytest.mark.asyncio
async def test_activity_slides_idle_and_caps_at_unchanged_absolute_expiry(
    clock: Clock,
) -> None:
    uow = FakeAuthUnitOfWork()
    service = AuthService(lambda: uow, clock=clock)
    session = await service.exchange_bootstrap(await service.issue_bootstrap())

    clock.advance(minutes=29)
    actor = await service.require_session(session.session_token)
    first_info = await service.session_info(actor)
    assert first_info.idle_expires_at == datetime(2026, 1, 1, 0, 59, tzinfo=UTC)
    assert first_info.absolute_expires_at == session.absolute_expires_at

    # Keep the sliding window alive while moving close to the absolute cap.
    for _ in range(27):
        clock.advance(minutes=25)
        actor = await service.require_session(session.session_token)
    clock.advance(minutes=1)
    actor = await service.require_session(session.session_token)
    capped_info = await service.session_info(actor)
    assert capped_info.idle_expires_at == session.absolute_expires_at
    assert capped_info.absolute_expires_at == session.absolute_expires_at

    clock.advance(minutes=15)
    with pytest.raises(AuthenticationError, match="invalid or expired session"):
        await service.require_session(session.session_token)


@pytest.mark.asyncio
async def test_absolute_expiry_fails_at_exact_twelve_hour_boundary(clock: Clock) -> None:
    uow = FakeAuthUnitOfWork()
    service = AuthService(lambda: uow, clock=clock)
    session = await service.exchange_bootstrap(await service.issue_bootstrap())
    clock.advance(hours=12)

    with pytest.raises(AuthenticationError, match="invalid or expired session"):
        await service.require_session(session.session_token)


@pytest.mark.asyncio
async def test_rotation_revokes_sessions_and_consumes_old_bootstraps(clock: Clock) -> None:
    uow = FakeAuthUnitOfWork()
    service = AuthService(lambda: uow, clock=clock)
    first_session = await service.exchange_bootstrap(await service.issue_bootstrap())
    pending_bootstrap = await service.issue_bootstrap()
    second_session = await service.exchange_bootstrap(pending_bootstrap)
    old_pending_bootstrap = await service.issue_bootstrap()

    fresh_bootstrap = await service.rotate()

    with pytest.raises(AuthenticationError, match="invalid or expired session"):
        await service.require_session(first_session.session_token)
    with pytest.raises(AuthenticationError, match="invalid or expired session"):
        await service.require_session(second_session.session_token)
    with pytest.raises(AuthenticationError, match="invalid or expired bootstrap token"):
        await service.exchange_bootstrap(old_pending_bootstrap)
    # The bootstrap issued after rotation is the only credential accepted.
    fresh_session = await service.exchange_bootstrap(fresh_bootstrap)
    assert fresh_session.actor_class == "operator"


@pytest.mark.asyncio
async def test_failed_csrf_does_not_slide_idle_expiry_or_leak_raw_tokens(
    clock: Clock,
) -> None:
    uow = FakeAuthUnitOfWork()
    service = AuthService(lambda: uow, clock=clock)
    bootstrap = "bootstrap-secret-for-test"
    session = await service.exchange_bootstrap(await service.issue_bootstrap())
    assert bootstrap not in repr(session)
    assert all(bootstrap not in key for key in uow.auth.bootstraps)
    assert all(bootstrap not in key for key in uow.auth.sessions)

    clock.advance(minutes=29)
    with pytest.raises(CsrfError) as raised:
        await service.require_session(
            session.session_token,
            csrf_token="wrong-csrf-secret",
            require_csrf=True,
        )
    assert "wrong-csrf-secret" not in repr(raised.value)
    row = next(iter(uow.auth.sessions.values()))
    assert row["idle_expires_at"] == datetime(2026, 1, 1, 0, 30, tzinfo=UTC)


@pytest.mark.asyncio
async def test_raw_tokens_are_absent_from_persistence_and_auth_errors(clock: Clock) -> None:
    uow = FakeAuthUnitOfWork()
    service = AuthService(lambda: uow, clock=clock)
    bootstrap = await service.issue_bootstrap()
    session = await service.exchange_bootstrap(bootstrap)

    assert bootstrap not in repr(session)
    assert session.session_token not in repr(session)
    assert session.csrf_token not in repr(session)
    persisted = [*uow.auth.bootstraps, *uow.auth.sessions]
    for value in persisted:
        assert bootstrap not in value
        assert session.session_token not in value
        assert session.csrf_token not in value

    invalid = "invalid-bootstrap-secret"
    with pytest.raises(AuthenticationError) as raised:
        await service.exchange_bootstrap(invalid)
    assert invalid not in str(raised.value)
    assert invalid not in repr(raised.value)
    assert raised.value.__cause__ is None


@pytest.mark.asyncio
async def test_session_idle_expiry_slides_but_absolute_expiry_does_not(clock: Clock) -> None:
    uow = FakeAuthUnitOfWork()
    service = AuthService(lambda: uow, clock=clock)
    token = await service.issue_bootstrap()
    session = await service.exchange_bootstrap(token)

    clock.advance(minutes=29)
    actor = await service.require_session(session.session_token)
    assert actor.actor_class == "operator"

    info = await service.session_info(actor)
    assert info.idle_expires_at == datetime(2026, 1, 1, 0, 59, tzinfo=UTC)
    assert info.expires_at == session.expires_at

    clock.advance(hours=11, minutes=59)
    with pytest.raises(AuthenticationError, match="invalid or expired session"):
        await service.require_session(session.session_token)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_auth_repository_persists_only_hashes_and_supports_session_activity(
    session_factory,
) -> None:
    from forge.persistence.models import OperatorSession
    from forge.persistence.unit_of_work import PostgresUnitOfWork
    from sqlalchemy import select

    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    service = AuthService(lambda: PostgresUnitOfWork(session_factory), clock=clock)
    bootstrap = await service.issue_bootstrap()
    session = await service.exchange_bootstrap(bootstrap)

    async with session_factory() as db:
        records = (await db.execute(select(OperatorSession))).scalars().all()
    assert all(bootstrap not in record.token_hash for record in records)
    assert all(session.session_token not in record.token_hash for record in records)
    assert all(session.csrf_token not in (record.csrf_hash or "") for record in records)

    actor = await service.require_session(session.session_token)
    info = await service.session_info(actor)
    assert info.expires_at == session.expires_at
    await service.logout(actor)
    with pytest.raises(AuthenticationError, match="invalid or expired session"):
        await service.require_session(session.session_token)


def test_authenticated_actor_cannot_be_constructed_as_worker() -> None:
    from forge.application.services.auth import AuthenticatedActor

    actor = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())
    assert isinstance(actor.actor_id, UUID)
    assert actor.actor_class == "operator"
