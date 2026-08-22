"""One-tick durable worker orchestration."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge.application.ports.commands import CommandRepository
from forge.domain.command import CommandEnvelope
from forge.domain.event import RunEvent
from forge.domain.lease import validate_lease_seconds
from forge.persistence.unit_of_work import PostgresUnitOfWork


class TransientCommandError(RuntimeError):
    """A handler failure that is explicitly safe to retry."""


CommandHandler = Callable[..., Awaitable[object]]


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

        renewal = asyncio.create_task(self._renew_until_done(command))
        try:
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
                await _invoke_handler(handler, command, work)
                await work.commit()
        except asyncio.CancelledError:
            # UoW exit rolls back and the lease remains available for expiry/reclaim.
            raise
        except TransientCommandError as error:
            await self._commands.fail(
                command.id,
                worker_id=self._worker_id,
                error=str(error),
                transient=True,
            )
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
            renewal.cancel()
            with suppress(asyncio.CancelledError):
                await renewal

        await self._commands.complete(command.id, worker_id=self._worker_id)
        return True

    async def _renew_until_done(self, command: CommandEnvelope) -> None:
        delay = self._lease_seconds / 3
        while True:
            await asyncio.sleep(delay)
            try:
                await self._commands.renew(
                    command.id,
                    worker_id=self._worker_id,
                    lease_seconds=self._lease_seconds,
                )
            except Exception:  # noqa: BLE001 - lease loss stops renewal safely
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


__all__ = ["CommandHandler", "TransientCommandError", "Worker"]
