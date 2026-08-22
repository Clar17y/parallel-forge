"""Session-bound PostgreSQL persistence for operator credentials and challenges."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from forge.persistence.models import Approval, ApprovalChallenge, OperatorSession, Run


class AuthPersistenceError(RuntimeError):
    """Persisted authentication data cannot be used safely."""


class PostgresAuthRepository:
    """Use exactly one caller-owned AsyncSession; never begin or commit here."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_bootstrap(self, *, token_hash: str, expires_at: datetime) -> OperatorSession:
        record = OperatorSession(
            id=uuid4(),
            credential_kind="bootstrap",
            token_hash=token_hash,
            expires_at=expires_at,
            actor_id=None,
            csrf_hash=None,
            idle_expires_at=None,
            absolute_expires_at=None,
            used_at=None,
            revoked_at=None,
        )
        self._session.add(record)
        await self._session.flush()
        return record

    async def consume_bootstrap(self, *, token_hash: str, now: datetime) -> OperatorSession | None:
        record = (
            await self._session.execute(
                select(OperatorSession)
                .where(
                    OperatorSession.credential_kind == "bootstrap",
                    OperatorSession.token_hash == token_hash,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if record is None or not _bootstrap_is_valid(record, now):
            return None
        record.used_at = now
        await self._session.flush()
        return record

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
    ) -> OperatorSession:
        if expires_at != absolute_expires_at:
            raise AuthPersistenceError("session expiry must equal absolute expiry")
        record = OperatorSession(
            id=session_id,
            credential_kind="session",
            token_hash=token_hash,
            expires_at=expires_at,
            actor_id=actor_id,
            csrf_hash=csrf_hash,
            idle_expires_at=idle_expires_at,
            absolute_expires_at=absolute_expires_at,
            used_at=None,
            revoked_at=None,
        )
        self._session.add(record)
        await self._session.flush()
        return record

    async def get_valid_session(
        self,
        *,
        token_hash: str,
        now: datetime,
        for_update: bool = True,
    ) -> OperatorSession | None:
        statement = select(OperatorSession).where(
            OperatorSession.credential_kind == "session",
            OperatorSession.token_hash == token_hash,
        )
        if for_update:
            statement = statement.with_for_update()
        record = (await self._session.execute(statement)).scalar_one_or_none()
        if record is None or not _session_is_valid(record, now):
            return None
        return record

    async def advance_idle(
        self, *, session_id: UUID, now: datetime, maximum: datetime
    ) -> OperatorSession | None:
        record = await self._locked_session(session_id)
        if record is None or not _session_is_valid(record, now):
            return None
        if record.idle_expires_at is None or record.absolute_expires_at is None:
            return None
        # The service passes the current idle expiry as a baseline; this
        # method is replaced by set_idle_expiry for the actual sliding target.
        record.idle_expires_at = min(record.idle_expires_at, maximum)
        await self._session.flush()
        return record

    async def set_idle_expiry(
        self, *, session_id: UUID, idle_expires_at: datetime, maximum: datetime
    ) -> OperatorSession | None:
        record = await self._locked_session(session_id)
        if record is None or record.absolute_expires_at is None:
            return None
        record.idle_expires_at = min(idle_expires_at, maximum)
        await self._session.flush()
        return record

    async def get_session_by_id(
        self,
        *,
        session_id: UUID,
        now: datetime,
        for_update: bool = False,
        actor_id: UUID | None = None,
    ) -> OperatorSession | None:
        statement = select(OperatorSession).where(
            OperatorSession.id == session_id,
            OperatorSession.credential_kind == "session",
        )
        if actor_id is not None:
            statement = statement.where(OperatorSession.actor_id == actor_id)
        if for_update:
            statement = statement.with_for_update()
        record = (await self._session.execute(statement)).scalar_one_or_none()
        if record is None or not _session_is_valid(record, now):
            return None
        return record

    async def lock_run(self, run_id: UUID) -> Run | None:
        return (
            await self._session.execute(select(Run).where(Run.id == run_id).with_for_update())
        ).scalar_one_or_none()

    async def create_challenge(
        self,
        *,
        challenge_id: UUID,
        session_id: UUID,
        run_id: UUID,
        gate: str,
        run_version: int,
        policy_version: int,
        evidence_digest: str,
        token_hash: str,
        expires_at: datetime,
    ) -> ApprovalChallenge:
        record = ApprovalChallenge(
            id=challenge_id,
            session_id=session_id,
            run_id=run_id,
            gate=gate,
            run_version=run_version,
            policy_version=policy_version,
            evidence_digest=evidence_digest,
            token_hash=token_hash,
            expires_at=expires_at,
            consumed_at=None,
        )
        self._session.add(record)
        await self._session.flush()
        return record

    async def get_challenge(
        self, *, token_hash: str, for_update: bool = True
    ) -> ApprovalChallenge | None:
        statement = select(ApprovalChallenge).where(
            ApprovalChallenge.token_hash == token_hash,
        )
        if for_update:
            statement = statement.with_for_update()
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def consume_challenge(self, *, challenge_id: UUID, at: datetime) -> None:
        record = (
            await self._session.execute(
                select(ApprovalChallenge)
                .where(ApprovalChallenge.id == challenge_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if record is None:
            return
        record.consumed_at = at
        await self._session.flush()

    async def create_approval(self, **values: object) -> Approval:
        record = Approval(**values)
        self._session.add(record)
        await self._session.flush()
        return record

    async def get_approval(self, *, approval_id: UUID, for_update: bool = True) -> Approval | None:
        statement = select(Approval).where(Approval.id == approval_id)
        if for_update:
            statement = statement.with_for_update()
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def revoke_session(self, *, session_id: UUID, at: datetime) -> None:
        await self._session.execute(
            update(OperatorSession)
            .where(
                OperatorSession.id == session_id,
                OperatorSession.credential_kind == "session",
                OperatorSession.revoked_at.is_(None),
            )
            .values(revoked_at=at)
        )
        await self._session.flush()

    async def revoke_all(self, *, at: datetime) -> None:
        # Bootstrap rows intentionally retain NULL revoked_at due to the
        # existing credential-shape constraint; marking them used revokes them.
        await self._session.execute(
            update(OperatorSession)
            .where(
                OperatorSession.credential_kind == "bootstrap",
                OperatorSession.used_at.is_(None),
            )
            .values(used_at=at)
        )
        await self._session.execute(
            update(OperatorSession)
            .where(
                OperatorSession.credential_kind == "session",
                OperatorSession.revoked_at.is_(None),
            )
            .values(revoked_at=at)
        )
        await self._session.flush()

    async def _locked_session(self, session_id: UUID) -> OperatorSession | None:
        return (
            await self._session.execute(
                select(OperatorSession)
                .where(
                    OperatorSession.id == session_id,
                    OperatorSession.credential_kind == "session",
                )
                .with_for_update()
            )
        ).scalar_one_or_none()


def _bootstrap_is_valid(record: OperatorSession, now: datetime) -> bool:
    return (
        record.credential_kind == "bootstrap"
        and record.used_at is None
        and record.revoked_at is None
        and record.expires_at > now
    )


def _session_is_valid(record: OperatorSession, now: datetime) -> bool:
    return (
        record.credential_kind == "session"
        and record.actor_id is not None
        and record.csrf_hash is not None
        and record.idle_expires_at is not None
        and record.absolute_expires_at is not None
        and record.revoked_at is None
        and record.expires_at > now
        and record.idle_expires_at > now
        and record.expires_at == record.absolute_expires_at
    )


__all__ = ["AuthPersistenceError", "PostgresAuthRepository"]
