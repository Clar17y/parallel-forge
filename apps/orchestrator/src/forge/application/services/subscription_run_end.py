"""Release attempts of ended runs once their client processes are proved stopped."""

from __future__ import annotations

import logging
from collections.abc import Callable
from uuid import UUID

from forge.persistence.repositories.subscription_run_end import PostgresRunEndSettlement
from forge.persistence.unit_of_work import PostgresUnitOfWork

logger = logging.getLogger(__name__)


class RunEndAttemptSettlement:
    def __init__(self, work_factory: Callable[[], PostgresUnitOfWork]) -> None:
        self._work_factory = work_factory

    async def reconcile_all(self) -> int:
        settled, cursor = 0, None
        while True:
            async with self._work_factory() as work:
                runs = await PostgresRunEndSettlement(work.session).candidate_run_ids(cursor, 100)
                await work.rollback()
            if not runs:
                return settled
            for run_id in runs:
                settled += await self._settle(run_id)
            cursor = runs[-1]

    async def _settle(self, run_id: UUID) -> int:
        async with self._work_factory() as work:
            count = await PostgresRunEndSettlement(work.session).settle_run(run_id)
            await work.commit()
        if count:
            logger.info("Settled %d stopped attempt(s) of ended run %s", count, run_id)
        return count


__all__ = ["RunEndAttemptSettlement"]
