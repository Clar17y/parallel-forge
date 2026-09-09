"""Short, read-only PostgreSQL transactions for event-stream pages."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge.domain.event import RunEvent
from forge.persistence.models import Run
from forge.persistence.models import RunEvent as EventRecord
from forge.persistence.repositories.events import _event_from_record


class EventQuery:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = session_factory

    async def exists(self, run_id: UUID) -> bool:
        async with self._factory() as session:
            return await session.scalar(select(Run.id).where(Run.id == run_id)) is not None

    async def page(self, run_id: UUID, after: int) -> list[RunEvent]:
        if type(after) is not int or not 0 <= after <= 2**63 - 1:
            raise ValueError("invalid event cursor")
        async with self._factory() as session:
            records = await session.scalars(
                select(EventRecord)
                .where(EventRecord.run_id == run_id, EventRecord.sequence > after)
                .order_by(EventRecord.sequence)
                .limit(128)
            )
            return [_event_from_record(record) for record in records]
