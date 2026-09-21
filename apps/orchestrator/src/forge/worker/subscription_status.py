"""Publish diagnostic registration independently of provider execution slots."""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable
from uuid import uuid4

from forge.domain.subscription import RouteSpec
from forge.domain.subscription_readiness import SubscriptionRouteReadiness
from forge.persistence.repositories.subscription_runtime_status import (
    RUNTIME_REPORT_SECONDS,
    SubscriptionRuntimeStatusStore,
)

logger = logging.getLogger(__name__)
# Leave two seconds of the 15-second reporting cadence for scheduling jitter
# after the independent persistence budget.
_SNAPSHOT_SECONDS = 10.0
_PERSIST_SECONDS = 3.0


class SubscriptionRuntimeReporter:
    def __init__(
        self,
        store: SubscriptionRuntimeStatusStore,
        routes: Iterable[RouteSpec | SubscriptionRouteReadiness],
        *,
        snapshot_supplier: Callable[[], Awaitable[Iterable[SubscriptionRouteReadiness]]]
        | None = None,
    ) -> None:
        self._store, self._routes = store, frozenset(routes)
        self._snapshot_supplier = snapshot_supplier
        self.instance_id = uuid4()

    async def publish(self) -> None:
        try:
            routes: Iterable[RouteSpec | SubscriptionRouteReadiness] = self._routes
            if self._snapshot_supplier is not None:
                try:
                    async with asyncio.timeout(_SNAPSHOT_SECONDS):
                        routes = tuple(await self._snapshot_supplier())
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - static routes remain safe diagnostics
                    logger.warning("Subscription runtime snapshot unavailable")
            async with asyncio.timeout(_PERSIST_SECONDS):
                updated = await self._store.report(self.instance_id, routes)
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
            async with asyncio.timeout(_PERSIST_SECONDS):
                await self._store.stop(self.instance_id)
        except Exception:  # noqa: BLE001 - an unrecorded stop becomes stale; it grants no authority
            logger.warning("Subscription runtime stop report unavailable")
