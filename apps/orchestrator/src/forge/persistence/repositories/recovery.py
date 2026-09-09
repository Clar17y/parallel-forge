"""PostgreSQL-fenced singleton startup recovery ownership."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge.domain.lease import validate_lease_seconds
from forge.persistence.models.recovery import RECOVERY_BARRIER_ID, RecoveryBarrier


class RecoveryBarrierLost(RuntimeError):
    """Recovery cannot prove current singleton ownership."""


@dataclass(frozen=True, slots=True)
class RecoveryLease:
    owner_id: UUID
    generation: int


class PostgresRecoveryBarrier:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    async def acquire(self, *, owner_id: UUID, lease_seconds: float) -> RecoveryLease | None:
        validate_lease_seconds(lease_seconds)
        async with self._factory() as session, session.begin():
            row = await session.scalar(
                select(RecoveryBarrier)
                .where(RecoveryBarrier.id == RECOVERY_BARRIER_ID)
                .with_for_update()
            )
            if row is None:
                raise RecoveryBarrierLost("recovery singleton is missing")
            now = await session.scalar(select(func.clock_timestamp()))
            if not isinstance(now, datetime):
                raise RecoveryBarrierLost("database recovery clock is unavailable")
            if row.owner_id is not None and row.expires_at is not None and row.expires_at > now:
                return None
            row.required, row.owner_id = True, owner_id
            row.generation += 1
            row.expires_at = now + timedelta(seconds=lease_seconds)
            return RecoveryLease(owner_id, row.generation)

    async def renew(self, lease: RecoveryLease, *, lease_seconds: float) -> RecoveryLease:
        validate_lease_seconds(lease_seconds)
        await self._change(
            lease, {"expires_at": func.clock_timestamp() + timedelta(seconds=lease_seconds)}
        )
        return lease

    async def finish(self, lease: RecoveryLease) -> None:
        await self._change(lease, {"required": False, "owner_id": None, "expires_at": None})

    async def abandon(self, lease: RecoveryLease) -> None:
        async with self._factory() as session, session.begin():
            await session.execute(
                update(RecoveryBarrier)
                .where(
                    RecoveryBarrier.id == RECOVERY_BARRIER_ID,
                    RecoveryBarrier.owner_id == lease.owner_id,
                    RecoveryBarrier.generation == lease.generation,
                )
                .values(required=True, owner_id=None, expires_at=None)
            )

    async def _change(self, lease: RecoveryLease, values: dict[str, object]) -> None:
        async with self._factory() as session, session.begin():
            changed = await session.scalar(
                update(RecoveryBarrier)
                .where(
                    RecoveryBarrier.id == RECOVERY_BARRIER_ID,
                    RecoveryBarrier.required.is_(True),
                    RecoveryBarrier.owner_id == lease.owner_id,
                    RecoveryBarrier.generation == lease.generation,
                    RecoveryBarrier.expires_at > func.clock_timestamp(),
                )
                .values(**values)
                .returning(RecoveryBarrier.id)
            )
            if changed is None:
                raise RecoveryBarrierLost("recovery lease expired or changed")
