"""One-tick durable worker orchestration."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge.application.ports.commands import (
    CommandLeaseLost,
    CommandRecoveryRequired,
    CommandRepository,
)
from forge.domain.command import CommandEnvelope
from forge.domain.event import RunEvent
from forge.domain.lease import validate_lease_seconds
from forge.persistence.unit_of_work import PostgresUnitOfWork


class TransientCommandError(RuntimeError):
    """A handler failure that is explicitly safe to retry."""


class LeaseLostCommandError(CommandLeaseLost):
    """Renewal proved this worker can no longer settle its active delivery."""


CommandHandler = Callable[..., Awaitable[object]]
_HANDLER_DRAIN_SECONDS = 1


class Worker:
    """Claim at most one command and complete it after its UoW commits."""

    def __init__(
        self,
        commands: CommandRepository,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        handlers: Mapping[str, CommandHandler],
        worker_id: str,
        lease_seconds: float = 30,
    ) -> None:
        self._commands = commands
        self._session_factory = session_factory
        self._handlers = dict(handlers)
        self._worker_id = worker_id
        validate_lease_seconds(lease_seconds)
        self._lease_seconds = lease_seconds
        self._draining: set[asyncio.Task[object]] = set()

    async def tick(self) -> bool | None:
        """Process one command, returning ``None`` when the queue is idle."""

        command = await self._commands.claim_next(
            worker_id=self._worker_id,
            lease_seconds=self._lease_seconds,
        )
        if command is None:
            return None
        handler = self._handlers.get(command.command_type)
        if handler is None:
            await self._commands.fail(
                command.id,
                worker_id=self._worker_id,
                error=f"unknown command type: {command.command_type}",
                transient=False,
            )
            return False

        lease_lost = asyncio.Event()
        handler_cancelling = asyncio.Event()
        renewal = asyncio.create_task(self._renew_until_done(command, lease_lost))
        processing = asyncio.create_task(
            self._process_handler(handler, command, lease_lost, handler_cancelling)
        )
        loss_wait = asyncio.create_task(lease_lost.wait())
        try:
            done, _ = await asyncio.wait(
                {processing, loss_wait}, return_when=asyncio.FIRST_COMPLETED
            )
            if loss_wait in done and not processing.done():
                # The processing task retains its UoW until the cancelled
                # handler drains. Returning here keeps this tick bounded
                # without closing a session still owned by that task.
                self._retain_draining_task(processing)
                with suppress(TimeoutError):
                    await asyncio.wait_for(
                        handler_cancelling.wait(), timeout=_HANDLER_DRAIN_SECONDS
                    )
                return False
            await processing
        except asyncio.CancelledError:
            processing.cancel()
            self._retain_draining_task(processing)
            raise
        except TransientCommandError as error:
            await self._commands.fail(
                command.id,
                worker_id=self._worker_id,
                error=str(error),
                transient=True,
            )
            return False
        except CommandLeaseLost:
            # A reclaiming worker owns the next disposition.  In particular,
            # recovery must not turn an admitted RUNNING execution into a
            # terminally failed command while its original provider returns.
            return False
        except CommandRecoveryRequired:
            # Leave the reclaiming delivery leased for an explicit recovery
            # owner. A terminal failure could race the original provider's
            # admitted execution before it settles.
            return False
        except Exception as error:  # noqa: BLE001 - all non-transient handler failures are terminal
            # Policy, integrity, authorization, and unknown handler errors are
            # terminal until a handler explicitly maps them to a transient error.
            await self._commands.fail(
                command.id,
                worker_id=self._worker_id,
                error=str(error),
                transient=False,
            )
            return False
        finally:
            loss_wait.cancel()
            with suppress(asyncio.CancelledError):
                await loss_wait
            renewal.cancel()
            with suppress(asyncio.CancelledError):
                await renewal

        try:
            await self._commands.complete(command.id, worker_id=self._worker_id)
        except CommandLeaseLost:
            return False
        return True

    async def drain(self) -> None:
        """Wait for handlers retained after lease-loss cancellation."""

        if self._draining:
            await asyncio.gather(*tuple(self._draining), return_exceptions=True)

    async def _process_handler(
        self,
        handler: CommandHandler,
        command: CommandEnvelope,
        lease_lost: asyncio.Event,
        handler_cancelling: asyncio.Event,
    ) -> None:
        async with PostgresUnitOfWork(self._session_factory) as work:
            await work.events.append(
                RunEvent(
                    run_id=command.run_id,
                    run_version=command.expected_run_version,
                    event_type="command.started",
                    payload={
                        "command_id": str(command.id),
                        "command_type": command.command_type,
                        "attempt": command.attempt,
                    },
                    actor_class="worker",
                )
            )
            await self._invoke_with_lease_watch(
                handler, command, work, lease_lost, handler_cancelling
            )
            await work.commit()

    async def _invoke_with_lease_watch(
        self,
        handler: CommandHandler,
        command: CommandEnvelope,
        work: PostgresUnitOfWork,
        lease_lost: asyncio.Event,
        handler_cancelling: asyncio.Event,
    ) -> None:
        handler_task = asyncio.create_task(_invoke_handler(handler, command, work))
        lost_task = asyncio.create_task(lease_lost.wait())
        try:
            done, _ = await asyncio.wait(
                {handler_task, lost_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if lost_task in done:
                handler_cancelling.set()
                await self._cancel_and_drain(handler_task)
                raise LeaseLostCommandError("command lease renewal failed")
            await handler_task
            if lease_lost.is_set():
                raise LeaseLostCommandError("command lease renewal failed")
        finally:
            lost_task.cancel()
            with suppress(asyncio.CancelledError):
                await lost_task
            if not handler_task.done():
                handler_cancelling.set()
                await self._cancel_and_drain(handler_task)

    @staticmethod
    async def _cancel_and_drain(handler_task: asyncio.Task[object]) -> None:
        handler_task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(handler_task), _HANDLER_DRAIN_SECONDS)
        except TimeoutError:
            # A task that suppresses cancellation may still hold the UoW.
            # Its owning processing task remains alive and observes its late
            # result before the UoW can close.
            await handler_task

    def _retain_draining_task(self, task: asyncio.Task[object]) -> None:
        self._draining.add(task)

        def observe(completed: asyncio.Task[object]) -> None:
            self._draining.discard(completed)
            if not completed.cancelled():
                with suppress(Exception):
                    completed.exception()

        task.add_done_callback(observe)

    async def _renew_until_done(self, command: CommandEnvelope, lease_lost: asyncio.Event) -> None:
        delay = self._lease_seconds / 3
        while True:
            await asyncio.sleep(delay)
            try:
                await self._commands.renew(
                    command.id,
                    worker_id=self._worker_id,
                    lease_seconds=self._lease_seconds,
                )
            except Exception:  # noqa: BLE001 - a handler must not outlive a lost lease
                lease_lost.set()
                return


async def _invoke_handler(
    handler: CommandHandler, command: CommandEnvelope, work: PostgresUnitOfWork
) -> None:
    """Invoke handlers with the explicit two-argument contract, allowing one-arg adapters."""

    parameters = inspect.signature(handler).parameters
    if len(parameters) >= 2:
        await handler(command, work)
    else:
        await handler(command)


__all__ = ["CommandHandler", "LeaseLostCommandError", "TransientCommandError", "Worker"]
