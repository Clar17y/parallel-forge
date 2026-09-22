"""Cancellation-aware durable worker process entry point."""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Iterable, Mapping
from contextlib import AbstractAsyncContextManager
from typing import cast
from uuid import uuid4

from forge.agents.runtime_factory import SubscriptionRuntimeAdapter
from forge.application.ports.commands import CommandLane, CommandRecoveryRequired
from forge.application.ports.operations import OperationAdapter
from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.services.recovery import RecoveryError, RecoveryService
from forge.application.services.subscription_decision_recovery import SubscriptionDecisionRecovery
from forge.application.services.subscription_effect_recovery import SubscriptionEffectRecovery
from forge.application.services.terminal_recovery import TerminalMergeRecovery
from forge.application.services.worker import CommandHandler, Worker
from forge.artifacts.filesystem import FilesystemArtifactStore
from forge.domain.local_cli import LocalCliTrust
from forge.domain.subscription_quota import PoolQuotaStatus, QuotaPoolKey
from forge.domain.subscription_readiness import SubscriptionRouteReadiness
from forge.persistence.database import create_engine, create_session_factory
from forge.persistence.repositories.capability_evidence import PostgresCapabilityEvidenceSource
from forge.persistence.repositories.capability_probe_diagnostics import (
    PostgresCapabilityProbeDiagnosticStore,
)
from forge.persistence.repositories.commands import PostgresCommandRepository
from forge.persistence.repositories.operations import PostgresOperationRepository
from forge.persistence.repositories.recovery import PostgresRecoveryBarrier, RecoveryBarrierLost
from forge.persistence.repositories.subscription_quota import PostgresSubscriptionQuotaRepository
from forge.persistence.unit_of_work import PostgresUnitOfWork
from forge.settings import Settings
from forge.worker.composition import WorkerCompositionError, WorkerHandlers, compose_worker_handlers
from forge.worker.startup import run_startup_recovery
from forge.worker.startup_intervention import StartupInterventionRecovery
from forge.worker.subscription_installations import (
    SubscriptionVerifierDependencies,
    load_subscription_installations_diagnostic,
    production_subscription_verifiers,
)
from forge.worker.subscription_invocation import SubscriptionInvocationWorker
from forge.worker.subscription_readiness import SubscriptionReadinessEnricher
from forge.worker.subscription_status import SubscriptionRuntimeReporter

logger = logging.getLogger(__name__)


async def run_worker(
    settings: Settings | None = None,
    *,
    adapters: Mapping[str, OperationAdapter] | None = None,
    subscription_adapters: Iterable[SubscriptionRuntimeAdapter] | None = None,
    handlers: Mapping[str, CommandHandler] | None = None,
    stop_event: asyncio.Event | None = None,
    poll_interval: float = 1.0,
    decision_retry_interval: float = 5.0,
    worker_id: str | None = None,
) -> None:
    """Build PostgreSQL dependencies, recover intents, then poll durably.

    Explicit registrations come from trusted host composition. When omitted, the
    public worker resolves the closed operator manifest with code-owned verifiers.
    Registration grants no account, billing or tool authority, and each adapter
    rechecks current evidence for every invocation. Explicit handlers own their
    own runtime dependencies.
    """

    if poll_interval <= 0 or poll_interval > 1:
        raise ValueError("worker idle poll interval must be between zero and one second")
    _validate_decision_retry_interval(decision_retry_interval)
    supplied_subscription_adapters = (
        None if subscription_adapters is None else tuple(subscription_adapters)
    )
    if handlers is not None and supplied_subscription_adapters:
        raise ValueError("subscription adapters require worker-owned composition")
    settings = settings or Settings(process_role="worker")
    stop_event = stop_event or asyncio.Event()
    engine = create_engine(settings.database_url)
    factory = create_session_factory(engine)
    worker: Worker | None = None
    control_worker: Worker | None = None
    owned_handlers: WorkerHandlers | None = None
    status_reporter: SubscriptionRuntimeReporter | None = None
    subscription_readiness = None
    subscription_readiness_supplier = None
    polls: list[asyncio.Task[None]] = []
    try:
        if supplied_subscription_adapters is not None:
            resolved_subscription_adapters = supplied_subscription_adapters
        elif (
            handlers is not None
            or getattr(settings, "subscription_installations_path", None) is None
        ):
            resolved_subscription_adapters = ()
        else:
            installation_load = load_subscription_installations_diagnostic(
                settings,
                production_subscription_verifiers(factory, settings.artifact_root)
                if settings.subscription_client_trust is LocalCliTrust.VERIFIED
                else SubscriptionVerifierDependencies(),
            )
            resolved_subscription_adapters = installation_load.adapters
            subscription_readiness = installation_load.readiness

            async def quota_status(key: QuotaPoolKey) -> PoolQuotaStatus:
                async with factory() as session:
                    return await PostgresSubscriptionQuotaRepository(
                        session, policy=settings.subscription_quota_policy
                    ).status(key)

            enricher = SubscriptionReadinessEnricher(
                PostgresCapabilityEvidenceSource(
                    factory, FilesystemArtifactStore(settings.artifact_root)
                )
                if settings.subscription_client_trust is LocalCliTrust.VERIFIED
                else None,
                quota_status,
                installation_load.specs,
                PostgresCapabilityProbeDiagnosticStore(factory),
            )

            async def subscription_readiness_supplier() -> tuple[SubscriptionRouteReadiness, ...]:
                return await enricher.enrich(subscription_readiness)

        commands = PostgresCommandRepository(factory)
        operations = PostgresOperationRepository(factory)
        recovery = RecoveryService(operations)
        effective_handlers: Mapping[str, CommandHandler] = handlers if handlers is not None else {}

        async def reconcile() -> None:
            nonlocal effective_handlers, owned_handlers
            intervention = StartupInterventionRecovery(factory)
            await intervention.wait_for_owners()
            if handlers is None:
                effective_handlers = compose_worker_handlers(
                    settings,
                    factory,
                    subscription_adapters=resolved_subscription_adapters,
                    subscription_readiness=subscription_readiness,
                    subscription_readiness_supplier=subscription_readiness_supplier,
                )
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
            await SubscriptionEffectRecovery(
                lambda: cast(AbstractAsyncContextManager[UnitOfWork], PostgresUnitOfWork(factory)),
                terminal_verifier=(
                    effective_handlers.tool_recovery
                    if isinstance(effective_handlers, WorkerHandlers)
                    else None
                ),
            ).reconcile_all()
            if (
                isinstance(effective_handlers, WorkerHandlers)
                and effective_handlers.subscription_decision_recovery is not None
            ):
                report = await effective_handlers.subscription_decision_recovery.reconcile_all()
                logger.info(
                    "Subscription decision recovery: applied=%d deferred=%d unsupported=%d",
                    report.applied,
                    report.deferred,
                    report.unsupported,
                )
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
        if isinstance(effective_handlers, WorkerHandlers):
            status_reporter = effective_handlers.subscription_status
        if status_reporter is not None:
            await status_reporter.publish()
        logger.info("Forge worker recovered and is polling")
        polls = [
            asyncio.create_task(_poll(worker, stop_event, poll_interval)),
            asyncio.create_task(_poll(control_worker, stop_event, poll_interval)),
        ]
        if status_reporter is not None:
            polls.append(asyncio.create_task(status_reporter.run(stop_event)))
        if (
            isinstance(effective_handlers, WorkerHandlers)
            and effective_handlers.subscription_decision_recovery is not None
        ):
            polls.append(
                asyncio.create_task(
                    _poll_decisions(
                        effective_handlers.subscription_decision_recovery,
                        stop_event,
                        decision_retry_interval,
                    )
                )
            )
        if (
            isinstance(effective_handlers, WorkerHandlers)
            and effective_handlers.subscription_invocations is not None
        ):
            for slot in range(settings.subscription_worker_concurrency):
                invocation = effective_handlers.subscription_invocations(
                    f"{base_worker_id}-subscription-{slot}"
                )
                polls.append(
                    asyncio.create_task(_poll_invocations(invocation, stop_event, poll_interval))
                )
        await asyncio.gather(*polls)
    finally:

        async def close_runtime() -> None:
            stop_event.set()
            for poll in polls:
                poll.cancel()
            await asyncio.gather(*polls, return_exceptions=True)
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
                try:
                    if status_reporter is not None:
                        await status_reporter.close()
                finally:
                    await engine.dispose()

        # Invocation cancellation must finish broker/process/usage settlement
        # before shared tool clients or PostgreSQL are closed. A second shutdown
        # signal must not abandon this cleanup task.
        cleanup = asyncio.create_task(close_runtime())
        cancelled = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                cancelled = True
        await cleanup
        if cancelled:
            raise asyncio.CancelledError


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


async def _poll_invocations(
    worker: SubscriptionInvocationWorker,
    stop_event: asyncio.Event,
    poll_interval: float,
) -> None:
    while not stop_event.is_set():
        if await worker.run_once(stop_event=stop_event) is None:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=poll_interval)
            except TimeoutError:
                pass


def _validate_decision_retry_interval(interval: float) -> None:
    if type(interval) not in (int, float) or not math.isfinite(interval) or not 0 < interval <= 60:
        raise ValueError("decision retry interval must be finite and between zero and 60 seconds")


async def _poll_decisions(
    recovery: SubscriptionDecisionRecovery,
    stop_event: asyncio.Event,
    interval: float,
) -> None:
    _validate_decision_retry_interval(interval)
    # Startup already performed the first scan. Delay before retries, and never
    # overlap scans in this worker or replay a source by invoking its provider.
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
            return
        except TimeoutError:
            if stop_event.is_set():
                return
        scan = asyncio.create_task(recovery.reconcile_all())
        stopped = asyncio.create_task(stop_event.wait())
        try:
            done, _ = await asyncio.wait((scan, stopped), return_when=asyncio.FIRST_COMPLETED)
            if scan not in done:
                return
            report = await scan
        finally:
            # Keep resources alive until an interrupted scan has closed its UoW
            # or artifact operation. A settled source remains replayable.
            scan.cancel()
            stopped.cancel()
            cleanup = asyncio.gather(scan, stopped, return_exceptions=True)
            cancelled = False
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    cancelled = True
            if cancelled:
                raise asyncio.CancelledError
        if report.applied:
            logger.info(
                "Subscription decision retry: applied=%d deferred=%d unsupported=%d",
                report.applied,
                report.deferred,
                report.unsupported,
            )


__all__ = ["run", "run_worker"]
