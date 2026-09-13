"""Publish diagnostic registration independently of provider execution slots."""

import asyncio
import logging
from collections.abc import Iterable
from uuid import uuid4

from forge.domain.subscription import RouteSpec
from forge.persistence.repositories.subscription_runtime_status import (
    RUNTIME_REPORT_SECONDS,
    SubscriptionRuntimeStatusStore,
)

logger = logging.getLogger(__name__)


class SubscriptionRuntimeReporter:
    def __init__(self, store: SubscriptionRuntimeStatusStore, routes: Iterable[RouteSpec]) -> None:
        self._store, self._routes = store, frozenset(routes)
        self.instance_id = uuid4()

    async def publish(self) -> None:
        try:
            async with asyncio.timeout(3):
                updated = await self._store.report(self.instance_id, self._routes)
            if not updated:
                logger.warning("Subscription runtime registration report rejected")
        except Exception:  # noqa: BLE001 - diagnostics must not affect execution or log credentials
            logger.warning("Subscription runtime registration report unavailable")

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=RUNTIME_REPORT_SECONDS)
            except TimeoutError:
                if not stop.is_set():
                    await self.publish()

    async def close(self) -> None:
        try:
            async with asyncio.timeout(3):
                await self._store.stop(self.instance_id)
        except Exception:  # noqa: BLE001 - an unrecorded stop becomes stale; it grants no authority
            logger.warning("Subscription runtime stop report unavailable")
