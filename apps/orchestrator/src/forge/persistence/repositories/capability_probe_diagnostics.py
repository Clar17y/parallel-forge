"""PostgreSQL-backed current capability-probe diagnostics."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge.domain.capability_diagnostics import CapabilityProbeDiagnostic
from forge.domain.capability_evidence import CapabilityEvidenceIdentity
from forge.domain.subscription_readiness import ReadinessReason
from forge.persistence.models.capability_probe_diagnostics import (
    CapabilityProbeDiagnosticRecord,
)

_CLOCK_SKEW = timedelta(minutes=5)
_MAX_VALIDITY = timedelta(hours=24)


class PostgresCapabilityProbeDiagnosticStore:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not callable(session_factory):
            raise TypeError("capability diagnostic session factory is required")
        self._factory, self._clock = session_factory, clock

    async def report(
        self,
        identity: CapabilityEvidenceIdentity,
        reason: ReadinessReason,
        *,
        observed_at: datetime,
        expires_at: datetime,
    ) -> CapabilityProbeDiagnostic:
        if not isinstance(identity, CapabilityEvidenceIdentity):
            raise TypeError("capability diagnostic identity is required")
        draft = CapabilityProbeDiagnostic(
            identity_digest=identity.digest,
            reason=reason,
            revision=1,
            observed_at=observed_at,
            expires_at=expires_at,
        )
        if draft.expires_at - draft.observed_at > _MAX_VALIDITY:
            raise ValueError("capability diagnostic time is invalid")
        async with self._factory() as session, session.begin():
            now = await self._now(session)
            if (
                draft.observed_at < now - _MAX_VALIDITY
                or draft.observed_at > now + _CLOCK_SKEW
                or draft.expires_at <= now
            ):
                raise ValueError("capability diagnostic time is invalid")
            statement = insert(CapabilityProbeDiagnosticRecord).values(
                identity_digest=draft.identity_digest,
                reason=draft.reason.value,
                revision=1,
                observed_at=draft.observed_at,
                expires_at=draft.expires_at,
                updated_at=now,
            )
            record = await session.scalar(
                statement.on_conflict_do_update(
                    index_elements=[CapabilityProbeDiagnosticRecord.identity_digest],
                    set_={
                        "reason": statement.excluded.reason,
                        "revision": CapabilityProbeDiagnosticRecord.revision + 1,
                        "observed_at": statement.excluded.observed_at,
                        "expires_at": statement.excluded.expires_at,
                        "updated_at": now,
                    },
                    where=(
                        statement.excluded.observed_at
                        >= CapabilityProbeDiagnosticRecord.observed_at
                    ),
                ).returning(CapabilityProbeDiagnosticRecord)
            )
            if record is None:
                record = await session.scalar(
                    select(CapabilityProbeDiagnosticRecord).where(
                        CapabilityProbeDiagnosticRecord.identity_digest == draft.identity_digest
                    )
                )
            if record is None:  # pragma: no cover - guarded by the upsert transaction
                raise RuntimeError("capability diagnostic update was lost")
            return _domain(record)

    async def resolve(
        self, identity: CapabilityEvidenceIdentity
    ) -> CapabilityProbeDiagnostic | None:
        if not isinstance(identity, CapabilityEvidenceIdentity):
            raise TypeError("capability diagnostic identity is required")
        async with self._factory() as session:
            now = await self._now(session)
            record = await session.scalar(
                select(CapabilityProbeDiagnosticRecord).where(
                    CapabilityProbeDiagnosticRecord.identity_digest == identity.digest
                )
            )
            if record is None or record.observed_at > now + _CLOCK_SKEW or record.expires_at <= now:
                return None
            return _domain(record)

    async def _now(self, session: AsyncSession) -> datetime:
        value = (
            self._clock()
            if self._clock is not None
            else await session.scalar(select(func.clock_timestamp()))
        )
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("capability diagnostic clock is invalid")
        return value.astimezone(UTC)


def _domain(record: CapabilityProbeDiagnosticRecord) -> CapabilityProbeDiagnostic:
    return CapabilityProbeDiagnostic(
        identity_digest=record.identity_digest,
        reason=ReadinessReason(record.reason),
        revision=record.revision,
        observed_at=record.observed_at,
        expires_at=record.expires_at,
    )


__all__ = ["PostgresCapabilityProbeDiagnosticStore"]
