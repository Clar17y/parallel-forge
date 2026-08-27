"""PostgreSQL repository for authoritative run state."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import null, select
from sqlalchemy.ext.asyncio import AsyncSession

from forge.application.services.state_engine import LEGAL, StateEngine
from forge.domain.approval import ApprovalGate
from forge.domain.event import RunEvent
from forge.domain.resource import ResourceState
from forge.domain.run import RunSnapshot, RunState, SuspensionContext, SuspensionKind
from forge.persistence.models import Project, ProjectPolicyVersion, Run, Task

if TYPE_CHECKING:
    from forge.persistence.repositories.events import PostgresEventRepository


_TERMINAL_STATES = frozenset({RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED})
_APPROVAL_GATE_BY_STATE = {
    RunState.AWAITING_PLAN_APPROVAL: ApprovalGate.PLAN,
    RunState.AWAITING_PR_APPROVAL: ApprovalGate.PR,
    RunState.AWAITING_MERGE_APPROVAL: ApprovalGate.MERGE,
}
_APPROVAL_DIGEST = re.compile(r"\A[0-9a-f]{64}\Z", re.ASCII)


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
                pending_gate=None,
                pending_evidence_digest=None,
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

        return await self._change_state(
            run_id,
            expected_version,
            lambda current: self._state_engine.transition(current, target),
            event_type,
            event_payload,
            actor_class=actor_class,
            actor_id=actor_id,
            occurred_at=occurred_at,
            payload_schema_version=payload_schema_version,
        )

    async def await_approval(
        self,
        run_id: UUID,
        expected_version: int,
        gate: ApprovalGate,
        evidence_digest: str,
        event_type: str,
        event_payload: Mapping[str, object],
        *,
        actor_class: str = "system",
        actor_id: UUID | None = None,
        occurred_at: datetime | None = None,
        payload_schema_version: int = 1,
    ) -> RunSnapshot:
        """Enter an evidence-bound approval gate in one locked transition."""

        return await self._change_state(
            run_id,
            expected_version,
            lambda current: self._state_engine.await_approval(current, gate, evidence_digest),
            event_type,
            event_payload,
            actor_class=actor_class,
            actor_id=actor_id,
            occurred_at=occurred_at,
            payload_schema_version=payload_schema_version,
        )

    async def intervene(
        self,
        run_id: UUID,
        expected_version: int,
        event_type: str,
        event_payload: Mapping[str, object],
        *,
        actor_class: str = "system",
        actor_id: UUID | None = None,
        occurred_at: datetime | None = None,
        payload_schema_version: int = 1,
    ) -> RunSnapshot:
        """Enter human intervention using the state engine's legal graph."""

        return await self._change_state(
            run_id,
            expected_version,
            self._state_engine.intervene,
            event_type,
            event_payload,
            actor_class=actor_class,
            actor_id=actor_id,
            occurred_at=occurred_at,
            payload_schema_version=payload_schema_version,
        )

    async def _change_state(
        self,
        run_id: UUID,
        expected_version: int,
        change: Callable[[RunSnapshot], RunSnapshot],
        event_type: str,
        event_payload: Mapping[str, object],
        *,
        actor_class: str,
        actor_id: UUID | None,
        occurred_at: datetime | None,
        payload_schema_version: int,
    ) -> RunSnapshot:
        """Apply one locked state operation and its causal event."""

        statement = select(Run).where(Run.id == run_id).with_for_update()
        result = await self._session.execute(statement)
        record = result.scalar_one_or_none()
        if record is None:
            raise RunNotFound(run_id)

        if record.version != expected_version:
            raise ConcurrencyConflict(run_id, expected_version, record.version)

        changed = change(_snapshot_from_record(record))
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
            # Roll back immediately so state cannot commit without its event.
            await self._session.rollback()
            raise
        return changed

    async def update_resource(
        self,
        run_id: UUID,
        expected_version: int,
        *,
        worktree_path: str | None = None,
        database_state: ResourceState,
        database_name: str | None = None,
        database_role: str | None = None,
        secret_id: str | None = None,
        event_type: str,
        event_payload: Mapping[str, object],
        actor_class: str = "system",
        actor_id: UUID | None = None,
        occurred_at: datetime | None = None,
        payload_schema_version: int = 1,
    ) -> RunSnapshot:
        """Apply one locked optimistic resource update and causal event."""

        statement = select(Run).where(Run.id == run_id).with_for_update()
        result = await self._session.execute(statement)
        record = result.scalar_one_or_none()
        if record is None:
            raise RunNotFound(run_id)

        if record.version != expected_version:
            raise ConcurrencyConflict(run_id, expected_version, record.version)

        current = _snapshot_from_record(record)
        changed = current.with_resource(
            worktree_path=worktree_path,
            database_state=database_state,
            database_name=database_name,
            database_role=database_role,
            secret_id=secret_id,
        )
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
            # A caller may catch an append failure and still call commit();
            # roll back immediately so state cannot commit without its event.
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
    if run.worktree_path is not None:
        raise RunCreationError("new runs cannot carry a worktree path")
    if run.database_state is not ResourceState.DISABLED or any(
        value is not None for value in (run.database_name, run.database_role, run.secret_id)
    ):
        raise RunCreationError("new runs must have a disabled database resource")
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
    pending_gate = _pending_gate(record.pending_gate)
    pending_evidence_digest = _pending_evidence_digest(record.pending_evidence_digest)
    _validate_pending_shape(state, pending_gate, pending_evidence_digest)
    context = _decode_suspension_context(
        record,
        state,
        suspended_state,
        suspension_kind,
    )
    resource_state = _resource_state(record.database_state)
    try:
        return RunSnapshot(
            id=record.id,
            project_id=record.project_id,
            task_id=record.task_id,
            policy_version=record.policy_version,
            base_ref=record.base_ref,
            base_sha=record.base_sha,
            branch_name=record.branch_name,
            worktree_path=record.worktree_path,
            database_state=resource_state,
            database_name=record.database_name,
            database_role=record.database_role,
            secret_id=record.secret_id,
            state=state,
            version=version,
            suspended_state=suspended_state,
            local_remediation_count=local_count,
            remote_remediation_count=remote_count,
            suspension_kind=suspension_kind,
            suspension_context=context,
            pending_gate=pending_gate,
            pending_evidence_digest=pending_evidence_digest,
        )
    except (TypeError, ValueError) as error:
        raise PersistenceDataError(f"run fields are malformed: {error}") from error


def _decode_suspension_context(
    record: Run,
    state: RunState,
    suspended_state: RunState | None,
    suspension_kind: SuspensionKind | None,
) -> SuspensionContext | None:
    schema_version = record.suspension_context_schema_version
    raw_context = record.suspension_context

    if state is RunState.PAUSED:
        if record.pending_gate is not None or record.pending_evidence_digest is not None:
            raise PersistenceDataError("paused run cannot have top-level approval metadata")
        if (
            suspended_state is None
            or suspension_kind is not SuspensionKind.PAUSE
            or schema_version not in {1, 2}
            or not isinstance(raw_context, Mapping)
        ):
            raise PersistenceDataError("paused run has malformed suspension metadata")
        if suspended_state in _TERMINAL_STATES or suspended_state is RunState.PAUSED:
            raise PersistenceDataError("paused run has an unsupported suspended state")
        context = _context_from_json(raw_context, schema_version)
        if context.state is not suspended_state:
            raise PersistenceDataError("paused run context state does not match suspended state")
        has_pending = (
            context.pending_gate is not None or context.pending_evidence_digest is not None
        )
        if schema_version == 1 and has_pending:
            raise PersistenceDataError(
                "version-1 suspension context cannot contain approval metadata"
            )
        if schema_version == 2 and not has_pending:
            raise PersistenceDataError("version-2 suspension context requires approval metadata")
        if (context.suspended_state is None) != (context.suspension_kind is None):
            raise PersistenceDataError("nested suspension context has inconsistent shape")
        if context.suspended_state is RunState.PAUSED:
            raise PersistenceDataError("suspension context cannot retain PAUSED")
        if context.suspension_kind is SuspensionKind.PAUSE:
            raise PersistenceDataError("nested pause context is not a supported state shape")
        if context.state in _APPROVAL_GATE_BY_STATE and (
            context.suspended_state is not None or context.suspension_kind is not None
        ):
            raise PersistenceDataError("approval suspension context has nested suspension metadata")
        if context.state not in _APPROVAL_GATE_BY_STATE and has_pending:
            raise PersistenceDataError("non-approval suspension context has approval metadata")
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


def _context_from_json(raw_context: Mapping[str, object], schema_version: int) -> SuspensionContext:
    base_keys = {"state", "suspended_state", "suspension_kind"}
    expected_keys = (
        base_keys
        if schema_version == 1
        else {
            *base_keys,
            "pending_gate",
            "pending_evidence_digest",
        }
    )
    if set(raw_context) != expected_keys:
        raise PersistenceDataError(
            f"suspension context keys do not match schema version {schema_version}"
        )
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
    pending_gate = None if schema_version == 1 else _pending_gate(raw_context["pending_gate"])
    pending_digest = (
        None
        if schema_version == 1
        else _pending_evidence_digest(raw_context["pending_evidence_digest"])
    )
    try:
        return SuspensionContext(
            state=state,
            suspended_state=suspended_state,
            suspension_kind=suspension_kind,
            pending_gate=pending_gate,
            pending_evidence_digest=pending_digest,
        )
    except (TypeError, ValueError) as error:
        raise PersistenceDataError("suspension context approval metadata is malformed") from error


def _apply_snapshot(record: Run, snapshot: RunSnapshot) -> None:
    record.state = snapshot.state.value
    record.version = snapshot.version
    record.suspended_state = (
        snapshot.suspended_state.value if snapshot.suspended_state is not None else None
    )
    record.suspension_kind = (
        snapshot.suspension_kind.value if snapshot.suspension_kind is not None else None
    )
    record.pending_gate = snapshot.pending_gate.value if snapshot.pending_gate is not None else None
    record.pending_evidence_digest = snapshot.pending_evidence_digest
    record.local_remediation_count = snapshot.local_remediation_count
    record.remote_remediation_count = snapshot.remote_remediation_count
    record.worktree_path = snapshot.worktree_path
    record.database_state = snapshot.database_state.value
    record.database_name = snapshot.database_name
    record.database_role = snapshot.database_role
    record.secret_id = snapshot.secret_id
    if snapshot.suspension_context is None:
        record.suspension_context_schema_version = None
        record.suspension_context = null()
    else:
        context = snapshot.suspension_context
        has_pending = (
            context.pending_gate is not None or context.pending_evidence_digest is not None
        )
        record.suspension_context_schema_version = 2 if has_pending else 1
        record.suspension_context = {
            "state": context.state.value,
            "suspended_state": (
                context.suspended_state.value if context.suspended_state is not None else None
            ),
            "suspension_kind": (
                context.suspension_kind.value if context.suspension_kind is not None else None
            ),
        }
        if has_pending:
            record.suspension_context["pending_gate"] = (
                context.pending_gate.value if context.pending_gate is not None else None
            )
            record.suspension_context["pending_evidence_digest"] = context.pending_evidence_digest


def _pending_gate(value: object) -> ApprovalGate | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise PersistenceDataError("pending gate is not a string")
    try:
        return ApprovalGate(value)
    except ValueError as error:
        raise PersistenceDataError("pending gate is unknown") from error


def _pending_evidence_digest(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _APPROVAL_DIGEST.fullmatch(value) is None:
        raise PersistenceDataError("pending evidence digest is not a lowercase SHA-256")
    return value


def _validate_pending_shape(
    state: RunState,
    pending_gate: ApprovalGate | None,
    pending_evidence_digest: str | None,
) -> None:
    expected_gate = _APPROVAL_GATE_BY_STATE.get(state)
    if expected_gate is None:
        if pending_gate is not None or pending_evidence_digest is not None:
            raise PersistenceDataError("non-approval run has pending approval metadata")
        return
    if pending_gate is not expected_gate or pending_evidence_digest is None:
        raise PersistenceDataError("approval run has mismatched pending approval metadata")


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


def _resource_state(value: object) -> ResourceState:
    if not isinstance(value, str):
        raise PersistenceDataError("database state is not a resource-state string")
    try:
        return ResourceState(value)
    except ValueError as error:
        raise PersistenceDataError(f"database_state contains unknown state {value!r}") from error


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
