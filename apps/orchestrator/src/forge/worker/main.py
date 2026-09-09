"""Cancellation-aware durable worker process entry point."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from uuid import uuid4

from forge.application.ports.commands import CommandLane, CommandRecoveryRequired
from forge.application.ports.operations import OperationAdapter
from forge.application.services.recovery import RecoveryError, RecoveryService
from forge.application.services.terminal_recovery import TerminalMergeRecovery
from forge.application.services.worker import CommandHandler, Worker
from forge.persistence.database import create_engine, create_session_factory
from forge.persistence.repositories.commands import PostgresCommandRepository
from forge.persistence.repositories.operations import PostgresOperationRepository
from forge.persistence.repositories.recovery import PostgresRecoveryBarrier, RecoveryBarrierLost
from forge.settings import Settings
from forge.worker.composition import WorkerCompositionError, WorkerHandlers, compose_worker_handlers
from forge.worker.startup import run_startup_recovery
from forge.worker.startup_intervention import StartupInterventionRecovery

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
    stop_event = stop_event or asyncio.Event()
    engine = create_engine(settings.database_url)
    factory = create_session_factory(engine)
    worker: Worker | None = None
    control_worker: Worker | None = None
    owned_handlers: WorkerHandlers | None = None
    try:
        commands = PostgresCommandRepository(factory)
        operations = PostgresOperationRepository(factory)
        recovery = RecoveryService(operations)
        effective_handlers: Mapping[str, CommandHandler] = handlers if handlers is not None else {}

        async def reconcile() -> None:
            nonlocal effective_handlers, owned_handlers
            intervention = StartupInterventionRecovery(factory)
            await intervention.wait_for_owners()
            if handlers is None:
                effective_handlers = compose_worker_handlers(settings, factory)
                if isinstance(effective_handlers, WorkerHandlers):
                    owned_handlers = effective_handlers
            recovery_adapters = (
                dict(effective_handlers.recovery_adapters)
                if isinstance(effective_handlers, WorkerHandlers)
                else {}
            )
            recovery_adapters.update(adapters or {})
            await recovery.reconcile_all(recovery_adapters, allow_unresolved=True)
            if (
                isinstance(effective_handlers, WorkerHandlers)
                and effective_handlers.tool_recovery is not None
            ):
                await effective_handlers.tool_recovery.recover_all(allow_unresolved=True)
            await TerminalMergeRecovery(factory).reconcile_all()
            await intervention.quarantine()

        if not await run_startup_recovery(PostgresRecoveryBarrier(factory), reconcile, stop_event):
            return
        base_worker_id = worker_id or f"forge-worker-{uuid4().hex}"
        worker = Worker(
            commands,
            factory,
            handlers=effective_handlers,
            worker_id=base_worker_id,
            lease_seconds=30,
            lane=CommandLane.NORMAL,
        )
        control_worker = Worker(
            commands,
            factory,
            handlers=effective_handlers,
            worker_id=f"{base_worker_id}-control",
            lease_seconds=30,
            lane=CommandLane.CONTROL,
        )
        logger.info("Forge worker recovered and is polling")
        polls = (
            asyncio.create_task(_poll(worker, stop_event, poll_interval)),
            asyncio.create_task(_poll(control_worker, stop_event, poll_interval)),
        )
        try:
            await asyncio.gather(*polls)
        finally:
            for poll in polls:
                poll.cancel()
            await asyncio.gather(*polls, return_exceptions=True)
    finally:
        try:
            try:
                if worker is not None:
                    await worker.drain()
            finally:
                try:
                    if control_worker is not None:
                        await control_worker.drain()
                finally:
                    if owned_handlers is not None:
                        await owned_handlers.aclose()
        finally:
            await engine.dispose()


def run() -> None:
    """Run the worker until cancellation, exiting nonzero on failed recovery."""

    logging.basicConfig(level=logging.WARNING)
    logger.setLevel(logging.INFO)
    try:
        asyncio.run(run_worker())
    except KeyboardInterrupt:
        logger.info("Forge worker stopped")
    except (
        RecoveryError,
        RecoveryBarrierLost,
        CommandRecoveryRequired,
        WorkerCompositionError,
    ) as error:
        logger.error("Forge worker failed: %s", error)
        raise SystemExit(1) from error


async def _poll(worker: Worker, stop_event: asyncio.Event, poll_interval: float) -> None:
    while not stop_event.is_set():
        if await worker.tick() is None:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=poll_interval)
            except TimeoutError:
                pass


__all__ = ["run", "run_worker"]
