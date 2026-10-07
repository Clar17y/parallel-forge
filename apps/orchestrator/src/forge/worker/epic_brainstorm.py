"""Separate durable discovery worker with process authority and late-result fencing."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import os
import sys
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge.agents.client_process import (
    ClientProcessReceipt,
    ClientProcessResult,
    ClientProcessSupervisor,
    ProcessIdentityStatus,
    terminal_launch_proof,
)
from forge.application.ports.epic_brainstorm import (
    AuthoringGatewayResult,
    BrainstormGateway,
    BrainstormGatewayResult,
)
from forge.application.ports.repository import RepositoryReader
from forge.domain.epic_brainstorm import (
    _MAX_USAGE,
    AuthoringJobSnapshot,
    BrainstormConflict,
    BrainstormTurn,
    duration_floor,
)
from forge.domain.subscription import AttemptTelemetry, TaskBudget
from forge.domain.subscription_launch import SubscriptionLaunchTerminalProof
from forge.domain.subscription_quota import QuotaPolicy
from forge.persistence.models.epic_brainstorm import BrainstormAttemptRow, BrainstormJobRow
from forge.persistence.repositories.epic_brainstorm import PostgresBrainstormRepository
from forge.tools.epic_brainstorm import BrainstormReadOnlyTools

_OPERATION_GRACE_SECONDS = 10.0


def _invocation_expired(created_at: datetime, reservation: Mapping[str, object]) -> bool:
    """Evaluate the durable admission clock exactly, including after a restart."""
    duration_ms = reservation.get("duration_ms")
    if type(duration_ms) is not int or duration_ms < 1 or not isinstance(created_at, datetime):
        return True
    try:
        elapsed = datetime.now(UTC) - created_at
    except TypeError, OverflowError:
        return True
    elapsed_us = elapsed.days * 86_400_000_000 + elapsed.seconds * 1_000_000 + elapsed.microseconds
    return elapsed_us >= duration_ms * 1000


def brainstorm_worker_owner(base_worker_id: str) -> str:
    """Fit a configured worker identity into the durable attempt owner column."""
    if (
        not isinstance(base_worker_id, str)
        or not base_worker_id
        or len(base_worker_id) > 255
        or "\x00" in base_worker_id
    ):
        raise ValueError("invalid brainstorm worker identity")
    try:
        encoded = base_worker_id.encode("utf-8")
    except UnicodeError:
        raise ValueError("invalid brainstorm worker identity") from None
    suffix = "-brainstorm"
    if len(base_worker_id) + len(suffix) <= 128:
        return base_worker_id + suffix
    digest = hashlib.sha256(encoded).hexdigest()
    return f"{base_worker_id[:52]}-{digest}{suffix}"


@lru_cache(maxsize=1)
def worker_host_scope() -> str | None:
    """Fingerprint the machine and process-visibility namespace; fail closed if unavailable."""
    try:
        if sys.platform == "win32":
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography"
            ) as key:
                machine = winreg.QueryValueEx(key, "MachineGuid")[0]
            visibility = "windows-global-pid"
        elif sys.platform.startswith("linux"):
            machine = Path("/etc/machine-id").read_text(encoding="ascii").strip()
            namespace = os.readlink("/proc/self/ns/pid")
            boot = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
            if not namespace or not boot:
                return None
            visibility = f"{namespace}:{boot}"
        else:
            return None
        if not isinstance(machine, str) or not machine or not visibility:
            return None
        return hashlib.sha256(
            f"forge-brainstorm:{sys.platform}:{machine}:{visibility}".encode()
        ).hexdigest()
    except OSError, ValueError:
        return None


class DurableBrainstormProcessLifecycle:
    """A gateway records intent before launch and terminal proof after reaping."""

    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], attempt_id: UUID, fence: UUID, owner: str
    ) -> None:
        self.sessions, self.attempt_id, self.fence, self.owner = sessions, attempt_id, fence, owner

    async def _transition(
        self,
        action: str,
        launch_id: str | None = None,
        receipt: ClientProcessReceipt | None = None,
        result: ClientProcessResult | None = None,
    ) -> None:
        revoke_started = False
        async with self.sessions() as session, session.begin():
            job_id = await session.scalar(
                select(BrainstormAttemptRow.job_id).where(
                    BrainstormAttemptRow.id == self.attempt_id
                )
            )
            if job_id is None:
                raise BrainstormConflict("attempt authority revoked")
            job = await session.get(BrainstormJobRow, job_id, with_for_update=True)
            attempt = await session.get(BrainstormAttemptRow, self.attempt_id, with_for_update=True)
            if (
                attempt is None
                or job is None
                or attempt.fence != self.fence
                or attempt.owner != self.owner
                or job.current_attempt_id != self.attempt_id
            ):
                raise BrainstormConflict("attempt authority revoked")
            if action == "intent" and (
                job.state != "running"
                or attempt.lease_expires_at <= datetime.now(UTC)
                or _invocation_expired(attempt.created_at, attempt.reservation or {})
            ):
                raise BrainstormConflict("launch authority expired or cancelled")
            if action == "intent":
                if attempt.launch_intent:
                    if attempt.launch_id != launch_id:
                        raise BrainstormConflict("launch replay identity conflicts")
                    return
                attempt.launch_intent = True
                attempt.launch_id = launch_id
                attempt.origin_host = worker_host_scope()
            elif action == "started":
                if not attempt.launch_intent or attempt.process_settled or receipt is None:
                    raise BrainstormConflict("process start lacks launch intent")
                if receipt.launch_id != attempt.launch_id:
                    raise BrainstormConflict("process receipt differs from launch intent")
                revoke_started = (
                    job.state != "running"
                    or attempt.lease_expires_at <= datetime.now(UTC)
                    or _invocation_expired(attempt.created_at, attempt.reservation or {})
                )
                if attempt.process_started:
                    if (
                        attempt.process_pid != receipt.pid
                        or attempt.process_identity != receipt.process_start_token
                    ):
                        raise BrainstormConflict("process start replay identity conflicts")
                else:
                    # A child may already exist under a prior intent. Persist its
                    # real receipt before rejecting further work so finish can match.
                    attempt.process_pid = receipt.pid
                    attempt.process_identity = receipt.process_start_token
                    attempt.process_started = True
            else:
                if attempt.process_settled:
                    return
                if not attempt.launch_intent:
                    raise BrainstormConflict("process finish lacks launch intent")
                if (
                    receipt is None
                    or result is None
                    or result.receipt != receipt
                    or not result.stop_confirmed
                    or receipt.launch_id != attempt.launch_id
                ):
                    return
                if attempt.process_started and (
                    attempt.process_pid != receipt.pid
                    or attempt.process_identity != receipt.process_start_token
                ):
                    return
                # A physical child may be spawned just before the started receipt
                # fails to persist. Its matching stopped supervisor result is still
                # needed to release the held reservation.
                if not attempt.process_started:
                    attempt.process_pid = receipt.pid
                    attempt.process_identity = receipt.process_start_token
                attempt.terminal_proof = terminal_launch_proof(result).model_dump(mode="json")
                attempt.process_settled = True
            await session.flush()
        if revoke_started:
            raise BrainstormConflict("launch authority expired or cancelled")

    async def launch_intent(self, launch_id: str) -> None:
        await self._transition("intent", launch_id)

    async def started(self, receipt: ClientProcessReceipt) -> None:
        await self._transition("started", receipt=receipt)

    async def finished(
        self, receipt: ClientProcessReceipt | None, result: ClientProcessResult | None
    ) -> None:
        await self._transition("finished", receipt=receipt, result=result)


class EpicBrainstormWorker:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        owner: str,
        gateway_factory: Callable[[AuthoringJobSnapshot], BrainstormGateway],
        reader_factory: Callable[
            [AuthoringJobSnapshot], RepositoryReader | Awaitable[RepositoryReader]
        ],
        lease_seconds: int = 30,
        quota_policy: QuotaPolicy | None = None,
        epic_ceiling: TaskBudget | None = None,
        kinds: frozenset[str] = frozenset(("brainstorm",)),
    ) -> None:
        if (
            not isinstance(owner, str)
            or not owner
            or len(owner) > 128
            or "\x00" in owner
            or lease_seconds < 5
        ):
            raise ValueError("worker identity and bounded lease are required")
        try:
            owner.encode("utf-8")
        except UnicodeError:
            raise ValueError("worker identity and bounded lease are required") from None
        self.sessions, self.owner, self.gateway_factory, self.reader_factory = (
            sessions,
            owner,
            gateway_factory,
            reader_factory,
        )
        self.lease_seconds = lease_seconds
        if not kinds or not kinds <= frozenset(("brainstorm", "decomposition")):
            raise ValueError("authoring worker kinds are invalid")
        self.kinds = kinds
        self.quota_policy = quota_policy or QuotaPolicy()
        self.epic_ceiling = epic_ceiling
        self._active_operations: set[asyncio.Task[AuthoringGatewayResult]] = set()

    def _retire_operation(self, operation: asyncio.Task[AuthoringGatewayResult]) -> None:
        self._active_operations.discard(operation)
        if not operation.cancelled():
            # Retrieve without formatting or persisting provider exception text.
            operation.exception()

    def _track_operation(self, operation: asyncio.Task[AuthoringGatewayResult]) -> None:
        self._active_operations.add(operation)
        operation.add_done_callback(self._retire_operation)

    async def drain(self) -> None:
        operations = tuple(self._active_operations)
        if operations:
            done, pending = await asyncio.wait(operations, timeout=_OPERATION_GRACE_SECONDS)
            if pending:
                raise RuntimeError("authoring process did not settle during drain")
            for operation in done:
                self._retire_operation(operation)

    def _repository(self, session: AsyncSession) -> PostgresBrainstormRepository:
        return PostgresBrainstormRepository(
            session, quota_policy=self.quota_policy, epic_ceiling=self.epic_ceiling
        )

    @staticmethod
    def _host_duration_ms(attempt: BrainstormAttemptRow) -> int:
        return max(1, int((datetime.now(UTC) - attempt.created_at).total_seconds() * 1000))

    @classmethod
    def _host_usage(
        cls, attempt: BrainstormAttemptRow, *, launched: bool, recovery: bool = False
    ) -> dict[str, object]:
        previous = attempt.usage or {}
        lower = duration_floor(previous)
        observed = max(cls._host_duration_ms(attempt), lower)
        duration = None if recovery or observed > _MAX_USAGE else observed
        return {
            "duration_ms": duration,
            "duration_lower_bound_ms": lower if recovery else min(observed, _MAX_USAGE),
            "tool_call_count": attempt.tool_calls_used,
            "input_tokens": None if launched else 0,
            "output_tokens": None if launched else 0,
            "estimated_api_cost_minor": None if launched else 0,
            "currency": None,
        }

    @staticmethod
    def _quota_reset(result: AuthoringGatewayResult | None) -> datetime | None:
        if (
            result is None
            or not isinstance(result.quota_reset_at, str)
            or len(result.quota_reset_at) > 64
        ):
            return None
        try:
            value = datetime.fromisoformat(result.quota_reset_at)
        except ValueError:
            return None
        return value.astimezone(UTC) if value.tzinfo and value > datetime.now(UTC) else None

    async def reconcile_settled(self) -> UUID | None:
        """Recover stopped work after a worker restart; never invent missing output."""
        async with self.sessions() as session, session.begin():
            now = datetime.now(UTC)
            expired_ids = select(BrainstormAttemptRow.id).where(
                BrainstormAttemptRow.lease_expires_at <= now
            )
            rows = (
                await session.scalars(
                    select(BrainstormJobRow)
                    .where(
                        or_(
                            and_(
                                BrainstormJobRow.state == "reconciling",
                                BrainstormJobRow.current_attempt_id.is_not(None),
                            ),
                            and_(
                                BrainstormJobRow.state.in_(("running", "cancel_requested")),
                                BrainstormJobRow.current_attempt_id.in_(expired_ids),
                            ),
                        ),
                        or_(
                            BrainstormJobRow.next_eligible_at.is_(None),
                            BrainstormJobRow.next_eligible_at <= now,
                        ),
                    )
                    .order_by(
                        BrainstormJobRow.next_eligible_at.asc().nulls_first(),
                        BrainstormJobRow.created_at,
                        BrainstormJobRow.id,
                    )
                    .with_for_update(skip_locked=True)
                    .limit(50)
                )
            ).all()
            for row in rows:
                recovered = await self._reconcile_candidate(session, row)
                if recovered is not None:
                    return recovered
            return None

    async def _reconcile_candidate(
        self, session: AsyncSession, row: BrainstormJobRow
    ) -> UUID | None:
        if row.current_attempt_id is None:
            return None
        attempt = await session.get(
            BrainstormAttemptRow, row.current_attempt_id, with_for_update=True
        )
        if attempt is None:
            row.next_eligible_at = datetime.now(UTC) + timedelta(seconds=5)
            return None
        if not attempt.process_settled and attempt.launch_intent:
            host_gone = False
            if (
                attempt.process_started
                and attempt.launch_id
                and attempt.process_pid is not None
                and attempt.process_pid > 0
                and attempt.process_identity
                and attempt.origin_host is not None
                and attempt.origin_host == worker_host_scope()
            ):
                receipt = ClientProcessReceipt(
                    launch_id=attempt.launch_id,
                    pid=attempt.process_pid,
                    process_start_token=attempt.process_identity,
                    launched_monotonic=0,
                )
                host_gone = (
                    ClientProcessSupervisor.identity_status(receipt) is ProcessIdentityStatus.GONE
                )
            if host_gone:
                attempt.process_settled = True
                await self._repository(session).audit(
                    row.epic_id,
                    None,
                    "attempt_host_gone",
                    attempt.id,
                    {"launch_id": attempt.launch_id, "pid": attempt.process_pid},
                )
            else:
                attempt.usage = self._host_usage(attempt, launched=True, recovery=True)
                attempt.usage_known = False
                next_failure = (
                    "cancelled"
                    if row.state == "cancel_requested" or row.failure == "cancelled"
                    else "interrupted"
                    if row.failure == "interrupted"
                    else "timeout"
                    if row.failure == "timeout"
                    or _invocation_expired(attempt.created_at, attempt.reservation or {})
                    else "process_unsettled"
                )
                if row.state != "reconciling" or row.failure != next_failure:
                    row.failure = next_failure
                    row.state = "reconciling"
                    row.version += 1
                row.next_eligible_at = datetime.now(UTC) + timedelta(seconds=5)
                return None
        if not attempt.process_settled:
            attempt.process_settled = True
            attempt.usage_known = False
            attempt.usage = self._host_usage(attempt, launched=False, recovery=True)
        cancelled = row.state == "cancel_requested" or row.failure == "cancelled"
        row.state, row.failure = (
            ("cancelled", "cancelled")
            if cancelled
            else (
                "failed",
                "interrupted"
                if row.failure == "interrupted"
                else "timeout"
                if row.failure == "timeout"
                or _invocation_expired(attempt.created_at, attempt.reservation or {})
                else "lost_result",
            )
        )
        row.version += 1
        row.next_eligible_at = None
        attempt.state, attempt.failure = "settled", row.failure
        if attempt.launch_intent:
            attempt.usage_known = False
            attempt.usage = self._host_usage(attempt, launched=True, recovery=True)
        repository = self._repository(session)
        await repository.quota_settle(row, attempt, exhausted=False, reset_at=None)
        await repository.audit(
            row.epic_id,
            None,
            "attempt_reconciled",
            attempt.id,
            {"state": row.state, "usage_known": attempt.usage_known},
        )
        return row.id

    async def run_once(self, *, stop_event: asyncio.Event | None = None) -> UUID | None:
        stop = stop_event or asyncio.Event()
        job_cancel = asyncio.Event()
        if stop.is_set():
            return None
        reconciled = await self.reconcile_settled()
        if reconciled is not None:
            return reconciled
        async with self.sessions() as session, session.begin():
            repository = self._repository(session)
            claimed = await repository.claim(self.owner, self.lease_seconds, kinds=self.kinds)
            if claimed is None:
                return None
            row, attempt = claimed
            snapshot = repository.decode_snapshot(row)
            turns = await repository.frozen_history(
                snapshot, conversation_id=snapshot.conversation_id
            )
            attempt_id, fence = attempt.id, attempt.fence
            reservation = dict(attempt.reservation)
            admitted_at = attempt.created_at
        if _invocation_expired(admitted_at, reservation):
            await self._settle_not_launched(snapshot.job_id, attempt_id, fence, "timeout")
            return snapshot.job_id
        if stop.is_set():
            await self._settle_not_launched(snapshot.job_id, attempt_id, fence, "interrupted")
            return snapshot.job_id
        lifecycle = DurableBrainstormProcessLifecycle(self.sessions, attempt_id, fence, self.owner)

        async def cancelled() -> bool:
            if (
                stop.is_set()
                or job_cancel.is_set()
                or _invocation_expired(admitted_at, reservation)
            ):
                return True
            async with self.sessions() as session:
                row = await session.get(BrainstormJobRow, snapshot.job_id)
                persisted_cancelled = (
                    row is None
                    or row.state == "cancel_requested"
                    or row.current_attempt_id != attempt_id
                )
            return (
                _invocation_expired(admitted_at, reservation)
                or stop.is_set()
                or job_cancel.is_set()
                or persisted_cancelled
            )

        result: AuthoringGatewayResult | None = None
        failure = "unavailable"
        operation: asyncio.Task[AuthoringGatewayResult] | None = None
        interrupted_during_cleanup = False
        try:
            # Construction is lazy. No client exists until the durable admission above.
            invocation_budget = replace(
                snapshot.budget,
                max_duration_seconds=cast(int, reservation["duration_ms"]) // 1000,
                max_tool_calls=cast(int, reservation["tool_call_count"]),
                max_input_tokens=cast(int | None, reservation["input_tokens"]),
                max_output_tokens=cast(int | None, reservation["output_tokens"]),
                max_cost_minor=cast(int | None, reservation["estimated_api_cost_minor"]),
            )
            invocation = snapshot.model_copy(update={"budget": invocation_budget})
            gateway = self.gateway_factory(invocation)
            if _invocation_expired(admitted_at, reservation):
                await self._apply_bounded(snapshot, attempt_id, fence, None, "timeout", job_cancel)
                return snapshot.job_id

            async def authorize_tool() -> None:
                async with self.sessions() as session, session.begin():
                    row = await session.get(BrainstormJobRow, snapshot.job_id, with_for_update=True)
                    active = await session.get(
                        BrainstormAttemptRow, attempt_id, with_for_update=True
                    )
                    if (
                        row is None
                        or active is None
                        or row.state != "running"
                        or row.current_attempt_id != attempt_id
                        or active.owner != self.owner
                        or active.fence != fence
                        or active.lease_expires_at <= datetime.now(UTC)
                        or _invocation_expired(active.created_at, active.reservation or {})
                        or stop.is_set()
                        or job_cancel.is_set()
                    ):
                        raise BrainstormConflict("tool authority revoked")
                    if active.tool_calls_used >= cast(int, reservation["tool_call_count"]):
                        raise BrainstormConflict("tool budget exhausted")
                    active.tool_calls_used += 1

            async def invoke() -> AuthoringGatewayResult:
                constructed = self.reader_factory(snapshot)
                asynchronous_reader = inspect.isawaitable(constructed)
                repository_reader = (
                    await cast(Awaitable[RepositoryReader], constructed)
                    if asynchronous_reader
                    else cast(RepositoryReader, constructed)
                )
                if asynchronous_reader and await cancelled():
                    return BrainstormGatewayResult(
                        proposal=None, telemetry=None, failure="cancelled"
                    )
                reader = BrainstormReadOnlyTools(repository_reader, authorize=authorize_tool)
                return await gateway.execute(
                    invocation, turns, reader, cancelled=cancelled, lifecycle=lifecycle
                )

            operation = asyncio.create_task(invoke())
            self._track_operation(operation)
            while not operation.done():
                remaining = (
                    cast(int, reservation["duration_ms"]) / 1000
                    - (datetime.now(UTC) - admitted_at).total_seconds()
                )
                await asyncio.wait(
                    {operation},
                    timeout=min(1.0, max(0.0, remaining)),
                )
                if _invocation_expired(admitted_at, reservation):
                    failure = "timeout"
                    if not operation.done():
                        operation.cancel()
                    break
                if operation.done():
                    break
                if await cancelled():
                    if _invocation_expired(admitted_at, reservation):
                        failure = "timeout"
                    else:
                        failure = "interrupted"
                        job_cancel.set()
                    operation.cancel()
                else:
                    await self._renew(snapshot.job_id, attempt_id, fence)
                if operation.cancelled() or failure in {"interrupted", "timeout"}:
                    break
            done, _ = await asyncio.wait({operation}, timeout=_OPERATION_GRACE_SECONDS)
            if done:
                self._retire_operation(operation)
            if done and not operation.cancelled():
                try:
                    result = operation.result()
                except Exception:  # noqa: BLE001 - closed provider failure
                    if failure not in {"timeout", "interrupted"}:
                        failure = "unavailable"
            elif not done and failure not in {"timeout", "interrupted"}:
                failure = "process_unsettled"
        except asyncio.CancelledError:
            if failure != "interrupted":
                failure = (
                    "timeout" if _invocation_expired(admitted_at, reservation) else "interrupted"
                )
            if failure != "timeout":
                job_cancel.set()
            if operation is not None and not operation.done():
                if not operation.cancelling():
                    operation.cancel()
                await self._wait_for_operation(operation)
            await self._apply_bounded(snapshot, attempt_id, fence, result, failure, job_cancel)
            raise
        except Exception:  # noqa: BLE001 - provider errors receive a closed durable category
            if failure != "interrupted":
                failure = (
                    "timeout" if _invocation_expired(admitted_at, reservation) else "unavailable"
                )
            if operation is not None and not operation.done():
                if not operation.cancelling():
                    operation.cancel()
                interrupted_during_cleanup = await self._wait_for_operation(operation)
                if interrupted_during_cleanup and failure != "timeout":
                    job_cancel.set()
                    failure = "interrupted"
        interrupted = await self._apply_bounded(
            snapshot, attempt_id, fence, result, failure, job_cancel
        )
        if interrupted or interrupted_during_cleanup:
            raise asyncio.CancelledError
        return snapshot.job_id

    async def _wait_for_operation(self, operation: asyncio.Task[AuthoringGatewayResult]) -> bool:
        deadline = asyncio.get_running_loop().time() + _OPERATION_GRACE_SECONDS
        interrupted = False
        while not operation.done():
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            try:
                await asyncio.wait({operation}, timeout=remaining)
            except asyncio.CancelledError:
                # A second caller cancellation must not interrupt shielded process cleanup.
                interrupted = True
        if operation.done():
            self._retire_operation(operation)
        return interrupted

    async def _apply_bounded(
        self,
        snapshot: AuthoringJobSnapshot,
        attempt_id: UUID,
        fence: UUID,
        result: AuthoringGatewayResult | None,
        failure: str,
        job_cancel: asyncio.Event,
    ) -> bool:
        task = asyncio.create_task(
            self._apply(snapshot, attempt_id, fence, result, failure, job_cancel)
        )
        deadline = asyncio.get_running_loop().time() + 10
        interrupted = False
        while not task.done():
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                task.cancel()
                task.add_done_callback(
                    lambda done: done.exception() if not done.cancelled() else None
                )
                return interrupted
            try:
                await asyncio.wait({task}, timeout=remaining)
            except asyncio.CancelledError:
                interrupted = True
                if failure != "timeout":
                    job_cancel.set()
        task.result()
        return interrupted

    async def _renew(self, job_id: UUID, attempt_id: UUID, fence: UUID) -> None:
        async with self.sessions() as session, session.begin():
            row = await session.get(BrainstormJobRow, job_id, with_for_update=True)
            attempt = await session.get(BrainstormAttemptRow, attempt_id, with_for_update=True)
            if (
                row is None
                or attempt is None
                or row.current_attempt_id != attempt_id
                or row.state != "running"
                or attempt.owner != self.owner
                or attempt.fence != fence
                or attempt.lease_expires_at <= datetime.now(UTC)
                or _invocation_expired(attempt.created_at, attempt.reservation or {})
            ):
                raise BrainstormConflict("worker lease revoked")
            attempt.lease_expires_at = datetime.now(UTC) + timedelta(seconds=self.lease_seconds)

    async def _settle_not_launched(
        self, job_id: UUID, attempt_id: UUID, fence: UUID, failure: str
    ) -> None:
        async with self.sessions() as session, session.begin():
            row = await session.get(BrainstormJobRow, job_id, with_for_update=True)
            attempt = await session.get(BrainstormAttemptRow, attempt_id, with_for_update=True)
            if (
                row is None
                or attempt is None
                or row.current_attempt_id != attempt_id
                or attempt.fence != fence
                or attempt.launch_intent
                or attempt.process_settled
                or row.state not in ("running", "cancel_requested")
            ):
                return
            attempt.process_settled = True
            attempt.usage = self._host_usage(attempt, launched=False)
            attempt.usage_known = attempt.usage["duration_ms"] is not None
            attempt.state, attempt.failure = "settled", failure
            row.state, row.failure = (
                ("cancelled", "cancelled")
                if row.state == "cancel_requested"
                else ("failed", failure)
            )
            row.version += 1
            repository = self._repository(session)
            await repository.quota_settle(row, attempt, exhausted=False, reset_at=None)
            await repository.audit(
                row.epic_id,
                None,
                "attempt_settled",
                attempt_id,
                {"failure": failure, "not_launched": True},
            )

    async def _apply(
        self,
        snapshot: AuthoringJobSnapshot,
        attempt_id: UUID,
        fence: UUID,
        result: AuthoringGatewayResult | None,
        failure: str,
        stop: asyncio.Event,
    ) -> None:
        async with self.sessions() as session, session.begin():
            row = await session.get(BrainstormJobRow, snapshot.job_id, with_for_update=True)
            attempt = await session.get(BrainstormAttemptRow, attempt_id, with_for_update=True)
            if (
                row is None
                or attempt is None
                or row.current_attempt_id != attempt_id
                or attempt.fence != fence
                or row.state not in ("running", "cancel_requested")
                or attempt.state == "settled"
            ):
                return
            repository = self._repository(session)
            reset_at = self._quota_reset(result)
            interrupted = failure == "interrupted" or stop.is_set()
            timed_out = failure == "timeout" or (
                not interrupted
                and _invocation_expired(attempt.created_at, attempt.reservation or {})
            )
            if not attempt.launch_intent:
                attempt.process_settled = True
                attempt.usage = self._host_usage(attempt, launched=False)
                attempt.usage_known = attempt.usage["duration_ms"] is not None
            elif not attempt.process_settled:
                # Lease expiry, timeout, task cancellation, or gateway return is not
                # proof that an external process stopped. Keep the reservation.
                if result is not None and result.failure == "quota_exhausted":
                    await repository.quota_settle(
                        row,
                        attempt,
                        exhausted=True,
                        reset_at=reset_at,
                    )
                measured = self._host_usage(attempt, launched=True)
                measured["duration_ms"] = None
                attempt.usage = measured
                attempt.usage_known = False
                row.state, row.failure = (
                    "reconciling",
                    "cancelled"
                    if row.state == "cancel_requested"
                    else "interrupted"
                    if interrupted
                    else "timeout"
                    if timed_out
                    else "process_unsettled",
                )
                row.version += 1
                attempt.state = "reconciling"
                await repository.audit(
                    row.epic_id, None, "attempt_reconciling", attempt_id, {"launch_intent": True}
                )
                return
            telemetry: AttemptTelemetry | None = result.telemetry if result else None
            telemetry_valid = True
            safe_currency = None
            if telemetry is not None:
                unsafe_measurement = any(
                    value is not None and value > _MAX_USAGE
                    for value in (
                        telemetry.input_tokens,
                        telemetry.output_tokens,
                        telemetry.tool_call_count,
                        telemetry.duration_ms,
                        telemetry.estimated_api_cost_minor,
                    )
                )
                if unsafe_measurement:
                    telemetry_valid = False
                currency = telemetry.currency
                if currency is not None:
                    if (
                        isinstance(currency, str)
                        and len(currency) == 3
                        and currency.isascii()
                        and currency.isupper()
                        and currency.isalpha()
                    ):
                        safe_currency = currency
                    else:
                        telemetry_valid = False
                try:
                    snapshot.budget.unknown_telemetry_policy.validate_telemetry(
                        telemetry, snapshot.budget.billing_mode
                    )
                except ValueError:
                    telemetry_valid = False
                if snapshot.budget.unknown_telemetry_policy.max_uncertain_attempts == 0 and (
                    telemetry.input_tokens is None
                    or telemetry.output_tokens is None
                    or (
                        attempt.reservation["estimated_api_cost_minor"] is not None
                        and telemetry.estimated_api_cost_minor is None
                    )
                ):
                    telemetry_valid = False
            if telemetry is None and attempt.launch_intent:
                attempt.usage_known = False
                attempt.usage = self._host_usage(attempt, launched=True)
            elif telemetry is not None:
                measured_duration = max(telemetry.duration_ms, self._host_duration_ms(attempt))
                duration_known = measured_duration <= _MAX_USAGE
                if not duration_known:
                    telemetry_valid = False
                prior_lower = duration_floor(attempt.usage)
                safe_input = (
                    telemetry.input_tokens
                    if telemetry.input_tokens is None or telemetry.input_tokens <= _MAX_USAGE
                    else None
                )
                safe_output = (
                    telemetry.output_tokens
                    if telemetry.output_tokens is None or telemetry.output_tokens <= _MAX_USAGE
                    else None
                )
                safe_cost = (
                    telemetry.estimated_api_cost_minor
                    if telemetry.estimated_api_cost_minor is None
                    or telemetry.estimated_api_cost_minor <= _MAX_USAGE
                    else None
                )
                tool_unknown = telemetry.tool_call_count > _MAX_USAGE
                attempt.usage_known = (
                    telemetry_valid
                    and duration_known
                    and (safe_cost is None or safe_currency is not None)
                    and safe_input is not None
                    and safe_output is not None
                    and (
                        attempt.reservation["estimated_api_cost_minor"] is None
                        or safe_cost is not None
                    )
                )
                attempt.usage = {
                    "input_tokens": safe_input,
                    "output_tokens": safe_output,
                    "tool_call_count": max(
                        0 if tool_unknown else telemetry.tool_call_count,
                        attempt.tool_calls_used,
                    ),
                    "tool_call_count_unknown": tool_unknown,
                    "duration_ms": measured_duration if duration_known else None,
                    "duration_lower_bound_ms": (
                        min(max(self._host_duration_ms(attempt), prior_lower), _MAX_USAGE)
                        if not duration_known
                        else measured_duration
                    ),
                    "estimated_api_cost_minor": safe_cost,
                    "currency": safe_currency,
                }
            over_budget = telemetry is not None and (
                measured_duration > cast(int, attempt.reservation["duration_ms"])
                or max(telemetry.tool_call_count, attempt.tool_calls_used)
                > cast(int, attempt.reservation["tool_call_count"])
                or any(
                    value is not None and limit is not None and value > limit
                    for value, limit in (
                        (
                            telemetry.input_tokens,
                            cast(int | None, attempt.reservation["input_tokens"]),
                        ),
                        (
                            telemetry.output_tokens,
                            cast(int | None, attempt.reservation["output_tokens"]),
                        ),
                        (
                            telemetry.estimated_api_cost_minor,
                            cast(int | None, attempt.reservation["estimated_api_cost_minor"]),
                        ),
                    )
                )
            )
            is_cancelled = row.state == "cancel_requested"
            if is_cancelled:
                row.state, row.failure = "cancelled", "cancelled"
                attempt.failure = "cancelled"
            elif timed_out:
                row.state, row.failure = "failed", "timeout"
                attempt.failure = "timeout"
            elif interrupted:
                row.state, row.failure = "failed", "interrupted"
                attempt.failure = "interrupted"
            elif (
                result is not None
                and result.proposal is not None
                and result.failure is None
                and attempt.process_started
                and attempt.process_settled
                and attempt.terminal_proof is not None
                and SubscriptionLaunchTerminalProof.model_validate(
                    attempt.terminal_proof
                ).permits_decision
                and telemetry is not None
                and telemetry_valid
                and not over_budget
            ):
                proposal = result.proposal.model_copy(update={"turn_id": uuid4()})
                row.proposal = proposal.model_dump(mode="json")
                row.proposal_digest = proposal.digest
                row.state, row.failure = "proposed", None
                conversation = await repository.conversation(
                    row.epic_id, row.project_id, row.conversation_id, lock=True
                )
                await repository.append(
                    conversation,
                    BrainstormTurn(
                        turn_id=proposal.turn_id,
                        conversation_id=row.conversation_id,
                        role="assistant",
                        text=proposal.problem,
                    ),
                )
                attempt.failure = None
            elif result is not None and result.failure == "quota_exhausted":
                row.state, row.failure = "quota_wait", "quota_exhausted"
                row.next_eligible_at = reset_at or datetime.now(UTC) + timedelta(
                    seconds=self.quota_policy.unknown_reset_cooldown_seconds
                )
                attempt.failure = "quota_exhausted"
            else:
                safe_failures = {
                    "cancelled",
                    "timeout",
                    "unavailable",
                    "process_unsettled",
                    "invalid_output",
                    "budget_exhausted",
                    "quota_exhausted",
                }
                safe_failure = (
                    "invalid_output"
                    if not telemetry_valid
                    else "budget_exhausted"
                    if over_budget
                    else result.failure
                    if result
                    and isinstance(result.failure, str)
                    and result.failure in safe_failures
                    else failure
                )
                row.state, row.failure = "failed", safe_failure
                attempt.failure = row.failure
            attempt.state = "settled"
            row.version += 1
            await repository.quota_settle(
                row,
                attempt,
                exhausted=result is not None and result.failure == "quota_exhausted",
                reset_at=reset_at,
                succeeded=row.state == "proposed",
            )
            await repository.audit(
                row.epic_id,
                None,
                "attempt_settled",
                attempt_id,
                {"state": row.state, "usage_known": attempt.usage_known},
            )
