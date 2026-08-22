"""PostgreSQL repository for authoritative run state."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import null, select
from sqlalchemy.ext.asyncio import AsyncSession

from forge.application.services.state_engine import LEGAL, StateEngine
from forge.domain.event import RunEvent
from forge.domain.run import RunSnapshot, RunState, SuspensionContext, SuspensionKind
from forge.persistence.models import Project, ProjectPolicyVersion, Run, Task

if TYPE_CHECKING:
    from forge.persistence.repositories.events import PostgresEventRepository


_TERMINAL_STATES = frozenset({RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED})


class PersistenceError(RuntimeError):
    """Base class for fail-closed persistence errors."""


class PersistenceDataError(PersistenceError):
    """Persisted data cannot be mapped to the domain model safely."""


class RunNotFound(PersistenceError):
    """The requested run does not exist."""

    def __init__(self, run_id: UUID) -> None:
        self.run_id = run_id
        super().__init__(f"run {run_id} was not found")


class RunCreationError(PersistenceError):
    """A requested run cannot be created without writing partial state."""


class ConcurrencyConflict(PersistenceError):
    """The expected run version is stale."""

    def __init__(self, run_id: UUID, expected_version: int, actual_version: int) -> None:
        self.run_id = run_id
        self.expected_version = expected_version
        self.actual_version = actual_version
        super().__init__(f"run {run_id} has version {actual_version}; expected {expected_version}")


class PostgresRunRepository:
    """Map immutable domain snapshots to the current ``runs`` row."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        state_engine: StateEngine | None = None,
        events: PostgresEventRepository | None = None,
    ) -> None:
        self._session = session
        self._state_engine = state_engine or StateEngine()
        self._events = events

    def bind_events(self, events: PostgresEventRepository) -> None:
        """Bind the event repository sharing this transaction's session."""

        self._events = events

    async def get(self, run_id: UUID) -> RunSnapshot:
        """Load and strictly validate one persisted run snapshot."""

        record = await self._session.get(Run, run_id)
        if record is None:
            raise RunNotFound(run_id)
        return _snapshot_from_record(record)

    async def get_for_update(self, run_id: UUID) -> RunSnapshot:
        """Load one run while holding its row lock for a same-transaction command."""

        record = (
            await self._session.execute(select(Run).where(Run.id == run_id).with_for_update())
        ).scalar_one_or_none()
        if record is None:
            raise RunNotFound(run_id)
        return _snapshot_from_record(record)

    async def list(
        self, *, project_id: UUID | None = None, task_id: UUID | None = None
    ) -> Sequence[RunSnapshot]:
        """List safe run snapshots in deterministic creation order."""

        statement = select(Run).order_by(Run.created_at, Run.id)
        if project_id is not None:
            statement = statement.where(Run.project_id == project_id)
        if task_id is not None:
            statement = statement.where(Run.task_id == task_id)
        result = await self._session.execute(statement)
        return [_snapshot_from_record(record) for record in result.scalars().all()]

    async def create(self, run: RunSnapshot) -> None:
        """Create one new run while snapshotting the project's current policy."""

        _validate_new_snapshot(run)

        with self._session.no_autoflush:
            project_result = await self._session.execute(
                select(Project)
                .where(Project.id == run.project_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            project = project_result.scalar_one_or_none()
            if project is None:
                raise RunCreationError(f"project {run.project_id} was not found")
            current_policy_version = project.current_policy_version
            if current_policy_version is None or current_policy_version < 1:
                raise RunCreationError(f"project {run.project_id} has no current policy version")
            policy_version = (
                current_policy_version if run.policy_version is None else run.policy_version
            )
            if policy_version != current_policy_version:
                raise RunCreationError("new run policy version is not the current project version")

            policy = await self._session.get(ProjectPolicyVersion, (run.project_id, policy_version))
            if policy is None:
                raise RunCreationError(
                    f"current policy {run.project_id}/{policy_version} was not found"
                )

            task = await self._session.get(Task, run.task_id)
            if task is None:
                raise RunCreationError(f"task {run.task_id} was not found")
            if task.project_id != run.project_id:
                raise RunCreationError(
                    f"task {run.task_id} does not belong to project {run.project_id}"
                )

            existing = await self._session.get(Run, run.id)
            if existing is not None:
                raise RunCreationError(f"run {run.id} already exists")

            record = Run(
                id=run.id,
                project_id=run.project_id,
                task_id=run.task_id,
                policy_version=policy_version,
                state=RunState.CREATED.value,
                version=0,
                suspended_state=None,
                suspension_kind=None,
                suspension_context_schema_version=None,
                suspension_context=null(),
                local_remediation_count=0,
                remote_remediation_count=0,
                token_budget=0,
                cost_budget_minor=0,
                duration_budget_seconds=0,
                base_ref=run.base_ref,
                base_sha=run.base_sha,
                branch_name=run.branch_name,
                database_state="DISABLED",
            )
            self._session.add(record)

        await self._session.flush()

    async def transition(
        self,
        run_id: UUID,
        expected_version: int,
        target: RunState,
        event_type: str,
        event_payload: Mapping[str, object],
        *,
        actor_class: str = "system",
        actor_id: UUID | None = None,
        occurred_at: datetime | None = None,
        payload_schema_version: int = 1,
    ) -> RunSnapshot:
        """Apply one locked state transition and append its causal event."""

        statement = select(Run).where(Run.id == run_id).with_for_update()
        result = await self._session.execute(statement)
        record = result.scalar_one_or_none()
        if record is None:
            raise RunNotFound(run_id)

        if record.version != expected_version:
            raise ConcurrencyConflict(run_id, expected_version, record.version)

        current = _snapshot_from_record(record)
        changed = self._state_engine.transition(current, target)
        if self._events is None:
            raise PersistenceError("run repository is not bound to an event repository")
        event = RunEvent(
            run_id=run_id,
            run_version=changed.version,
            event_type=event_type,
            payload=event_payload,
            actor_class=actor_class,
            actor_id=actor_id,
            payload_schema_version=payload_schema_version,
            occurred_at=occurred_at or _utc_now(),
        )
        try:
            _apply_snapshot(record, changed)
            await self._events.append(event)
            await self._session.flush()
        except BaseException:
            # A caller may catch the append failure and still call commit().
            # Roll back immediately so a tracked run update can never commit
            # without its causal event; the UoW remains reusable afterwards.
            await self._session.rollback()
            raise
        return changed


def _validate_new_snapshot(run: RunSnapshot) -> None:
    """Reject any input that is not an untouched new run."""

    if run.state is not RunState.CREATED:
        raise RunCreationError("new runs must start in CREATED")
    if run.version != 0:
        raise RunCreationError("new runs must start at version 0")
    if run.suspended_state is not None or run.suspension_kind is not None:
        raise RunCreationError("new runs cannot carry suspension metadata")
    if run.suspension_context is not None:
        raise RunCreationError("new runs cannot carry a suspension context")
    if run.local_remediation_count != 0 or run.remote_remediation_count != 0:
        raise RunCreationError("new runs must have zero remediation counts")
    if run.policy_version is not None and run.policy_version < 1:
        raise RunCreationError("new run policy version must be positive")
    if (run.base_ref is None) != (run.base_sha is None):
        raise RunCreationError("new run base reference and SHA must be provided together")
    if run.base_ref is not None and not run.base_ref.strip():
        raise RunCreationError("new run base reference must not be blank")
    if run.base_sha is not None and re.fullmatch(r"[0-9a-f]{40}", run.base_sha) is None:
        raise RunCreationError("new run base SHA must be a lowercase commit")
    if run.branch_name is not None and (not run.branch_name.strip() or len(run.branch_name) > 512):
        raise RunCreationError("new run branch name is invalid")


def _snapshot_from_record(record: Run) -> RunSnapshot:
    """Map a run row while rejecting unknown or inconsistent persisted values."""

    state = _run_state(record.state, "state")
    version = _nonnegative_int(record.version, "version")
    local_count = _nonnegative_int(record.local_remediation_count, "local_remediation_count")
    remote_count = _nonnegative_int(record.remote_remediation_count, "remote_remediation_count")
    suspended_state = (
        None
        if record.suspended_state is None
        else _run_state(record.suspended_state, "suspended_state")
    )
    suspension_kind = (
        None if record.suspension_kind is None else _suspension_kind(record.suspension_kind)
    )
    context = _decode_suspension_context(record, state, suspended_state, suspension_kind)
    return RunSnapshot(
        id=record.id,
        project_id=record.project_id,
        task_id=record.task_id,
        policy_version=record.policy_version,
        base_ref=record.base_ref,
        base_sha=record.base_sha,
        branch_name=record.branch_name,
        state=state,
        version=version,
        suspended_state=suspended_state,
        local_remediation_count=local_count,
        remote_remediation_count=remote_count,
        suspension_kind=suspension_kind,
        suspension_context=context,
    )


def _decode_suspension_context(
    record: Run,
    state: RunState,
    suspended_state: RunState | None,
    suspension_kind: SuspensionKind | None,
) -> SuspensionContext | None:
    schema_version = record.suspension_context_schema_version
    raw_context = record.suspension_context

    if state is RunState.PAUSED:
        if (
            suspended_state is None
            or suspension_kind is not SuspensionKind.PAUSE
            or schema_version != 1
            or not isinstance(raw_context, Mapping)
        ):
            raise PersistenceDataError("paused run has malformed suspension metadata")
        if suspended_state in _TERMINAL_STATES or suspended_state is RunState.PAUSED:
            raise PersistenceDataError("paused run has an unsupported suspended state")
        context = _context_from_json(raw_context)
        if context.state is not suspended_state:
            raise PersistenceDataError("paused run context state does not match suspended state")
        if (context.suspended_state is None) != (context.suspension_kind is None):
            raise PersistenceDataError("nested suspension context has inconsistent shape")
        if context.suspended_state is RunState.PAUSED:
            raise PersistenceDataError("suspension context cannot retain PAUSED")
        if context.suspension_kind is SuspensionKind.PAUSE:
            raise PersistenceDataError("nested pause context is not a supported state shape")
        if context.suspension_kind is SuspensionKind.INTERVENTION and (
            context.suspended_state is None
            or context.state is not RunState.AWAITING_HUMAN_INTERVENTION
            or RunState.AWAITING_HUMAN_INTERVENTION
            not in LEGAL.get(context.suspended_state, frozenset())
        ):
            raise PersistenceDataError("nested intervention context is malformed")
        if context.suspension_kind is None and (
            context.state is RunState.AWAITING_HUMAN_INTERVENTION
            or context.state in _TERMINAL_STATES
        ):
            raise PersistenceDataError("nested active context has an unsupported state")
        return context

    if state is RunState.AWAITING_HUMAN_INTERVENTION:
        if (
            suspended_state is None
            or suspension_kind is not SuspensionKind.INTERVENTION
            or schema_version is not None
            or raw_context is not None
            or RunState.AWAITING_HUMAN_INTERVENTION not in LEGAL.get(suspended_state, frozenset())
        ):
            raise PersistenceDataError("intervention run has malformed suspension metadata")
        return None

    if suspended_state is not None or suspension_kind is not None:
        raise PersistenceDataError("non-suspended run has suspension metadata")
    if schema_version is not None or raw_context is not None:
        raise PersistenceDataError("non-suspended run has suspension context")
    return None


def _context_from_json(raw_context: Mapping[str, object]) -> SuspensionContext:
    expected_keys = {"state", "suspended_state", "suspension_kind"}
    if set(raw_context) != expected_keys:
        raise PersistenceDataError("suspension context keys do not match schema version 1")
    raw_state = raw_context["state"]
    raw_suspended_state = raw_context["suspended_state"]
    raw_kind = raw_context["suspension_kind"]
    state = _run_state(raw_state, "suspension_context.state")
    suspended_state = (
        None
        if raw_suspended_state is None
        else _run_state(raw_suspended_state, "suspension_context.suspended_state")
    )
    suspension_kind = None if raw_kind is None else _suspension_kind(raw_kind)
    return SuspensionContext(
        state=state,
        suspended_state=suspended_state,
        suspension_kind=suspension_kind,
    )


def _apply_snapshot(record: Run, snapshot: RunSnapshot) -> None:
    record.state = snapshot.state.value
    record.version = snapshot.version
    record.suspended_state = (
        snapshot.suspended_state.value if snapshot.suspended_state is not None else None
    )
    record.suspension_kind = (
        snapshot.suspension_kind.value if snapshot.suspension_kind is not None else None
    )
    record.local_remediation_count = snapshot.local_remediation_count
    record.remote_remediation_count = snapshot.remote_remediation_count
    if snapshot.suspension_context is None:
        record.suspension_context_schema_version = None
        record.suspension_context = null()
    else:
        record.suspension_context_schema_version = 1
        record.suspension_context = {
            "state": snapshot.suspension_context.state.value,
            "suspended_state": (
                snapshot.suspension_context.suspended_state.value
                if snapshot.suspension_context.suspended_state is not None
                else None
            ),
            "suspension_kind": (
                snapshot.suspension_context.suspension_kind.value
                if snapshot.suspension_context.suspension_kind is not None
                else None
            ),
        }


def _run_state(value: object, field_name: str) -> RunState:
    if not isinstance(value, str):
        raise PersistenceDataError(f"{field_name} is not a run-state string")
    try:
        return RunState(value)
    except ValueError as error:
        raise PersistenceDataError(f"{field_name} contains unknown state {value!r}") from error


def _suspension_kind(value: object) -> SuspensionKind:
    if not isinstance(value, str):
        raise PersistenceDataError("suspension kind is not a string")
    try:
        return SuspensionKind(value)
    except ValueError as error:
        raise PersistenceDataError(f"unknown suspension kind {value!r}") from error


def _nonnegative_int(value: object, field_name: str) -> int:
    if type(value) is not int or value < 0:
        raise PersistenceDataError(f"{field_name} must be a nonnegative integer")
    return value


def _utc_now() -> datetime:
    from datetime import UTC

    return datetime.now(UTC)


__all__ = [
    "ConcurrencyConflict",
    "PersistenceDataError",
    "PersistenceError",
    "PostgresRunRepository",
    "RunCreationError",
    "RunNotFound",
]
