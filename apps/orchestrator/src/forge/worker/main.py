"""Cancellation-aware durable worker process entry point."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from uuid import uuid4

from forge.application.ports.operations import OperationAdapter
from forge.application.services.recovery import RecoveryError, RecoveryService
from forge.application.services.worker import CommandHandler, Worker
from forge.persistence.database import create_engine, create_session_factory
from forge.persistence.repositories.commands import PostgresCommandRepository
from forge.persistence.repositories.operations import PostgresOperationRepository
from forge.settings import Settings

logger = logging.getLogger(__name__)


async def run_worker(
    settings: Settings | None = None,
    *,
    adapters: Mapping[str, OperationAdapter] | None = None,
    handlers: Mapping[str, CommandHandler] | None = None,
    stop_event: asyncio.Event | None = None,
    poll_interval: float = 1.0,
    worker_id: str | None = None,
) -> None:
    """Build PostgreSQL dependencies, recover intents, then poll durably."""

    if poll_interval <= 0 or poll_interval > 1:
        raise ValueError("worker idle poll interval must be between zero and one second")
    settings = settings or Settings(process_role="worker")
    adapters = adapters or {}
    handlers = handlers or {}
    stop_event = stop_event or asyncio.Event()
    engine = create_engine(settings.database_url)
    factory = create_session_factory(engine)
    try:
        commands = PostgresCommandRepository(factory)
        operations = PostgresOperationRepository(factory)
        recovery = RecoveryService(operations)
        await recovery.reconcile_all(adapters)
        worker = Worker(
            commands,
            factory,
            handlers=handlers,
            worker_id=worker_id or f"forge-worker-{uuid4().hex}",
            lease_seconds=30,
        )
        logger.info("Forge worker recovered and is polling")
        while not stop_event.is_set():
            if await worker.tick() is None:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=poll_interval)
                except TimeoutError:
                    pass
    finally:
        await engine.dispose()


def run() -> None:
    """Run the worker until cancellation, exiting nonzero on failed recovery."""

    try:
        asyncio.run(run_worker())
    except KeyboardInterrupt:
        logger.info("Forge worker stopped")
    except RecoveryError as error:
        logger.error("Forge worker recovery failed: %s", error)
        raise SystemExit(1) from error


__all__ = ["run", "run_worker"]
