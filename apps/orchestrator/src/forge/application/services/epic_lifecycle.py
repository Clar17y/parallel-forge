"""Durable epic control intents and child command reconciliation."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from forge.application.services.auth import AuthenticatedActor
from forge.application.services.epic_dispatch import EpicDispatchProjection, EpicDispatchService
from forge.application.services.epic_eligibility import EpicEligibilityService, EpicItemEligibility
from forge.application.services.runs import RunCommandRequest, RunCommandService
from forge.domain.approval import ApprovalGate
from forge.domain.command import CommandStatus
from forge.domain.epic_run_bridge import (
    EpicAttempt,
    EpicExecutionNotFound,
    EpicExecutionSnapshot,
    EpicLaunchConflict,
)
from forge.domain.operation import canonical_digest
from forge.domain.run import RunState
from forge.persistence.models.api import OperatorAuditEvent
from forge.persistence.models.epic_run_bridge import (
    EpicChildBudgetHold,
    EpicControlIntent,
    EpicExecution,
    EpicExecutionControl,
)
from forge.persistence.models.run import Run
from forge.persistence.unit_of_work import PostgresUnitOfWork

Action = Literal["pause", "resume", "cancel"]
_TERMINAL = {RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED}
_REQUESTED = {
    "pause": "PAUSE_REQUESTED",
    "resume": "RESUME_REQUESTED",
    "cancel": "CANCEL_REQUESTED",
}
_SETTLED = {"pause": "PAUSED", "resume": "ACTIVE", "cancel": "CANCELLED"}


class EpicControlReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    execution_id: UUID
    execution_version: int
    action: Action
    state: str
    intent_ids: tuple[UUID, ...]
    blocker_code: str | None = None


class EpicControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    expected_execution_version: int = Field(ge=1, strict=True)
    action: Action


class EpicChildProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    attempt: EpicAttempt
    run_state: RunState
    run_version: int
    pending_gate: ApprovalGate | None
    retained_gate: ApprovalGate | None
    effects_settled: bool


class EpicIntentProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    intent_id: UUID
    control_version: int
    action: str
    run_id: UUID
    command_id: UUID | None
    status: str
    refusal: str | None


class EpicOwnerActionProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    actor_id: UUID
    event_type: str
    warnings: tuple[str, ...]
    note: str | None


class EpicExecutionProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    execution: EpicExecutionSnapshot
    control_version: int | None
    control_state: str | None
    blocker_code: str | None
    children: tuple[EpicChildProjection, ...]
    intents: tuple[EpicIntentProjection, ...]
    owner_actions: tuple[EpicOwnerActionProjection, ...]
    items: tuple[EpicItemEligibility, ...] = ()
    dispatch: EpicDispatchProjection | None = None


class EpicLifecycleService:
    def __init__(
        self,
        unit_of_work_factory: Callable[[], PostgresUnitOfWork],
        *,
        commands: RunCommandService,
        eligibility: EpicEligibilityService | None = None,
        dispatch: EpicDispatchService | None = None,
    ) -> None:
        self._work = unit_of_work_factory
        self._commands = commands
        self._eligibility = eligibility
        self._dispatch = dispatch

    async def list(self, epic_id: UUID) -> tuple[EpicExecutionProjection, ...]:
        async with self._work() as work:
            await work.epics.get(epic_id)
            rows = (
                await work.session.scalars(
                    select(EpicExecution)
                    .where(EpicExecution.epic_id == epic_id)
                    .order_by(EpicExecution.created_at, EpicExecution.id)
                )
            ).all()
            result = tuple([await self._project(work, row) for row in rows])
            await work.commit()
        return result

    async def get(self, epic_id: UUID, execution_id: UUID) -> EpicExecutionProjection:
        async with self._work() as work:
            row = await work.session.get(EpicExecution, execution_id)
            if row is None or row.epic_id != epic_id:
                raise EpicExecutionNotFound("execution was not found")
            result = await self._project(work, row)
            await work.commit()
        return await self._with_readiness(result)

    async def _with_readiness(self, value: EpicExecutionProjection) -> EpicExecutionProjection:
        updates: dict[str, object] = {}
        if self._eligibility is not None:
            updates["items"] = await self._eligibility.readiness(
                epic_id=value.execution.epic_id, execution_id=value.execution.execution_id
            )
        if self._dispatch is not None:
            updates["dispatch"] = await self._dispatch.get(
                value.execution.epic_id, value.execution.execution_id
            )
        return value.model_copy(update=updates) if updates else value

    async def _project(
        self, work: PostgresUnitOfWork, row: EpicExecution
    ) -> EpicExecutionProjection:
        source = await work.epic_run_bridge.get_execution(row.id)
        control = await work.session.get(EpicExecutionControl, row.id)
        attempts = await work.epic_run_bridge.list_attempts(row.epic_id, execution_id=row.id)
        children = []
        for attempt in attempts:
            run = await work.runs.get(attempt.run_id)
            hold = await work.session.get(EpicChildBudgetHold, attempt.attempt_id)
            children.append(
                EpicChildProjection(
                    attempt=attempt,
                    run_state=run.state,
                    run_version=run.version,
                    pending_gate=run.pending_gate,
                    retained_gate=(
                        run.suspension_context.pending_gate
                        if run.suspension_context is not None
                        else None
                    ),
                    effects_settled=hold.effects_settled if hold else False,
                )
            )
        intents = (
            await work.session.scalars(
                select(EpicControlIntent)
                .where(EpicControlIntent.execution_id == row.id)
                .order_by(EpicControlIntent.created_at, EpicControlIntent.id)
            )
        ).all()
        audit = (
            await work.session.scalars(
                select(OperatorAuditEvent)
                .where(
                    OperatorAuditEvent.subject_type == "epic",
                    OperatorAuditEvent.subject_id == row.epic_id,
                    OperatorAuditEvent.event_type.in_(
                        (
                            "epic.execution_started",
                            "epic.execution_control_requested",
                        )
                    ),
                    OperatorAuditEvent.payload["execution_id"].astext == str(row.id),
                )
                .order_by(OperatorAuditEvent.created_at, OperatorAuditEvent.id)
            )
        ).all()
        return EpicExecutionProjection(
            execution=source,
            control_version=control.version if control else None,
            control_state=control.state if control else None,
            blocker_code=control.blocker_code if control else None,
            children=tuple(children),
            intents=tuple(
                EpicIntentProjection(
                    intent_id=value.id,
                    control_version=value.control_version,
                    action=value.action,
                    run_id=value.run_id,
                    command_id=value.command_id,
                    status=value.status,
                    refusal=value.refusal,
                )
                for value in intents
            ),
            owner_actions=tuple(
                EpicOwnerActionProjection(
                    actor_id=value.actor_id,
                    event_type=value.event_type,
                    warnings=tuple(value.payload.get("warnings", ())),
                    note=value.payload.get("override_note"),
                )
                for value in audit
            ),
        )

    async def request(
        self,
        *,
        actor: AuthenticatedActor,
        epic_id: UUID,
        execution_id: UUID,
        idempotency_key: str,
        request: EpicControlRequest,
    ) -> EpicControlReceipt:
        digest = canonical_digest(
            {"execution_id": str(execution_id), "request": request.model_dump(mode="json")}
        )
        async with self._work() as work:
            receipt = await work.mutations.reserve(
                actor_id=actor.actor_id,
                action="epic.execution.control",
                scope=f"execution:{execution_id}",
                idempotency_key=idempotency_key,
                request_digest=digest,
            )
            if receipt.is_replay:
                if receipt.response_payload is None:
                    raise RuntimeError("control receipt has no response")
                result = EpicControlReceipt.model_validate(receipt.response_payload)
                await work.commit()
                return result
            await work.epics.get(epic_id, for_update=True)
            control = await work.session.get(
                EpicExecutionControl, execution_id, with_for_update=True
            )
            if control is None or control.epic_id != epic_id:
                raise EpicExecutionNotFound("execution control was not found")
            if control.version != request.expected_execution_version:
                raise EpicLaunchConflict(
                    ["execution_version_stale"], actual_epic_version=control.version
                )
            if control.state in ("PAUSE_REQUESTED", "RESUME_REQUESTED", "CANCEL_REQUESTED"):
                raise EpicLaunchConflict(["control_pending"], actual_epic_version=control.version)
            if request.action == "pause" and control.state not in ("ACTIVE", "BLOCKED"):
                raise EpicLaunchConflict(
                    ["pause_state_invalid"], actual_epic_version=control.version
                )
            if request.action == "resume" and control.state not in ("PAUSED", "BLOCKED"):
                raise EpicLaunchConflict(
                    ["resume_state_invalid"], actual_epic_version=control.version
                )
            if request.action == "cancel" and control.state in ("SUCCEEDED", "CANCELLED"):
                raise EpicLaunchConflict(
                    ["cancel_state_invalid"], actual_epic_version=control.version
                )
            attempts = await work.epic_run_bridge.list_attempts(epic_id, execution_id=execution_id)
            live = []
            observing = []
            historical = []
            already_settled = []
            for attempt in attempts:
                run = await work.runs.get_for_update(attempt.run_id)
                if run.state in _TERMINAL:
                    if (await work.runs.prove_quiescent(run.id)).is_quiescent:
                        historical.append(run)
                        hold = await work.session.get(EpicChildBudgetHold, attempt.attempt_id)
                        if hold is not None:
                            hold.effects_settled = True
                    else:
                        observing.append(run.id)
                    continue
                if request.action == "resume":
                    if run.state is RunState.PAUSED:
                        live.append(run)
                    else:
                        already_settled.append(run)
                elif request.action == "pause" and run.state is RunState.PAUSED:
                    if not (await work.runs.prove_quiescent(attempt.run_id)).is_quiescent:
                        observing.append(attempt.run_id)
                    else:
                        already_settled.append(run)
                else:
                    live.append(run)
            control.version += 1
            control.state = (
                _REQUESTED[request.action] if live or observing else _SETTLED[request.action]
            )
            control.blocker_code = None
            intent_ids = []
            for run in live:
                intent = EpicControlIntent(
                    id=uuid4(),
                    execution_id=execution_id,
                    control_version=control.version,
                    actor_id=actor.actor_id,
                    session_id=actor.session_id,
                    action=request.action,
                    run_id=run.id,
                    expected_run_version=run.version,
                    status="requested",
                )
                work.session.add(intent)
                intent_ids.append(intent.id)
            for run_id in observing:
                intent = EpicControlIntent(
                    id=uuid4(),
                    execution_id=execution_id,
                    control_version=control.version,
                    actor_id=actor.actor_id,
                    session_id=actor.session_id,
                    action=request.action,
                    run_id=run_id,
                    expected_run_version=0,
                    status="observing",
                )
                work.session.add(intent)
                intent_ids.append(intent.id)
            # Persist the request-time participant set. A completed predecessor
            # remains historical; a later owner-admitted child has no marker and
            # cannot make an aggregate control look settled by coincidence.
            for run in historical + already_settled:
                work.session.add(EpicControlIntent(
                    id=uuid4(), execution_id=execution_id, control_version=control.version,
                    actor_id=actor.actor_id, session_id=actor.session_id,
                    action=request.action, run_id=run.id,
                    expected_run_version=run.version,
                    status="historical" if run in historical else "settled",
                ))
            await work.session.flush()
            result = EpicControlReceipt(
                execution_id=execution_id,
                execution_version=control.version,
                action=request.action,
                state=control.state,
                intent_ids=tuple(intent_ids),
            )
            await work.audit.append(
                actor_id=actor.actor_id,
                event_type="epic.execution_control_requested",
                subject_type="epic",
                subject_id=epic_id,
                correlation_id=receipt.id,
                payload={
                    "execution_id": str(execution_id),
                    "action": request.action,
                    "control_version": control.version,
                    "intent_ids": [str(value) for value in intent_ids],
                },
            )
            await work.mutations.complete(
                receipt.id,
                response_status=202 if intent_ids else 200,
                response_payload=result.model_dump(mode="json"),
                resource_kind="epic_execution",
                resource_id=execution_id,
            )
            await work.commit()
            return result

    async def reconcile_one(self) -> UUID | None:
        """Advance one durable intent; safe to call after any worker restart."""
        async with self._work() as work:
            identities = tuple(
                (
                    await work.session.scalars(
                        select(EpicControlIntent.id)
                        .where(EpicControlIntent.status.in_(("requested", "enqueued", "observing")))
                        .order_by(
                            EpicControlIntent.checked_at.nullsfirst(),
                            EpicControlIntent.created_at,
                            EpicControlIntent.id,
                        )
                        .limit(64)
                    )
                ).all()
            )
            await work.commit()
        for identity in identities:
            if await self._reconcile_intent(identity):
                return identity
        return None

    async def reconcile_one_hold(self) -> UUID | None:
        """Release one terminal child floor only after run/effect quiescence proof."""
        async with self._work() as work:
            ids = tuple(
                (
                    await work.session.scalars(
                        select(EpicChildBudgetHold.attempt_id)
                        .join(Run, Run.id == EpicChildBudgetHold.run_id)
                        .where(
                            EpicChildBudgetHold.effects_settled.is_(False),
                            Run.state.in_(tuple(state.value for state in _TERMINAL)),
                        )
                        .order_by(
                            EpicChildBudgetHold.checked_at.nullsfirst(),
                            EpicChildBudgetHold.attempt_id,
                        )
                        .limit(32)
                    )
                ).all()
            )
            await work.commit()
        for attempt_id in ids:
            async with self._work() as work:
                hold = await work.session.get(EpicChildBudgetHold, attempt_id)
                if hold is None or hold.effects_settled:
                    await work.commit()
                    continue
                await work.epics.get(hold.epic_id, for_update=True)
                hold = await work.session.get(EpicChildBudgetHold, attempt_id, with_for_update=True)
                if hold is None or hold.effects_settled:
                    await work.commit()
                    continue
                run = await work.runs.get_for_update(hold.run_id)
                hold.checked_at = datetime.now(UTC)
                if (
                    run.state in _TERMINAL
                    and (await work.runs.prove_quiescent(hold.run_id)).is_quiescent
                ):
                    hold.effects_settled = True
                    await work.commit()
                    return attempt_id
                await work.commit()
        return None

    async def _reconcile_intent(self, identity: UUID) -> bool:
        async with self._work() as work:
            intent = await work.session.get(EpicControlIntent, identity)
            if intent is None or intent.status not in ("requested", "enqueued", "observing"):
                await work.commit()
                return False
            if intent.status == "requested":
                control = await work.session.get(EpicExecutionControl, intent.execution_id)
                if control is None:
                    raise EpicExecutionNotFound("execution control was not found")
                # Keep the immutable epic source fence through run-command
                # validation and insertion. Supersession cannot commit between
                # the version check and the durable effect.
                await work.epics.get(control.epic_id, for_update=True)
                control = await work.session.get(
                    EpicExecutionControl,
                    intent.execution_id,
                    with_for_update=True,
                    populate_existing=True,
                )
                intent = await work.session.get(
                    EpicControlIntent, identity, with_for_update=True, populate_existing=True
                )
                if intent is None or intent.status != "requested":
                    await work.commit()
                    return False
                if control is None or control.version != intent.control_version:
                    intent.status = "superseded"
                    await work.commit()
                    return True
                intent.checked_at = datetime.now(UTC)
                try:
                    command = await self._commands.enqueue_in_work(
                        work=work,
                        actor=AuthenticatedActor(
                            actor_id=intent.actor_id,
                            actor_class="operator",
                            session_id=intent.session_id,
                        ),
                        run_id=intent.run_id,
                        idempotency_key=f"epic-control:{identity}",
                        request=RunCommandRequest(
                            command_type=intent.action,
                            expected_run_version=intent.expected_run_version,
                        ),
                    )
                except (ValueError, RuntimeError) as error:
                    intent.status = "refused"
                    intent.refusal = str(error)[:512]
                    control.state = "BLOCKED"
                    control.blocker_code = "child_control_refused"
                    await work.commit()
                    return True
                intent.command_id = command.id
                intent.status = "enqueued"
                await work.commit()
                return True
            action = intent.action
            run_id = intent.run_id
            command_id = intent.command_id
            observing = intent.status == "observing"
            intent.checked_at = datetime.now(UTC)
            await work.commit()
        if observing:
            async with self._work() as work:
                run = await work.runs.get_for_update(run_id)
                settled = (
                    run.state in _TERMINAL
                    or
                    (action == "cancel" and run.state is RunState.CANCELLED)
                    or (action == "pause" and run.state is RunState.PAUSED)
                ) and (await work.runs.prove_quiescent(run_id)).is_quiescent
                await work.commit()
            if settled:
                return await self._settle_intent(identity)
            return False
        if command_id is None:
            await self._record_refusal(identity, "child_command_missing")
            return True
        async with self._work() as work:
            command = await work.commands.get(command_id)
            if command.status in (CommandStatus.PENDING, CommandStatus.LEASED):
                await work.commit()
                return False
            if command.status is not CommandStatus.COMPLETED:
                refusal = command.error_summary or "child_command_failed"
                await work.commit()
                await self._record_refusal(identity, refusal)
                return True
            await work.commit()
        return await self._settle_intent(identity)

    async def _settle_intent(self, identity: UUID) -> bool:
        async with self._work() as work:
            row = await work.session.get(EpicControlIntent, identity)
            if row is None:
                await work.commit()
                return False
            control = await work.session.get(EpicExecutionControl, row.execution_id)
            if control is None:
                raise EpicExecutionNotFound("execution control was not found")
            await work.epics.get(control.epic_id, for_update=True)
            control = await work.session.get(
                EpicExecutionControl,
                row.execution_id,
                with_for_update=True,
                populate_existing=True,
            )
            if control is None:
                raise EpicExecutionNotFound("execution control was not found")
            row = await work.session.get(
                EpicControlIntent, identity, with_for_update=True, populate_existing=True
            )
            if row is None or row.status not in ("enqueued", "observing"):
                await work.commit()
                return False
            if control.version != row.control_version:
                row.status = "superseded"
                await work.commit()
                return True
            if control.version == row.control_version and row.status in ("enqueued", "observing"):
                if row.status == "enqueued":
                    command = (
                        await work.commands.get(row.command_id)
                        if row.command_id is not None
                        else None
                    )
                    if command is None or command.status is not CommandStatus.COMPLETED:
                        await work.commit()
                        return False
                # A completed command is durable acknowledgement. Current run
                # state can subsequently change; final aggregate validation
                # below evaluates that separately for every bound child.
                current = await work.runs.get_for_update(row.run_id)
                correct_state = (
                    (
                        row.status == "enqueued"
                        and row.action in ("pause", "resume", "cancel")
                        and current.version > row.expected_run_version
                    )
                    or (row.action == "pause" and current.state is RunState.PAUSED)
                    or (row.action == "cancel" and current.state is RunState.CANCELLED)
                    or (row.status == "observing" and current.state in _TERMINAL)
                )
                if row.action == "cancel" and current.state is RunState.CANCELLED and correct_state:
                    correct_state = (await work.runs.prove_quiescent(row.run_id)).is_quiescent
                if row.action == "pause" and current.state is RunState.PAUSED and correct_state:
                    correct_state = (await work.runs.prove_quiescent(row.run_id)).is_quiescent
                if row.status == "observing" and current.state in _TERMINAL and correct_state:
                    correct_state = (await work.runs.prove_quiescent(row.run_id)).is_quiescent
                if not correct_state:
                    await work.commit()
                    return False
                row.status = "historical" if row.status == "observing" and current.state in _TERMINAL else "settled"
                if current.state in _TERMINAL and (
                    await work.runs.prove_quiescent(row.run_id)
                ).is_quiescent:
                    hold = await work.session.scalar(
                        select(EpicChildBudgetHold).where(EpicChildBudgetHold.run_id == row.run_id)
                    )
                    if hold is not None:
                        hold.effects_settled = True
                pending = await work.session.scalar(
                    select(EpicControlIntent.id)
                    .where(
                        EpicControlIntent.execution_id == row.execution_id,
                        EpicControlIntent.control_version == row.control_version,
                        EpicControlIntent.id != row.id,
                        EpicControlIntent.status.not_in(("settled", "historical")),
                    )
                    .limit(1)
                )
                if pending is None:
                    bound = await work.epic_run_bridge.list_attempts(
                        control.epic_id, execution_id=row.execution_id
                    )
                    participants = {
                        intent.run_id: intent for intent in (
                            await work.session.scalars(
                                select(EpicControlIntent).where(
                                    EpicControlIntent.execution_id == row.execution_id,
                                    EpicControlIntent.control_version == row.control_version,
                                )
                            )
                        ).all()
                    }
                    uncontrolled = False
                    for attempt in bound:
                        participant = participants.get(attempt.run_id)
                        if participant is None or participant.status not in ("settled", "historical"):
                            uncontrolled = True
                            break
                        sibling = await work.runs.get_for_update(attempt.run_id)
                        expected_state = (
                            sibling.state in _TERMINAL
                            if participant.status == "historical"
                            else (
                                (row.action == "pause" and sibling.state is RunState.PAUSED)
                                or (
                                    row.action == "resume"
                                    and sibling.state not in _TERMINAL
                                    and sibling.state is not RunState.PAUSED
                                )
                                or (row.action == "cancel" and sibling.state is RunState.CANCELLED)
                            )
                        )
                        if not expected_state:
                            uncontrolled = True
                            break
                        if (
                            (participant.status == "historical" or row.action != "resume")
                            and not (await work.runs.prove_quiescent(attempt.run_id)).is_quiescent
                        ):
                            uncontrolled = True
                            break
                    control.state = "BLOCKED" if uncontrolled else _SETTLED[row.action]
                    control.blocker_code = "uncontrolled_child" if uncontrolled else None
            await work.commit()
            return True

    async def _record_refusal(self, intent_id: UUID, reason: str) -> None:
        async with self._work() as work:
            row = await work.session.get(EpicControlIntent, intent_id)
            if row is None:
                return
            control = await work.session.get(EpicExecutionControl, row.execution_id)
            if control is None:
                return
            await work.epics.get(control.epic_id, for_update=True)
            control = await work.session.get(
                EpicExecutionControl,
                row.execution_id,
                with_for_update=True,
                populate_existing=True,
            )
            if control is None:
                return
            row = await work.session.get(
                EpicControlIntent, intent_id, with_for_update=True, populate_existing=True
            )
            if row is None:
                return
            if row.status in ("requested", "enqueued"):
                row.status = "refused"
                row.refusal = reason
                if control.version == row.control_version:
                    control.state = "BLOCKED"
                    control.blocker_code = "child_control_refused"
            await work.commit()
