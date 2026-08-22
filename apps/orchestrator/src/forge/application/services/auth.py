"""Local operator authentication and server-side session lifecycle."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol, Self
from uuid import UUID, uuid4

from forge.application.ports.clock import Clock, SystemClock

BOOTSTRAP_LIFETIME = timedelta(minutes=5)
SESSION_IDLE_LIFETIME = timedelta(minutes=30)
SESSION_ABSOLUTE_LIFETIME = timedelta(hours=12)


class AuthenticationError(RuntimeError):
    """Authentication failed without exposing credential or persistence details."""


class CsrfError(AuthenticationError):
    """The authenticated session did not present its matching CSRF token."""


@dataclass(frozen=True, slots=True)
class AuthenticatedActor:
    """An operator actor reconstructed from a current server-side session."""

    actor_id: UUID
    actor_class: Literal["operator"]
    session_id: UUID

    def __post_init__(self) -> None:
        if self.actor_class != "operator":
            raise ValueError("authenticated actors must be operators")


@dataclass(frozen=True, slots=True)
class AuthenticatedSession:
    """The one-time exchange result; raw credentials are intentionally non-repr fields."""

    session_id: UUID
    actor_id: UUID
    actor_class: Literal["operator"]
    expires_at: datetime
    idle_expires_at: datetime
    absolute_expires_at: datetime
    session_token: str = field(repr=False)
    csrf_token: str = field(repr=False)

    @property
    def actor(self) -> AuthenticatedActor:
        """Return the server-issued operator identity for this session."""

        return AuthenticatedActor(
            actor_id=self.actor_id,
            actor_class=self.actor_class,
            session_id=self.session_id,
        )


@dataclass(frozen=True, slots=True)
class SessionInfo:
    """Safe session metadata suitable for an API response."""

    actor: AuthenticatedActor
    expires_at: datetime
    idle_expires_at: datetime
    absolute_expires_at: datetime


class AuthRepository(Protocol):
    """The session-bound persistence operations used by AuthService."""

    async def create_bootstrap(self, *, token_hash: str, expires_at: datetime) -> object: ...

    async def consume_bootstrap(self, *, token_hash: str, now: datetime) -> object | None: ...

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
    ) -> object: ...

    async def get_valid_session(
        self,
        *,
        token_hash: str,
        now: datetime,
        for_update: bool = True,
    ) -> object | None: ...

    async def set_idle_expiry(
        self, *, session_id: UUID, idle_expires_at: datetime, maximum: datetime
    ) -> object | None: ...

    async def get_session_by_id(
        self,
        *,
        session_id: UUID,
        now: datetime,
        for_update: bool = False,
        actor_id: UUID | None = None,
    ) -> object | None: ...

    async def lock_run(self, run_id: UUID) -> object | None: ...

    async def create_challenge(self, **values: object) -> object: ...

    async def get_challenge(self, *, token_hash: str, for_update: bool = True) -> object | None: ...

    async def consume_challenge(self, *, challenge_id: UUID, at: datetime) -> None: ...

    async def create_approval(self, **values: object) -> object: ...

    async def get_approval(
        self, *, approval_id: UUID, for_update: bool = True
    ) -> object | None: ...

    async def revoke_session(self, *, session_id: UUID, at: datetime) -> None: ...

    async def revoke_all(self, *, at: datetime) -> None: ...


class AuthUnitOfWork(Protocol):
    """Narrow transaction protocol, keeping unrelated fakes auth-free."""

    auth: AuthRepository

    async def __aenter__(self) -> Self: ...

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None: ...

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...


class AuthService:
    """Issue, exchange, rotate, and validate local operator credentials."""

    def __init__(
        self,
        unit_of_work_factory: Callable[[], AuthUnitOfWork],
        *,
        clock: Clock | None = None,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._clock = clock or SystemClock()

    async def issue_bootstrap(self) -> str:
        """Persist one five-minute bootstrap hash and return its raw token once."""

        now = _aware_now(self._clock)
        token = _new_token()
        async with self._unit_of_work_factory() as work:
            await work.auth.create_bootstrap(
                token_hash=hash_token(token),
                expires_at=now + BOOTSTRAP_LIFETIME,
            )
            await work.commit()
        return token

    async def exchange_bootstrap(self, token: str) -> AuthenticatedSession:
        """Atomically consume a bootstrap and create one operator session."""

        if not isinstance(token, str) or not token:
            raise AuthenticationError("invalid or expired bootstrap token")
        now = _aware_now(self._clock)
        session_token = _new_token()
        csrf_token = _new_token()
        actor_id = uuid4()
        session_id = uuid4()
        absolute_expires_at = now + SESSION_ABSOLUTE_LIFETIME
        idle_expires_at = min(now + SESSION_IDLE_LIFETIME, absolute_expires_at)

        async with self._unit_of_work_factory() as work:
            consumed = await work.auth.consume_bootstrap(
                token_hash=hash_token(token),
                now=now,
            )
            if consumed is None:
                raise AuthenticationError("invalid or expired bootstrap token")
            await work.auth.create_session(
                session_id=session_id,
                token_hash=hash_token(session_token),
                csrf_hash=hash_token(csrf_token),
                actor_id=actor_id,
                expires_at=absolute_expires_at,
                idle_expires_at=idle_expires_at,
                absolute_expires_at=absolute_expires_at,
            )
            await work.commit()

        return AuthenticatedSession(
            session_id=session_id,
            actor_id=actor_id,
            actor_class="operator",
            expires_at=absolute_expires_at,
            idle_expires_at=idle_expires_at,
            absolute_expires_at=absolute_expires_at,
            session_token=session_token,
            csrf_token=csrf_token,
        )

    async def rotate(self) -> str:
        """Revoke every prior credential and atomically issue one bootstrap token."""

        now = _aware_now(self._clock)
        token = _new_token()
        async with self._unit_of_work_factory() as work:
            await work.auth.revoke_all(at=now)
            await work.auth.create_bootstrap(
                token_hash=hash_token(token),
                expires_at=now + BOOTSTRAP_LIFETIME,
            )
            await work.commit()
        return token

    async def require_session(
        self,
        token: str,
        *,
        csrf_token: str | None = None,
        require_csrf: bool = False,
    ) -> AuthenticatedActor:
        """Validate a cookie token and return only the server-issued operator actor."""

        if not isinstance(token, str) or not token:
            raise AuthenticationError("invalid or expired session")
        if require_csrf and (not isinstance(csrf_token, str) or not csrf_token):
            raise CsrfError("invalid csrf token")
        now = _aware_now(self._clock)
        async with self._unit_of_work_factory() as work:
            row = await work.auth.get_valid_session(
                token_hash=hash_token(token),
                now=now,
                for_update=True,
            )
            if row is None:
                raise AuthenticationError("invalid or expired session")
            actor_id = _uuid_value(row, "actor_id")
            session_id = _uuid_value(row, "id")
            if actor_id is None or session_id is None:
                raise AuthenticationError("invalid or expired session")
            if require_csrf:
                stored_csrf = _string_value(row, "csrf_hash")
                if stored_csrf is None or not hmac.compare_digest(
                    stored_csrf, hash_token(csrf_token or "")
                ):
                    raise CsrfError("invalid csrf token")
            absolute = _datetime_value(row, "absolute_expires_at")
            await work.auth.set_idle_expiry(
                session_id=session_id,
                idle_expires_at=now + SESSION_IDLE_LIFETIME,
                maximum=absolute,
            )
            await work.commit()
        return AuthenticatedActor(actor_id=actor_id, actor_class="operator", session_id=session_id)

    async def session_info(self, actor: AuthenticatedActor) -> SessionInfo:
        """Load safe current expiry metadata for an already authenticated actor."""

        now = _aware_now(self._clock)
        async with self._unit_of_work_factory() as work:
            row = await work.auth.get_session_by_id(
                session_id=actor.session_id,
                now=now,
                for_update=False,
            )
            if row is None:
                raise AuthenticationError("invalid or expired session")
            row_actor = _uuid_value(row, "actor_id")
            if row_actor != actor.actor_id:
                raise AuthenticationError("invalid or expired session")
            expires_at = _datetime_value(row, "expires_at")
            idle_expires_at = _datetime_value(row, "idle_expires_at")
            absolute_expires_at = _datetime_value(row, "absolute_expires_at")
            await work.rollback()
        return SessionInfo(
            actor=actor,
            expires_at=expires_at,
            idle_expires_at=idle_expires_at,
            absolute_expires_at=absolute_expires_at,
        )

    async def logout(self, actor: AuthenticatedActor) -> None:
        """Revoke one session without exposing any credential material."""

        now = _aware_now(self._clock)
        async with self._unit_of_work_factory() as work:
            await work.auth.revoke_session(session_id=actor.session_id, at=now)
            await work.commit()


def hash_token(token: str) -> str:
    """Return the canonical SHA-256 representation used for every raw token."""

    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _new_token() -> str:
    return secrets.token_urlsafe(32)


def _aware_now(clock: Clock) -> datetime:
    value = clock.now()
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("security clock must return an aware datetime")
    return value.astimezone(UTC)


def _uuid_value(row: object, name: str) -> UUID | None:
    value = _row_value(row, name)
    return value if isinstance(value, UUID) else None


def _string_value(row: object, name: str) -> str | None:
    value = _row_value(row, name)
    return value if isinstance(value, str) else None


def _datetime_value(row: object, name: str) -> datetime:
    value = _row_value(row, name)
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise AuthenticationError("invalid or expired session")
    return value


def _row_value(row: object, name: str) -> object:
    value = getattr(row, name, None)
    if value is not None:
        return value
    if isinstance(row, Mapping):
        return row.get(name)
    return None


__all__ = [
    "BOOTSTRAP_LIFETIME",
    "SESSION_ABSOLUTE_LIFETIME",
    "SESSION_IDLE_LIFETIME",
    "AuthRepository",
    "AuthService",
    "AuthUnitOfWork",
    "AuthenticatedActor",
    "AuthenticatedSession",
    "AuthenticationError",
    "CsrfError",
    "SessionInfo",
    "hash_token",
]
