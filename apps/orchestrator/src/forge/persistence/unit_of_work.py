"""Explicit PostgreSQL unit-of-work boundary."""

from __future__ import annotations

from typing import Self

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge.application.services.state_engine import StateEngine
from forge.persistence.repositories.events import PostgresEventRepository
from forge.persistence.repositories.runs import PostgresRunRepository


class PostgresUnitOfWork:
    """Own exactly one async session and require an explicit successful commit."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        state_engine: StateEngine | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._state_engine = state_engine or StateEngine()
        self._session: AsyncSession | None = None
        self._entered = False
        self._committed = False
        self.runs: PostgresRunRepository
        self.events: PostgresEventRepository

    @property
    def session(self) -> AsyncSession:
        """Expose the one session for diagnostics and narrowly scoped adapters."""

        if self._session is None or not self._entered:
            raise RuntimeError("unit of work is not active")
        return self._session

    async def __aenter__(self) -> Self:
        if self._entered:
            raise RuntimeError("unit of work cannot be entered twice concurrently")
        self._session = self._session_factory()
        self.events = PostgresEventRepository(self._session)
        self.runs = PostgresRunRepository(
            self._session,
            state_engine=self._state_engine,
            events=self.events,
        )
        self._entered = True
        self._committed = False
        return self

    async def __aexit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> None:
        del exc, traceback
        session = self._session
        try:
            if session is not None and (exc_type is not None or not self._committed):
                await session.rollback()
        finally:
            if session is not None:
                await session.close()
            self._session = None
            self._entered = False
            self._committed = False

    async def commit(self) -> None:
        """Commit the current transaction and mark this context successful."""

        session = self.session
        try:
            await session.commit()
        except BaseException:
            await session.rollback()
            raise
        self._committed = True

    async def rollback(self) -> None:
        """Explicitly roll back all work in this context."""

        await self.session.rollback()
        self._committed = False


__all__ = ["PostgresUnitOfWork"]
