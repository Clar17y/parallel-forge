"""Own one subscription invocation from lease renewal through durable settlement."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any

from forge.application.ports.scheduling import SchedulingLeaseRevoked
from forge.application.ports.subscription_execution import (
    SubscriptionAdmission,
    SubscriptionSettlement,
)
from forge.application.ports.subscription_gateway import (
    SubscriptionFailure,
    SubscriptionGateway,
    SubscriptionInterrupted,
    SubscriptionInvocationRequest,
    SubscriptionInvocationResult,
)
from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
from forge.domain.lease import validate_lease_seconds


@dataclass(frozen=True, slots=True)
class SubscriptionAttemptOutcome:
    result: SubscriptionInvocationResult
    settlement: SubscriptionSettlement


class SubscriptionAttemptRunner:
    def __init__(
        self,
        work_factory: Callable[[], UnitOfWork],
        *,
        heartbeat_seconds: float = 5,
        lease_seconds: float = 30,
        cleanup_seconds: float = 5,
    ) -> None:
        timings = (heartbeat_seconds, lease_seconds, cleanup_seconds)
        if (
            any(
                type(value) not in (int, float) or not math.isfinite(value) or value <= 0
                for value in timings
            )
            or heartbeat_seconds >= lease_seconds / 2
        ):
            raise ValueError("attempt runner timings are invalid")
        validate_lease_seconds(lease_seconds)
        self._work_factory = work_factory
        self._heartbeat, self._lease, self._cleanup = timings
        self._executor = SubscriptionDecisionExecutor(work_factory)
        # Noncooperative callbacks remain owned and their eventual exceptions are
        # observed. Their attempts are settled UNCERTAIN, never released as safe.
        self._pending: set[asyncio.Task[Any]] = set()

    async def execute(
        self,
        admission: SubscriptionAdmission,
        request: SubscriptionInvocationRequest,
        gateway: SubscriptionGateway,
        revoke: Callable[[], Awaitable[None]],
        *,
        stop_event: asyncio.Event | None = None,
    ) -> SubscriptionAttemptOutcome:
        if (
            not isinstance(admission, SubscriptionAdmission)
            or not isinstance(request, SubscriptionInvocationRequest)
            or request.attempt != admission.attempt
            or request.task != admission.task
            or request.envelope != admission.envelope
        ):
            raise ValueError("invocation request differs from admission")
        stop = stop_event if stop_event is not None else asyncio.Event()
        operation = asyncio.create_task(self._execute(admission, request, gateway, revoke, stop))
        cancelled = False
        while True:
            try:
                outcome = await asyncio.shield(operation)
                break
            except asyncio.CancelledError:
                # Cancellation belongs to this coordinator, not the provider
                # task: revoke authority before delivering provider interruption.
                if operation.cancelled():
                    raise
                cancelled = True
                stop.set()
        if cancelled:
            raise SubscriptionInterrupted(outcome.result)
        return outcome

    async def _execute(
        self,
        admission: SubscriptionAdmission,
        request: SubscriptionInvocationRequest,
        gateway: SubscriptionGateway,
        revoke: Callable[[], Awaitable[None]],
        stop: asyncio.Event,
    ) -> SubscriptionAttemptOutcome:
        deadline = asyncio.get_running_loop().time() + request.budget.max_duration_seconds
        stopping = asyncio.create_task(stop.wait())
        provider: asyncio.Task[SubscriptionInvocationResult] | None = None
        try:
            failure = await self._renew_until(admission, stopping, deadline)
            if stop.is_set() and failure is None:
                failure = SubscriptionFailure.INTERRUPTED
            if failure is None:

                async def invoke() -> SubscriptionInvocationResult:
                    return await gateway.execute(request)

                provider = asyncio.create_task(invoke())
                while failure is None:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        failure = SubscriptionFailure.DEADLINE
                        break
                    await asyncio.wait(
                        {provider, stopping},
                        timeout=min(self._heartbeat, remaining),
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if stopping.done():
                        failure = SubscriptionFailure.INTERRUPTED
                        break
                    if asyncio.get_running_loop().time() >= deadline:
                        failure = SubscriptionFailure.DEADLINE
                        break
                    # Also renew after provider completion. A completion racing a
                    # failed renewal cannot become a successful decision.
                    failure = await self._renew_until(admission, stopping, deadline)
                    if provider.done():
                        break
            revoked = await self._revoke(revoke)
            if provider is not None and not provider.done():
                provider.cancel()
                if not await self._finished_within(provider):
                    self._retain(provider)
                    failure = SubscriptionFailure.UNCERTAIN
            if provider is not None and provider.done():
                result = self._result(provider, admission)
            else:
                result = SubscriptionInvocationResult(
                    attempt=admission.attempt, failure=failure or SubscriptionFailure.UNCERTAIN
                )
            if not revoked or result.failure is SubscriptionFailure.UNCERTAIN:
                failure = SubscriptionFailure.UNCERTAIN
            if stop.is_set() and failure is None:
                failure = SubscriptionFailure.INTERRUPTED
            if failure is not None:
                # Measured interruption evidence remains intact even when a
                # stronger uncertainty/control classification supersedes it.
                detail = result.failure_detail if failure is result.failure else None
                if detail is None:
                    detail = {
                        SubscriptionFailure.INTERRUPTED: "attempt stopped by control or cancellation",
                        SubscriptionFailure.DEADLINE: "attempt duration limit reached",
                        SubscriptionFailure.UNCERTAIN: "attempt termination or lease ownership is unconfirmed",
                    }.get(failure)
                result = replace(result, decision=None, failure=failure, failure_detail=detail)
            settlement = await self._executor.settle(admission, result)
            return SubscriptionAttemptOutcome(result, settlement)
        finally:
            stopping.cancel()
            await asyncio.gather(stopping, return_exceptions=True)

    async def _renew_until(
        self, admission: SubscriptionAdmission, stopping: asyncio.Task[bool], deadline: float
    ) -> SubscriptionFailure | None:
        if stopping.done():
            return SubscriptionFailure.INTERRUPTED
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return SubscriptionFailure.DEADLINE
        renewal = asyncio.create_task(self._renew(admission))
        await asyncio.wait(
            {renewal, stopping},
            timeout=min(remaining, self._lease - self._heartbeat),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not renewal.done():
            renewal.cancel()
            if not await self._finished_within(renewal):
                self._retain(renewal)
                return SubscriptionFailure.UNCERTAIN
        if stopping.done():
            self._observe(renewal)
            return SubscriptionFailure.INTERRUPTED
        try:
            renewal.result()
        except asyncio.CancelledError:
            return (
                SubscriptionFailure.DEADLINE
                if asyncio.get_running_loop().time() >= deadline
                else SubscriptionFailure.UNCERTAIN
            )
        except SchedulingLeaseRevoked:
            return SubscriptionFailure.INTERRUPTED
        except Exception:  # noqa: BLE001 - failed renewal cannot establish continued ownership
            return SubscriptionFailure.UNCERTAIN
        if asyncio.get_running_loop().time() >= deadline:
            return SubscriptionFailure.DEADLINE
        return None

    async def _renew(self, admission: SubscriptionAdmission) -> None:
        async with self._work_factory() as work:
            await work.scheduler.renew(admission.lease, timedelta(seconds=self._lease))
            await work.commit()

    async def _revoke(self, revoke: Callable[[], Awaitable[None]]) -> bool:
        async def invoke() -> None:
            await revoke()

        revocation = asyncio.create_task(invoke())
        if not await self._finished_within(revocation):
            revocation.cancel()
            self._retain(revocation)
            return False
        try:
            revocation.result()
            return True
        except Exception, asyncio.CancelledError:  # noqa: BLE001 - failed revocation fences result
            return False

    async def _finished_within(self, task: asyncio.Task[Any]) -> bool:
        await asyncio.wait({task}, timeout=self._cleanup)
        return task.done()

    @staticmethod
    def _result(
        task: asyncio.Task[SubscriptionInvocationResult], admission: SubscriptionAdmission
    ) -> SubscriptionInvocationResult:
        try:
            result = task.result()
        except SubscriptionInterrupted as interrupted:
            result = interrupted.result
        except asyncio.CancelledError:
            return SubscriptionInvocationResult(
                attempt=admission.attempt, failure=SubscriptionFailure.INTERRUPTED
            )
        except Exception:  # noqa: BLE001 - untrusted provider failure becomes a protocol failure
            return SubscriptionInvocationResult(
                attempt=admission.attempt, failure=SubscriptionFailure.PROTOCOL
            )
        if (
            not isinstance(result, SubscriptionInvocationResult)
            or result.attempt != admission.attempt
        ):
            return SubscriptionInvocationResult(
                attempt=admission.attempt, failure=SubscriptionFailure.PROTOCOL
            )
        return result

    def _retain(self, task: asyncio.Task[Any]) -> None:
        self._pending.add(task)

        def finished(value: asyncio.Task[Any]) -> None:
            self._pending.discard(value)
            self._observe(value)

        task.add_done_callback(finished)

    @staticmethod
    def _observe(task: asyncio.Task[Any]) -> None:
        try:
            task.result()
        except Exception, asyncio.CancelledError:  # noqa: BLE001 - observe quarantined task safely
            return
