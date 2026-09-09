"""Durable stopped-delivery evidence for recovery before worker acknowledgement."""

from __future__ import annotations

from uuid import UUID

from forge.application.ports.commands import CommandRecoveryRequired
from forge.application.ports.executions import ExecutionStatus
from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.services.control_settlement import controlled_stop
from forge.domain.command import CommandEnvelope
from forge.domain.event import RunEvent
from forge.domain.run import RunSnapshot, RunState


async def record_suspended_delivery(
    work: UnitOfWork,
    command: CommandEnvelope,
    admitted_run: RunSnapshot,
    step_id: UUID,
    kind: str,
    semantic_attempt: int,
) -> None:
    """Bind a known terminal stage to its exact control stop in the caller's UoW.

    This does not acknowledge a command or commit. The stage finalizer records
    this receipt in the transaction that settles its execution and artifacts.
    """
    kinds = {
        "start_planning": "plan",
        "implement": "implement",
        "remediate": "implement",
        "review": "review",
        "validate": "validate",
    }
    states = {
        "plan": {RunState.PLANNING},
        "implement": {RunState.IMPLEMENTING, RunState.REMEDIATING},
        "review": {RunState.REVIEWING},
        "validate": {RunState.VALIDATING},
    }
    if (
        admitted_run.id != command.run_id
        or kinds.get(command.command_type) != kind
        or admitted_run.state not in states.get(kind, set())
        or type(semantic_attempt) is not int
        or semantic_attempt < 1
        or command.payload.get("semantic_attempt", 1) != semantic_attempt
        or not await controlled_stop(work, command, admitted_run)
    ):
        raise CommandRecoveryRequired("suspended delivery authority is not proven")
    execution_id: UUID | None = None
    output_id: UUID | None = None
    if kind == "validate":
        step = await work.controller_steps.get(command.run_id, step_id)
        if (
            step is None
            or step.status is not ExecutionStatus.CANCELLED
            or step.attempt != semantic_attempt
        ):
            raise CommandRecoveryRequired("suspended controller outcome is not terminal")
        output_id = step.output_artifact_id
    else:
        outcome = await work.executions.get_outcome(command.run_id, kind, semantic_attempt)
        if (
            outcome is None
            or outcome.status is not ExecutionStatus.CANCELLED
            or outcome.step_id != step_id
        ):
            raise CommandRecoveryRequired("suspended agent outcome is not terminal")
        execution_id, output_id = outcome.agent_execution_id, outcome.output_artifact_id
    current = await work.runs.get_for_update(command.run_id)
    events = await work.events.list_after(command.run_id, 0)
    controls = [
        event
        for event in events
        if event.run_version == current.version
        and event.event_type in {"run.paused", "run.cancelled"}
    ]
    if len(controls) != 1:
        raise CommandRecoveryRequired("suspended delivery control event is ambiguous")
    payload = {
        "command_id": str(command.id),
        "command_type": command.command_type,
        "command_payload": dict(command.payload),
        "idempotency_key": command.idempotency_key,
        "delivery_attempt": command.attempt,
        "expected_run_version": command.expected_run_version,
        "admitted_run_version": admitted_run.version,
        "admitted_state": admitted_run.state.value,
        "control_command_id": controls[0].payload["command_id"],
        "step_id": str(step_id),
        "kind": kind,
        "semantic_attempt": semantic_attempt,
        "execution_id": str(execution_id) if execution_id is not None else None,
        "output_artifact_id": str(output_id) if output_id is not None else None,
    }
    receipts = [
        event
        for event in events
        if event.event_type == "delivery.suspended"
        and event.payload.get("command_id") == str(command.id)
    ]
    if receipts:
        if (
            len(receipts) != 1
            or receipts[0].payload != payload
            or receipts[0].run_version != current.version
            or receipts[0].actor_class != "worker"
            or receipts[0].payload_schema_version != 1
        ):
            raise CommandRecoveryRequired("suspended delivery receipt conflicts")
        return
    await work.events.append(
        RunEvent(
            run_id=command.run_id,
            run_version=current.version,
            event_type="delivery.suspended",
            payload=payload,
            actor_class="worker",
        )
    )


__all__ = ["record_suspended_delivery"]
