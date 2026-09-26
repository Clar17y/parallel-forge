"""Durable lifecycle adapter for official subscription clients."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from uuid import UUID

from forge.agents.client_process import (
    ClientProcessReceipt,
    ClientProcessResult,
    terminal_launch_proof,
)
from forge.application.ports.unit_of_work import UnitOfWork


class DurableClientProcessLifecycle:
    """Persists only process identity and bounded measured terminal evidence."""

    def __init__(
        self,
        work_factory: Callable[[], AbstractAsyncContextManager[UnitOfWork]],
        *,
        attempt_id: UUID,
        worker_identity: str,
    ) -> None:
        if not worker_identity:
            raise ValueError("worker identity is required")
        self._work_factory, self._attempt_id, self._worker_identity = (
            work_factory,
            attempt_id,
            worker_identity,
        )

    async def launch_intent(self, launch_id: str) -> None:
        async with self._work_factory() as work:
            await work.subscription.launch_intent(
                self._attempt_id, launch_id, worker_identity=self._worker_identity
            )
            await work.commit()

    async def started(self, receipt: ClientProcessReceipt) -> None:
        async with self._work_factory() as work:
            await work.subscription.launch_started(
                self._attempt_id,
                receipt.launch_id,
                worker_identity=self._worker_identity,
                pid=receipt.pid,
                process_start_token=receipt.process_start_token,
            )
            await work.commit()

    async def finished(
        self, receipt: ClientProcessReceipt, result: ClientProcessResult | None
    ) -> None:
        # Frames, stderr and monotonic time never enter durable storage.
        if result is not None and receipt != result.receipt:
            raise ValueError("terminal process receipt identity differs")
        terminal = None if result is None else terminal_launch_proof(result)
        async with self._work_factory() as work:
            await work.subscription.launch_finished(
                self._attempt_id,
                receipt.launch_id,
                worker_identity=self._worker_identity,
                terminal=terminal,
                uncertain=result is None or not result.stop_confirmed,
            )
            await work.commit()


__all__ = ["DurableClientProcessLifecycle"]
