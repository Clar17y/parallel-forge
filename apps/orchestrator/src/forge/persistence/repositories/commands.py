"""PostgreSQL repository for durable commands and worker leases."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge.application.ports.commands import CommandLane, CommandLeaseLost
from forge.domain.command import CommandEnvelope, CommandStatus, thaw_payload
from forge.domain.lease import validate_lease_seconds
from forge.domain.payload import redact_durable_text
from forge.persistence.models import Run, RunCommand
from forge.persistence.repositories.runs import PersistenceDataError


class CommandError(RuntimeError):
    """Base class for command persistence errors."""


class CommandNotFound(CommandError):
    """The requested command does not exist."""


class IdempotencyConflict(CommandError):
    """An idempotency key was reused for a different immutable request."""


class CommandLeaseError(CommandLeaseLost, CommandError):
    """The worker does not hold a current lease for this command."""


class CommandStateConflict(CommandError):
    """A terminal command was asked to take a contradictory terminal result."""


_MAX_ERROR_LENGTH = 1024
_CONTROL_COMMAND_TYPES = frozenset({"pause", "cancel"})


class PostgresCommandRepository:
    """Persist commands with explicit idempotency and row-lock lease semantics."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        *,
        session: AsyncSession | None = None,
    ) -> None:
        if (session_factory is None) == (session is None):
            raise ValueError("provide exactly one command persistence boundary")
        self._session_factory = session_factory
        self._session = session

    async def enqueue(
        self,
        *,
        run_id: UUID,
        command_type: str,
        idempotency_key: str,
        payload: Mapping[str, object],
        expected_run_version: int = 0,
        actor_id: UUID | None = None,
        payload_schema_version: int = 1,
        available_at: datetime | None = None,
    ) -> CommandEnvelope:
        requested_available_at = available_at
        available_at = available_at or _utc_now()
        candidate = CommandEnvelope(
            id=uuid4(),
            run_id=run_id,
            command_type=command_type,
            idempotency_key=idempotency_key,
            payload=payload,
            status=CommandStatus.PENDING,
            expected_run_version=expected_run_version,
            actor_id=actor_id,
            payload_schema_version=payload_schema_version,
            attempt=0,
            available_at=available_at,
            lease_owner=None,
            lease_expires_at=None,
        )
        if self._session is not None:
            return await self._enqueue_core(
                self._session,
                candidate,
                compare_availability=requested_available_at is not None,
            )
        if self._session_factory is None:
            raise CommandError("command persistence boundary is not configured")
        async with self._factory()() as session, session.begin():
            return await self._enqueue_core(
                session,
                candidate,
                compare_availability=requested_available_at is not None,
            )

    async def _enqueue_core(
        self,
        session: AsyncSession,
        candidate: CommandEnvelope,
        *,
        compare_availability: bool,
    ) -> CommandEnvelope:
        inserted = await session.execute(
            insert(RunCommand)
            .values(
                id=candidate.id,
                run_id=candidate.run_id,
                command_type=candidate.command_type,
                idempotency_key=candidate.idempotency_key,
                expected_run_version=candidate.expected_run_version,
                actor_id=candidate.actor_id,
                payload_schema_version=candidate.payload_schema_version,
                payload=thaw_payload(candidate.payload),
                status="PENDING",
                available_at=candidate.available_at,
                attempt_count=0,
            )
            .on_conflict_do_nothing(index_elements=[RunCommand.idempotency_key])
            .returning(RunCommand.id)
        )
        inserted_id = inserted.scalar_one_or_none()
        record = await session.get(RunCommand, inserted_id or candidate.id)
        if record is None:
            record = (
                await session.execute(
                    select(RunCommand).where(
                        RunCommand.idempotency_key == candidate.idempotency_key
                    )
                )
            ).scalar_one_or_none()
        if record is None:
            raise CommandError("command insert disappeared before it could be loaded")
        actual = _command_from_record(record)
        if inserted_id is None:
            _check_enqueue_match(
                candidate,
                actual,
                compare_availability=compare_availability,
            )
        return actual

    async def get(self, command_id: UUID) -> CommandEnvelope:
        if self._session is not None:
            record = await self._session.get(RunCommand, command_id)
            if record is None:
                raise CommandNotFound(f"command {command_id} was not found")
            return _command_from_record(record)
        async with self._factory()() as session:
            record = await session.get(RunCommand, command_id)
            if record is None:
                raise CommandNotFound(f"command {command_id} was not found")
            return _command_from_record(record)

    async def get_by_idempotency_key(self, idempotency_key: str) -> CommandEnvelope | None:
        """Load an existing command from the active transaction by its key."""

        if self._session is not None:
            record = (
                await self._session.execute(
                    select(RunCommand).where(RunCommand.idempotency_key == idempotency_key)
                )
            ).scalar_one_or_none()
            return None if record is None else _command_from_record(record)
        async with self._factory()() as session:
            record = (
                await session.execute(
                    select(RunCommand).where(RunCommand.idempotency_key == idempotency_key)
                )
            ).scalar_one_or_none()
            return None if record is None else _command_from_record(record)

    async def claim_next(
        self,
        *,
        worker_id: str,
        lease_seconds: float,
        lane: CommandLane = CommandLane.NORMAL,
    ) -> CommandEnvelope | None:
        _validate_worker_and_lease(worker_id, lease_seconds)
        if not isinstance(lane, CommandLane):
            raise TypeError("command lane is invalid")
        now = _utc_now()
        expiry = now + timedelta(seconds=lease_seconds)
        async with self._factory()() as session, session.begin():
            skipped: set[UUID] = set()
            while True:
                eligibility = [
                    RunCommand.available_at <= now,
                    or_(
                        RunCommand.status == "PENDING",
                        and_(
                            RunCommand.status == "LEASED",
                            RunCommand.lease_expires_at <= now,
                        ),
                    ),
                ]
                if lane is CommandLane.CONTROL:
                    eligibility.append(RunCommand.command_type.in_(_CONTROL_COMMAND_TYPES))
                else:
                    eligibility.append(RunCommand.command_type.not_in(_CONTROL_COMMAND_TYPES))
                if skipped:
                    eligibility.append(~RunCommand.id.in_(skipped))
                result = await session.execute(
                    select(RunCommand)
                    .where(*eligibility)
                    .order_by(
                        RunCommand.available_at,
                        RunCommand.created_at,
                        RunCommand.id,
                    )
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
                record = result.scalar_one_or_none()
                if record is None:
                    return None

                run_state = await session.scalar(
                    select(Run.state).where(Run.id == record.run_id).with_for_update()
                )
                if run_state is None:
                    raise PersistenceDataError(f"command {record.id} references a missing run")
                if lane is CommandLane.NORMAL and (
                    (run_state == "PAUSED" and record.command_type != "resume")
                    or (
                        run_state in {"CANCELLED", "COMPLETED", "FAILED"}
                        and record.command_type != "teardown_run_resources"
                    )
                ):
                    skipped.add(record.id)
                    continue
                active_normal_lease = await session.scalar(
                    select(func.count())
                    .select_from(RunCommand)
                    .where(
                        RunCommand.run_id == record.run_id,
                        RunCommand.id != record.id,
                        RunCommand.status == "LEASED",
                        RunCommand.lease_expires_at > now,
                        RunCommand.command_type.not_in(_CONTROL_COMMAND_TYPES),
                    )
                )
                active_control_lease = await session.scalar(
                    select(func.count())
                    .select_from(RunCommand)
                    .where(
                        RunCommand.run_id == record.run_id,
                        RunCommand.id != record.id,
                        RunCommand.status == "LEASED",
                        RunCommand.lease_expires_at > now,
                        RunCommand.command_type.in_(_CONTROL_COMMAND_TYPES),
                    )
                )
                pending_control = await session.scalar(
                    select(func.count())
                    .select_from(RunCommand)
                    .where(
                        RunCommand.run_id == record.run_id,
                        RunCommand.id != record.id,
                        RunCommand.status == "PENDING",
                        RunCommand.available_at <= now,
                        RunCommand.command_type.in_(_CONTROL_COMMAND_TYPES),
                    )
                )
                blocked = (
                    int(active_control_lease or 0) > 0
                    if lane is CommandLane.CONTROL
                    else int(active_normal_lease or 0) > 0
                    or int(active_control_lease or 0) > 0
                    or int(pending_control or 0) > 0
                )
                if blocked:
                    skipped.add(record.id)
                    continue

                record.status = "LEASED"
                record.lease_owner = worker_id
                record.lease_expires_at = expiry
                record.attempt_count += 1
                record.completed_at = None
                await session.flush()
                return _command_from_record(record)

    async def renew(
        self, command_id: UUID, *, worker_id: str, lease_seconds: float
    ) -> CommandEnvelope:
        _validate_worker_and_lease(worker_id, lease_seconds)
        now = _utc_now()
        async with self._factory()() as session, session.begin():
            record = await self._locked(command_id, session)
            _require_owned_lease(record, worker_id, now)
            record.lease_expires_at = now + timedelta(seconds=lease_seconds)
            await session.flush()
            return _command_from_record(record)

    async def assert_current_lease(self, command: CommandEnvelope) -> CommandEnvelope:
        """Lock and validate the exact delivery that is settling a command.

        This method is deliberately session-bound: the caller's final run and
        execution updates commit only if this lease fence remains true.
        """

        if command.status is not CommandStatus.LEASED or command.lease_owner is None:
            raise CommandLeaseError("command delivery has no lease to fence")
        if self._session is None:
            raise CommandError("lease fence requires an active unit of work")
        record = await self._locked(command.id, self._session)
        _require_owned_lease(record, command.lease_owner, _utc_now())
        if record.attempt_count != command.attempt:
            raise CommandLeaseError("command lease delivery was reclaimed")
        return _command_from_record(record)

    async def has_pending_current_control_stop(
        self, *, run_id: UUID, expected_run_version: int
    ) -> bool:
        """Return whether a valid current-version stop is awaiting settlement.

        This query is intentionally session-bound.  Its caller must already
        hold the corresponding run row lock, which also serializes API control
        admission.  Stale or malformed controls grant no finalization fence;
        their worker delivery remains responsible for strict rejection.
        """

        if self._session is None:
            raise CommandError("control-stop fence requires an active unit of work")
        pending = await self._session.scalar(
            select(RunCommand.id)
            .where(
                RunCommand.run_id == run_id,
                RunCommand.expected_run_version == expected_run_version,
                RunCommand.command_type.in_(_CONTROL_COMMAND_TYPES),
                RunCommand.status.in_(("PENDING", "LEASED")),
                RunCommand.payload_schema_version == 1,
                RunCommand.payload == {},
                RunCommand.actor_id.is_not(None),
            )
            .limit(1)
        )
        return pending is not None

    async def list_outstanding_normal(
        self, *, run_id: UUID, exclude_command_id: UUID
    ) -> list[CommandEnvelope]:
        """Load paused-run blockers under its already-held run-row lock."""

        if self._session is None:
            raise CommandError("outstanding command inspection requires an active unit of work")
        records = (
            (
                await self._session.execute(
                    select(RunCommand)
                    .where(
                        RunCommand.run_id == run_id,
                        RunCommand.id != exclude_command_id,
                        RunCommand.command_type.not_in(_CONTROL_COMMAND_TYPES),
                        RunCommand.status.in_(("PENDING", "LEASED")),
                    )
                    .order_by(RunCommand.created_at, RunCommand.id)
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        return [_command_from_record(record) for record in records]

    async def cancel_expired_observed_lease(
        self, command: CommandEnvelope, *, reason: str
    ) -> CommandEnvelope | None:
        """Settle exactly one expired observed normal lease without reclaiming it."""

        if self._session is None:
            raise CommandError("recovery settlement requires an active unit of work")
        if (
            command.status is not CommandStatus.LEASED
            or command.lease_owner is None
            or command.lease_expires_at is None
        ):
            raise CommandLeaseError("recovery settlement requires an observed leased command")
        result = await self._session.execute(
            update(RunCommand)
            .where(
                RunCommand.id == command.id,
                RunCommand.run_id == command.run_id,
                RunCommand.status == "LEASED",
                RunCommand.lease_owner == command.lease_owner,
                RunCommand.attempt_count == command.attempt,
                RunCommand.lease_expires_at == command.lease_expires_at,
                RunCommand.lease_expires_at <= func.now(),
                RunCommand.command_type.not_in(_CONTROL_COMMAND_TYPES),
            )
            .values(
                status="CANCELLED",
                lease_owner=None,
                lease_expires_at=None,
                completed_at=func.now(),
                error_summary=_bounded_error(reason),
            )
            .returning(RunCommand)
        )
        record = result.scalar_one_or_none()
        return None if record is None else _command_from_record(record)

    async def cancel_pending_unstarted(self, command: CommandEnvelope) -> CommandEnvelope | None:
        """Cancel only an unchanged pending delivery which has never been claimed."""

        if self._session is None:
            raise CommandError("deferred settlement requires an active unit of work")
        if (
            command.status is not CommandStatus.PENDING
            or command.attempt != 0
            or command.lease_owner is not None
            or command.lease_expires_at is not None
        ):
            raise CommandLeaseError("deferred settlement requires an unstarted pending command")
        result = await self._session.execute(
            update(RunCommand)
            .where(
                RunCommand.id == command.id,
                RunCommand.run_id == command.run_id,
                RunCommand.command_type == command.command_type,
                RunCommand.idempotency_key == command.idempotency_key,
                RunCommand.expected_run_version == command.expected_run_version,
                RunCommand.actor_id == command.actor_id,
                RunCommand.payload_schema_version == command.payload_schema_version,
                RunCommand.payload == thaw_payload(command.payload),
                RunCommand.status == "PENDING",
                RunCommand.attempt_count == 0,
                RunCommand.lease_owner.is_(None),
                RunCommand.lease_expires_at.is_(None),
                RunCommand.command_type.not_in(_CONTROL_COMMAND_TYPES),
            )
            .values(
                status="CANCELLED",
                completed_at=func.now(),
                error_summary="delivery deferred before admission during pause reconciliation",
            )
            .returning(RunCommand)
        )
        record = result.scalar_one_or_none()
        return None if record is None else _command_from_record(record)

    async def complete(
        self,
        command_id: UUID,
        *,
        worker_id: str,
        result: Mapping[str, object] | None = None,
    ) -> CommandEnvelope:
        del result  # The command contract has no unbounded result column.
        now = _utc_now()
        async with self._factory()() as session, session.begin():
            record = await self._locked(command_id, session)
            if record.status == "COMPLETED":
                return _command_from_record(record)
            _require_owned_lease(record, worker_id, now)
            record.status = "COMPLETED"
            record.lease_owner = None
            record.lease_expires_at = None
            record.completed_at = now
            record.error_summary = None
            await session.flush()
            return _command_from_record(record)

    async def fail(
        self,
        command_id: UUID,
        *,
        worker_id: str,
        error: str,
        transient: bool = False,
    ) -> CommandEnvelope:
        bounded_error = _bounded_error(error)
        now = _utc_now()
        async with self._factory()() as session, session.begin():
            record = await self._locked(command_id, session)
            if record.status == "FAILED" and not transient:
                if record.error_summary == bounded_error:
                    return _command_from_record(record)
                raise CommandStateConflict(f"command {command_id} already failed differently")
            _require_owned_lease(record, worker_id, now)
            record.lease_owner = None
            record.lease_expires_at = None
            record.error_summary = bounded_error
            if transient:
                record.status = "PENDING"
                record.completed_at = None
                delay = min(300, 2 ** min(max(record.attempt_count - 1, 0), 8))
                record.available_at = now + timedelta(seconds=delay)
            else:
                record.status = "FAILED"
                record.completed_at = now
            await session.flush()
            return _command_from_record(record)

    async def cancel(self, command_id: UUID, *, worker_id: str, reason: str) -> CommandEnvelope:
        """Cancel a currently leased command under the same lease rules."""

        now = _utc_now()
        async with self._factory()() as session, session.begin():
            record = await self._locked(command_id, session)
            if record.status == "CANCELLED":
                return _command_from_record(record)
            _require_owned_lease(record, worker_id, now)
            record.status = "CANCELLED"
            record.lease_owner = None
            record.lease_expires_at = None
            record.completed_at = now
            record.error_summary = _bounded_error(reason)
            await session.flush()
            return _command_from_record(record)

    async def _locked(self, command_id: UUID, session: AsyncSession) -> RunCommand:
        record = (
            await session.execute(
                select(RunCommand).where(RunCommand.id == command_id).with_for_update()
            )
        ).scalar_one_or_none()
        if record is None:
            raise CommandNotFound(f"command {command_id} was not found")
        return record

    def _factory(self) -> async_sessionmaker[AsyncSession]:
        if self._session_factory is None:
            raise CommandError("command persistence boundary is not configured")
        return self._session_factory


def _validate_worker_and_lease(worker_id: str, lease_seconds: float) -> None:
    if not worker_id or len(worker_id) > 255:
        raise ValueError("worker id must contain 1-255 characters")
    validate_lease_seconds(lease_seconds)


def _require_owned_lease(record: RunCommand, worker_id: str, now: datetime) -> None:
    if (
        record.status != "LEASED"
        or record.lease_owner != worker_id
        or record.lease_expires_at is None
        or record.lease_expires_at <= now
    ):
        raise CommandLeaseError(f"worker {worker_id!r} does not hold command {record.id}")


def _check_enqueue_match(
    expected: CommandEnvelope,
    actual: CommandEnvelope,
    *,
    compare_availability: bool,
) -> None:
    if (
        expected.run_id != actual.run_id
        or expected.command_type != actual.command_type
        or expected.expected_run_version != actual.expected_run_version
        or expected.actor_id != actual.actor_id
        or expected.payload_schema_version != actual.payload_schema_version
        or _canonical_payload(expected.payload) != _canonical_payload(actual.payload)
        or (compare_availability and _utc(expected.available_at) != _utc(actual.available_at))
    ):
        raise IdempotencyConflict(
            f"command idempotency key {expected.idempotency_key!r} has a different request"
        )


def _canonical_payload(value: Mapping[str, object]) -> str:
    import json

    return json.dumps(thaw_payload(value), sort_keys=True, separators=(",", ":"))


def _command_from_record(record: RunCommand) -> CommandEnvelope:
    status = {
        "PENDING": CommandStatus.PENDING,
        "LEASED": CommandStatus.LEASED,
        "COMPLETED": CommandStatus.COMPLETED,
        "FAILED": CommandStatus.FAILED,
        "CANCELLED": CommandStatus.CANCELLED,
    }.get(record.status)
    if status is None:
        raise PersistenceDataError(f"unknown stored command status {record.status!r}")
    if not isinstance(record.payload, Mapping):
        raise PersistenceDataError("stored command payload is not an object")
    try:
        return CommandEnvelope(
            id=record.id,
            run_id=record.run_id,
            command_type=record.command_type,
            idempotency_key=record.idempotency_key,
            payload=record.payload,
            status=status,
            expected_run_version=record.expected_run_version,
            actor_id=record.actor_id,
            payload_schema_version=record.payload_schema_version,
            attempt=record.attempt_count,
            available_at=record.available_at,
            lease_owner=record.lease_owner,
            lease_expires_at=record.lease_expires_at,
            created_at=record.created_at,
            completed_at=record.completed_at,
            error_summary=record.error_summary,
        )
    except (TypeError, ValueError) as error:
        raise PersistenceDataError("stored command is malformed") from error


def _bounded_error(error: str) -> str:
    if not isinstance(error, str):
        error = repr(error)
    return redact_durable_text(error)[:_MAX_ERROR_LENGTH]


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise PersistenceDataError("stored command timestamp is not timezone-aware")
    return value.astimezone(UTC)


def _utc_now() -> datetime:
    return datetime.now(UTC)


__all__ = [
    "CommandError",
    "CommandLeaseError",
    "CommandNotFound",
    "CommandStateConflict",
    "IdempotencyConflict",
    "PostgresCommandRepository",
]
