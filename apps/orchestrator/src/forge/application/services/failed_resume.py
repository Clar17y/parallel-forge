"""Fail-closed recovery of a terminal command which failed before admission."""

from __future__ import annotations

from dataclasses import replace
from uuid import UUID, uuid5

from forge.application.ports.commands import CommandLeaseLost, CommandRecoveryRequired
from forge.application.ports.controller_steps import ControllerStepUnsettledError
from forge.application.ports.executions import (
    ExecutionOutcome,
    ExecutionStatus,
    ExecutionUnsettledError,
)
from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.services.resume_stage import resume_stage
from forge.domain.actor import AgentRole
from forge.domain.command import CommandEnvelope, CommandStatus, thaw_payload
from forge.domain.event import RunEvent
from forge.domain.run import RunSnapshot, RunState
from forge.persistence.repositories.commands import CommandNotFound


async def settle_failed_delivery(
    work: UnitOfWork, resume: CommandEnvelope, paused: RunSnapshot
) -> tuple[CommandEnvelope, ...]:
    """Return one immutable failed source only after proving zero admission."""

    _validate_resume(resume)
    try:
        fenced = await work.commands.assert_current_lease(resume)
    except CommandLeaseLost, CommandNotFound:
        raise CommandRecoveryRequired("resume command lease is no longer current") from None
    if not _same_delivery(resume, fenced):
        raise CommandRecoveryRequired("resume command delivery differs from its lease")
    current = await work.runs.get_for_update(resume.run_id)
    if (
        current != paused
        or current.state is not RunState.PAUSED
        or current.version != resume.expected_run_version
        or current.suspended_state is None
    ):
        raise CommandRecoveryRequired("failed delivery requires exact paused run authority")
    pause = await _pause_authority(work, current)
    candidates = await work.commands.list_failed_normal(
        run_id=current.id, exclude_command_id=resume.id
    )
    candidates = [
        candidate
        for candidate in candidates
        if resume_stage(current.suspended_state, candidate.command_type) is not None
        and candidate.expected_run_version == current.version - 1
    ]
    if len(candidates) != 1:
        raise CommandRecoveryRequired("failed delivery source is absent or ambiguous")
    source = candidates[0]
    try:
        kind, attempt = await _validate_unadmitted(work, current, source)
        event_type = "delivery.failed_before_admission"
        receipt_payload = _receipt_payload(source, kind, attempt, pause.id, current.suspended_state)
    except CommandRecoveryRequired:
        kind, attempt, outcome = await _validate_settled(work, current, source)
        event_type = "delivery.failed_after_settlement"
        receipt_payload = {
            **_receipt_payload(source, kind, attempt, pause.id, current.suspended_state),
            "step_id": str(outcome.step_id),
            "execution_id": str(outcome.agent_execution_id),
            "finish_status": outcome.finish_status.value,
            "output_artifact_id": (
                str(outcome.output_artifact_id) if outcome.output_artifact_id is not None else None
            ),
        }
    proof = await work.runs.prove_quiescent(current.id, exclude_command_id=resume.id)
    if not proof.is_quiescent:
        raise CommandRecoveryRequired("paused run has unresolved durable effects")
    receipt = RunEvent(
        run_id=current.id,
        run_version=current.version,
        event_type=event_type,
        actor_class="worker",
        payload=receipt_payload,
    )
    events = await work.events.list_after(current.id, 0)
    if any(
        event.event_type == receipt.event_type
        and event.run_version == current.version
        and event.payload.get("command_id") == str(source.id)
        for event in events
    ):
        raise CommandRecoveryRequired("failed delivery receipt already exists")
    await work.events.append(receipt)
    return (source,)


async def validate_settled_failed_receipt(
    work: UnitOfWork,
    source: CommandEnvelope,
    *,
    paused_version: int,
    pause_id: UUID,
    state: RunState,
) -> None:
    """Require the exact terminal execution and immutable settlement receipt."""
    current = await work.runs.get_for_update(source.run_id)
    if current.version < paused_version or source.expected_run_version != paused_version - 1:
        raise CommandRecoveryRequired("settled continuation paused authority differs")
    paused = replace(current, version=paused_version, suspended_state=state)
    kind, attempt, outcome = await _validate_settled(work, paused, source)
    expected = {
        **_receipt_payload(source, kind, attempt, pause_id, state),
        "step_id": str(outcome.step_id),
        "execution_id": str(outcome.agent_execution_id),
        "finish_status": outcome.finish_status.value,
        "output_artifact_id": (
            str(outcome.output_artifact_id) if outcome.output_artifact_id is not None else None
        ),
    }
    receipts = [
        event
        for event in await work.events.list_for_version(source.run_id, paused_version)
        if event.event_type == "delivery.failed_after_settlement"
        and event.payload.get("command_id") == str(source.id)
    ]
    if (
        len(receipts) != 1
        or receipts[0].actor_class != "worker"
        or receipts[0].actor_id is not None
        or receipts[0].payload_schema_version != 1
        or receipts[0].payload != expected
    ):
        raise CommandRecoveryRequired("settled continuation receipt differs")


async def _validate_settled(
    work: UnitOfWork, paused: RunSnapshot, source: CommandEnvelope
) -> tuple[str, int, ExecutionOutcome]:
    stage = resume_stage(paused.suspended_state, source.command_type)
    attempt = source.payload.get("semantic_attempt", 1)
    if (
        stage is None
        or source.status is not CommandStatus.FAILED
        or source.run_id != paused.id
        or source.command_type != stage[0]
        or source.payload_schema_version != 1
        or source.expected_run_version != paused.version - 1
        or type(attempt) is not int
        or attempt < 1
    ):
        raise CommandRecoveryRequired("settled failed delivery source authority is invalid")
    kind = stage[1]
    if kind == "validate":
        raise CommandRecoveryRequired("settled controller delivery requires recovery")
    namespaces = {
        "plan": UUID("e516bb38-63f8-4c28-b4e4-6cc9b3a9e9c5"),
        "implement": UUID("6649eb62-7e4a-421f-9861-8be14cefa22b"),
        "review": UUID("29a779ce-d141-498d-b6bc-a18bececf766"),
    }
    steps = {
        "plan": UUID("3b7b0e4a-5e6a-4b8d-9db0-3c3c12f1f411"),
        "implement": UUID("5f7bc719-a867-443c-b6a8-936c6663a983"),
        "review": UUID("11c37116-b982-454c-b101-0f7e4bc4e3ed"),
    }
    outcome = await work.executions.get_outcome(paused.id, kind, attempt)
    role = (
        AgentRole.PLANNER
        if kind == "plan"
        else AgentRole.DEVELOPER
        if kind == "implement"
        else AgentRole.REVIEWER
    )
    if (
        outcome is None
        or outcome.status
        not in {ExecutionStatus.SUCCEEDED, ExecutionStatus.FAILED, ExecutionStatus.CANCELLED}
        or outcome.agent_execution_id != uuid5(namespaces[kind], str(source.id))
        or outcome.step_id != uuid5(steps[kind], str(source.id))
        or outcome.role is not role
        or outcome.kind != kind
        or outcome.attempt != attempt
    ):
        raise CommandRecoveryRequired("settled failed delivery proof differs")
    return kind, attempt, outcome


async def validate_failed_receipt(
    work: UnitOfWork,
    source: CommandEnvelope,
    *,
    paused_version: int,
    pause_id: UUID,
    state: RunState,
) -> None:
    """Verify a historical zero-admission receipt without rewriting its source.

    A later continuation may already occupy the same semantic attempt, so this
    checks the immutable receipt rather than treating that fresh admission as
    evidence against the original failed delivery.
    """
    stage = resume_stage(state, source.command_type)
    attempt = source.payload.get("semantic_attempt", 1)
    if (
        stage is None
        or source.command_type != stage[0]
        or source.status is not CommandStatus.FAILED
        or source.attempt < 1
        or source.payload_schema_version != 1
        or source.expected_run_version != paused_version - 1
        or type(attempt) is not int
        or attempt < 1
    ):
        raise CommandRecoveryRequired("failed continuation source is invalid")
    receipts = [
        event
        for event in await work.events.list_for_version(source.run_id, paused_version)
        if event.event_type == "delivery.failed_before_admission"
        and event.payload.get("command_id") == str(source.id)
    ]
    expected = _receipt_payload(source, stage[1], attempt, pause_id, state)
    if (
        len(receipts) != 1
        or receipts[0].actor_class != "worker"
        or receipts[0].actor_id is not None
        or receipts[0].payload_schema_version != 1
        or receipts[0].payload != expected
    ):
        raise CommandRecoveryRequired("failed continuation receipt differs")


def _receipt_payload(
    source: CommandEnvelope, kind: str, attempt: int, pause_id: UUID, state: RunState
) -> dict[str, object]:
    return {
        "command_id": str(source.id),
        "command_type": source.command_type,
        "idempotency_key": source.idempotency_key,
        "command_payload": thaw_payload(source.payload),
        "expected_run_version": source.expected_run_version,
        "actor_id": str(source.actor_id) if source.actor_id is not None else None,
        "payload_schema_version": source.payload_schema_version,
        "delivery_attempt": source.attempt,
        "kind": kind,
        "semantic_attempt": attempt,
        "pause_command_id": str(pause_id),
        "deferred_state": state.value,
    }


async def _validate_unadmitted(
    work: UnitOfWork, paused: RunSnapshot, source: CommandEnvelope
) -> tuple[str, int]:
    stage = resume_stage(paused.suspended_state, source.command_type)
    attempt = source.payload.get("semantic_attempt", 1)
    if (
        stage is None
        or source.run_id != paused.id
        or source.command_type != stage[0]
        or source.status is not CommandStatus.FAILED
        or source.attempt < 1
        or source.payload_schema_version != 1
        or source.expected_run_version != paused.version - 1
        or type(attempt) is not int
        or attempt < 1
    ):
        raise CommandRecoveryRequired("failed delivery source authority is invalid")
    try:
        next_attempt = (
            await work.controller_steps.next_attempt(paused.id, stage[1])
            if stage[1] == "validate"
            else await work.executions.next_attempt(paused.id, stage[1])
        )
    except ControllerStepUnsettledError, ExecutionUnsettledError:
        raise CommandRecoveryRequired("failed delivery admission is unsettled") from None
    if next_attempt != attempt:
        raise CommandRecoveryRequired("failed delivery was already admitted")
    return stage[1], attempt


def _validate_resume(command: CommandEnvelope) -> None:
    if (
        command.command_type != "resume"
        or command.status is not CommandStatus.LEASED
        or command.payload != {}
        or command.payload_schema_version != 1
        or command.actor_id is None
    ):
        raise CommandRecoveryRequired("resume command authority is invalid")


def _same_delivery(command: CommandEnvelope, fenced: CommandEnvelope) -> bool:
    return (
        command.id == fenced.id
        and command.run_id == fenced.run_id
        and command.command_type == fenced.command_type
        and command.idempotency_key == fenced.idempotency_key
        and command.payload == fenced.payload
        and command.expected_run_version == fenced.expected_run_version
        and command.actor_id == fenced.actor_id
        and command.payload_schema_version == fenced.payload_schema_version
        and command.attempt == fenced.attempt
        and command.lease_owner == fenced.lease_owner
        and fenced.status is CommandStatus.LEASED
    )


async def _pause_authority(work: UnitOfWork, run: RunSnapshot) -> CommandEnvelope:
    events = [
        event
        for event in await work.events.list_for_version(run.id, run.version)
        if event.event_type == "run.paused"
    ]
    if len(events) != 1:
        raise CommandRecoveryRequired("paused run has no unique causal pause event")
    event = events[0]
    try:
        pause = await work.commands.get(UUID(str(event.payload.get("command_id"))))
    except CommandNotFound, TypeError, ValueError:
        raise CommandRecoveryRequired("causal pause command is missing") from None
    if (
        event.actor_class != "operator"
        or event.payload_schema_version != 1
        or pause.run_id != run.id
        or pause.command_type != "pause"
        or pause.status is not CommandStatus.COMPLETED
        or pause.actor_id is None
        or pause.actor_id != event.actor_id
        or pause.payload_schema_version != 1
        or pause.payload != {}
        or pause.expected_run_version != run.version - 1
        or event.payload
        != {
            "command_id": str(pause.id),
            "command_type": "pause",
            "command_payload": {},
            "expected_run_version": run.version - 1,
        }
    ):
        raise CommandRecoveryRequired("causal pause command binding is invalid")
    return pause


__all__ = ["settle_failed_delivery", "validate_failed_receipt", "validate_settled_failed_receipt"]
