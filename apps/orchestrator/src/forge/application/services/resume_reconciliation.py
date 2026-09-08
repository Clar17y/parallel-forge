"""Fail-closed settlement of stopped normal deliveries before a paused run resumes."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from uuid import UUID, uuid5

from forge.application.ports.artifacts import ArtifactStore
from forge.application.ports.commands import CommandLeaseLost, CommandRecoveryRequired
from forge.application.ports.controller_steps import ControllerStepUnsettledError
from forge.application.ports.executions import ExecutionStatus, ExecutionUnsettledError
from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.services.deferred_delivery import settle_deferred_delivery
from forge.application.services.published_resume import published_stage
from forge.application.services.retained_outcome import load_retained_outcome
from forge.application.services.retained_validation import cancel_retained_validation
from forge.application.services.state_engine import StateEngine
from forge.domain.actor import AgentRole
from forge.domain.agent import AgentFinishStatus
from forge.domain.command import CommandEnvelope, CommandStatus
from forge.domain.event import RunEvent
from forge.domain.run import RunSnapshot, RunState
from forge.persistence.repositories.commands import CommandNotFound

_KIND_BY_COMMAND = {
    "start_planning": "plan",
    "implement": "implement",
    "remediate": "implement",
    "review": "review",
    "validate": "validate",
}
_ADMITTED_STATES = {
    "plan": {RunState.PLANNING},
    "implement": {RunState.IMPLEMENTING, RunState.REMEDIATING},
    "review": {RunState.REVIEWING},
    "validate": {RunState.VALIDATING},
}
_EXECUTION_NAMESPACE = {
    "plan": UUID("e516bb38-63f8-4c28-b4e4-6cc9b3a9e9c5"),
    "implement": UUID("6649eb62-7e4a-421f-9861-8be14cefa22b"),
    "review": UUID("29a779ce-d141-498d-b6bc-a18bececf766"),
}


class ResumeReconciler:
    """Settle stopped deliveries using retained outcomes or verified stop receipts.

    The caller owns the exact resume lease and holds the paused run lock.  This
    service deliberately does not resume a run, invoke a provider, or enqueue
    successor work; its returned sources are for the caller's later phase flow.
    """

    def __init__(self, artifact_store: ArtifactStore | None = None) -> None:
        self._store = artifact_store

    async def reconcile(
        self, work: UnitOfWork, resume_command: CommandEnvelope
    ) -> tuple[CommandEnvelope, ...]:
        _validate_resume(resume_command)
        try:
            fenced = await work.commands.assert_current_lease(resume_command)
        except CommandLeaseLost, CommandNotFound:
            raise CommandRecoveryRequired("resume command lease is no longer current") from None
        if not _same_delivery(resume_command, fenced):
            raise CommandRecoveryRequired("resume command delivery differs from its lease")

        run = await work.runs.get_for_update(resume_command.run_id)
        if run.state is not RunState.PAUSED or run.version != resume_command.expected_run_version:
            raise CommandRecoveryRequired(
                "resume reconciliation requires the exact current paused run"
            )
        pause = await _pause_authority(work, run)
        events = await work.events.list_after(run.id, 0)
        receipts = [
            event
            for event in events
            if event.event_type in {"delivery.suspended", "delivery.deferred"}
            and event.run_version == run.version
        ]
        outstanding = await work.commands.list_outstanding_normal(
            run_id=run.id, exclude_command_id=resume_command.id
        )
        if not outstanding and not receipts:
            proof = await work.runs.prove_quiescent(run.id, exclude_command_id=resume_command.id)
            if not proof.is_quiescent:
                raise CommandRecoveryRequired("paused run has unresolved durable effects")
            return ()

        settled: list[CommandEnvelope] = []
        receipt_by_command: dict[UUID, RunEvent] = {}
        for receipt in receipts:
            source_id = _receipt_command_id(receipt)
            if source_id in receipt_by_command:
                raise CommandRecoveryRequired("suspended delivery receipt is ambiguous")
            receipt_by_command[source_id] = receipt

        for source in outstanding:
            if source.status is CommandStatus.PENDING:
                settled.append(await settle_deferred_delivery(work, resume_command, source))
                continue
            if source.status is not CommandStatus.LEASED:
                raise CommandRecoveryRequired("unsupported paused-run command state")
            source_receipt = receipt_by_command.get(source.id)
            if source_receipt is None:
                recovered_source = await self._settle_retained(work, run, pause, source)
                events = await work.events.list_after(run.id, 0)
                source_receipt = next(
                    (
                        event
                        for event in events
                        if event.event_type == "delivery.suspended"
                        and event.payload.get("command_id") == str(source.id)
                    ),
                    None,
                )
                if source_receipt is None:
                    raise CommandRecoveryRequired("retained outcome has no suspended receipt")
                await _validate_receipt(work, run, pause, source_receipt, source, self._store)
                settled.append(recovered_source)
                continue
            await _validate_receipt(work, run, pause, source_receipt, source, self._store)
            # The conditional update uses PostgreSQL time and the entire
            # observed lease identity, so a renewal/reclaim races safely fail.
            cancelled = await work.commands.cancel_expired_observed_lease(
                source, reason="stopped delivery settled during resume reconciliation"
            )
            if cancelled is None:
                raise CommandRecoveryRequired("stopped delivery lease changed before settlement")
            settled.append(cancelled)

        # Completed stopped deliveries are already acknowledged.  They can be
        # returned for continuation only when their receipt remains complete.
        for source_id, receipt in receipt_by_command.items():
            if source_id in {command.id for command in outstanding}:
                continue
            try:
                source = await work.commands.get(source_id)
            except CommandNotFound:
                raise CommandRecoveryRequired(
                    "suspended delivery source command is missing"
                ) from None
            if source.status is CommandStatus.COMPLETED:
                await _validate_receipt(work, run, pause, receipt, source, self._store)
                settled.append(source)
            elif source.status is CommandStatus.CANCELLED:
                # A prior reconciliation has already settled this receipt.
                await _validate_receipt(work, run, pause, receipt, source, self._store)
                settled.append(source)
            else:
                raise CommandRecoveryRequired("suspended delivery source has unsupported status")

        proof = await work.runs.prove_quiescent(run.id, exclude_command_id=resume_command.id)
        if not proof.is_quiescent:
            raise CommandRecoveryRequired("paused run has unresolved durable effects")
        return tuple(settled)

    async def _settle_retained(
        self,
        work: UnitOfWork,
        run: RunSnapshot,
        pause: CommandEnvelope,
        source: CommandEnvelope,
    ) -> CommandEnvelope:
        kind = _KIND_BY_COMMAND.get(source.command_type)
        if (
            kind not in {"plan", "implement", "review", "validate"}
            or source.payload_schema_version != 1
        ):
            raise CommandRecoveryRequired("retained outcome source is invalid")
        attempt = source.payload.get("semantic_attempt", 1)
        if type(attempt) is not int or attempt < 1 or source.expected_run_version > run.version - 1:
            raise CommandRecoveryRequired("retained outcome source is invalid")
        admitted = _admitted_run(run, source, kind)
        cancelled = await work.commands.cancel_expired_observed_lease(
            source, reason="retained stopped delivery settled during resume reconciliation"
        )
        if cancelled is None:
            raise CommandRecoveryRequired("retained outcome lease changed before settlement")
        if self._store is not None:
            published = await published_stage(work, self._store, run, cancelled)
            if published is not None:
                step_id, execution_id, output_id = published
                await _record_retained_receipt(
                    work,
                    run,
                    pause,
                    source,
                    admitted,
                    step_id,
                    kind,
                    attempt,
                    execution_id,
                    output_id,
                    published=True,
                )
                return cancelled
        if kind == "validate":
            step_id = await cancel_retained_validation(work, run, cancelled)
            await _record_retained_receipt(
                work, run, pause, source, admitted, step_id, kind, attempt, None, None
            )
            return cancelled
        if self._store is None:
            raise CommandRecoveryRequired("retained outcome recovery requires artifact storage")
        execution_id = uuid5(_EXECUTION_NAMESPACE[kind], str(source.id))
        admission = await work.executions.get_admission(run.id, execution_id)
        if (
            admission is None
            or admission.kind != kind
            or admission.attempt != attempt
            or admission.step_id != uuid5(_step_namespace(kind), str(source.id))
            or admission.role is not _role(kind)
            or admission.status is not ExecutionStatus.RUNNING
        ):
            raise CommandRecoveryRequired("retained outcome admission is invalid")
        retained = await load_retained_outcome(
            work,
            self._store,
            command_id=source.id,
            delivery_attempt=source.attempt,
            admission=admission,
        )
        await work.executions.finalize(
            run.id,
            admission.step_id,
            execution_id,
            AgentFinishStatus.CANCELLED,
            retained.usage,
            output_artifact_id=retained.output_artifact_id,
            provider=admission.provider,
            model=admission.model,
            instruction_version=admission.instruction_version,
            kind=kind,
            attempt=attempt,
            role=admission.role,
        )
        await _record_retained_receipt(
            work,
            run,
            pause,
            source,
            admitted,
            admission.step_id,
            kind,
            attempt,
            execution_id,
            retained.output_artifact_id,
        )
        return cancelled


def _step_namespace(kind: str) -> UUID:
    return {
        "plan": UUID("3b7b0e4a-5e6a-4b8d-9db0-3c3c12f1f411"),
        "implement": UUID("5f7bc719-a867-443c-b6a8-936c6663a983"),
        "review": UUID("11c37116-b982-454c-b101-0f7e4bc4e3ed"),
    }[kind]


def _role(kind: str) -> AgentRole:
    return (
        AgentRole.PLANNER
        if kind == "plan"
        else AgentRole.DEVELOPER
        if kind == "implement"
        else AgentRole.REVIEWER
    )


def _admitted_run(run: RunSnapshot, source: CommandEnvelope, kind: str) -> RunSnapshot:
    restored = StateEngine().resume(run)
    state = restored.state
    version = run.version - 1
    if (
        state not in _ADMITTED_STATES[kind]
        or version < source.expected_run_version
        or version > source.expected_run_version + (1 if kind == "plan" else 0)
        or StateEngine().pause(replace(restored, version=version)) != run
    ):
        raise CommandRecoveryRequired("retained outcome admitted state is invalid")
    return replace(restored, version=version)


async def _record_retained_receipt(
    work: UnitOfWork,
    run: RunSnapshot,
    pause: CommandEnvelope,
    source: CommandEnvelope,
    admitted: RunSnapshot,
    step_id: UUID,
    kind: str,
    attempt: int,
    execution_id: UUID | None,
    output_artifact_id: UUID | None,
    *,
    published: bool = False,
) -> None:
    payload = {
        "command_id": str(source.id),
        "command_type": source.command_type,
        "command_payload": dict(source.payload),
        "idempotency_key": source.idempotency_key,
        "delivery_attempt": source.attempt,
        "expected_run_version": source.expected_run_version,
        "admitted_run_version": admitted.version,
        "admitted_state": admitted.state.value,
        "control_command_id": str(pause.id),
        "step_id": str(step_id),
        "kind": kind,
        "semantic_attempt": attempt,
        "execution_id": str(execution_id) if execution_id else None,
        "output_artifact_id": str(output_artifact_id) if output_artifact_id else None,
    }
    if published:
        payload["published"] = True
    await work.events.append(
        RunEvent(
            run_id=run.id,
            run_version=run.version,
            event_type="delivery.suspended",
            payload=payload,
            actor_class="worker",
        )
    )


def _validate_resume(command: CommandEnvelope) -> None:
    if (
        command.command_type != "resume"
        or command.status is not CommandStatus.LEASED
        or command.payload_schema_version != 1
        or command.payload != {}
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
    matches = [
        event
        for event in await work.events.list_for_version(run.id, run.version)
        if event.event_type == "run.paused"
    ]
    if len(matches) != 1:
        raise CommandRecoveryRequired("paused run has no unique causal pause event")
    event = matches[0]
    if event.actor_class != "operator" or event.payload_schema_version != 1:
        raise CommandRecoveryRequired("causal pause event authority is invalid")
    try:
        pause = await work.commands.get(UUID(str(event.payload.get("command_id"))))
    except CommandNotFound, TypeError, ValueError:
        raise CommandRecoveryRequired("causal pause command is missing") from None
    expected_payload = {
        "command_id": str(pause.id),
        "command_type": "pause",
        "command_payload": {},
        "expected_run_version": run.version - 1,
    }
    if (
        pause.run_id != run.id
        or pause.command_type != "pause"
        or pause.status is not CommandStatus.COMPLETED
        or pause.actor_id is None
        or pause.actor_id != event.actor_id
        or pause.payload_schema_version != 1
        or pause.payload != {}
        or pause.expected_run_version != run.version - 1
        or event.payload != expected_payload
    ):
        raise CommandRecoveryRequired("causal pause command binding is invalid")
    return pause


def _receipt_command_id(receipt: RunEvent) -> UUID:
    if (
        receipt.actor_class != "worker"
        or receipt.actor_id is not None
        or receipt.payload_schema_version != 1
    ):
        raise CommandRecoveryRequired("suspended delivery receipt authority is invalid")
    try:
        return UUID(str(receipt.payload.get("command_id")))
    except TypeError, ValueError:
        raise CommandRecoveryRequired(
            "suspended delivery receipt command identifier is invalid"
        ) from None


async def _validate_receipt(
    work: UnitOfWork,
    run: RunSnapshot,
    pause: CommandEnvelope,
    receipt: RunEvent,
    source: CommandEnvelope,
    store: ArtifactStore | None,
) -> None:
    source_id = _receipt_command_id(receipt)
    payload: Mapping[str, object] = receipt.payload
    kind = _KIND_BY_COMMAND.get(source.command_type)
    semantic_attempt = source.payload.get("semantic_attempt", 1)
    if receipt.event_type == "delivery.deferred":
        expected = {
            "command_id": str(source.id),
            "command_type": source.command_type,
            "idempotency_key": source.idempotency_key,
            "command_payload": dict(source.payload),
            "expected_run_version": source.expected_run_version,
            "actor_id": str(source.actor_id) if source.actor_id is not None else None,
            "payload_schema_version": 1,
            "delivery_attempt": 0,
            "kind": kind,
            "semantic_attempt": semantic_attempt,
            "pause_command_id": str(pause.id),
            "deferred_state": run.suspended_state.value
            if run.suspended_state is not None
            else None,
        }
        if (
            payload != expected
            or source.status is not CommandStatus.CANCELLED
            or source.attempt != 0
            or source.payload_schema_version != 1
            or source.run_id != run.id
            or receipt.run_id != run.id
            or receipt.run_version != run.version
            or source.expected_run_version != run.version - 1
            or kind is None
            or type(semantic_attempt) is not int
            or semantic_attempt < 1
        ):
            raise CommandRecoveryRequired("deferred delivery receipt binding is invalid")
        try:
            next_attempt = (
                await work.controller_steps.next_attempt(run.id, kind)
                if kind == "validate"
                else await work.executions.next_attempt(run.id, kind)
            )
        except ControllerStepUnsettledError, ExecutionUnsettledError:
            raise CommandRecoveryRequired("deferred delivery admission is unsettled") from None
        if next_attempt != semantic_attempt:
            raise CommandRecoveryRequired("deferred delivery already has an admission")
        return
    admitted_version = payload.get("admitted_run_version")
    if (
        source_id != source.id
        or kind is None
        or receipt.run_id != run.id
        or source.run_id != run.id
        or source.payload_schema_version != 1
        or receipt.run_version != run.version
        or payload.get("command_type") != source.command_type
        or payload.get("command_payload") != dict(source.payload)
        or payload.get("idempotency_key") != source.idempotency_key
        or payload.get("delivery_attempt") != source.attempt
        or payload.get("expected_run_version") != source.expected_run_version
        or payload.get("control_command_id") != str(pause.id)
        or type(semantic_attempt) is not int
        or semantic_attempt < 1
        or payload.get("kind") != kind
        or payload.get("semantic_attempt") != semantic_attempt
        or type(admitted_version) is not int
        or admitted_version != run.version - 1
        or admitted_version < source.expected_run_version
        or admitted_version > source.expected_run_version + (1 if kind == "plan" else 0)
        or payload.get("admitted_state") not in {state.value for state in _ADMITTED_STATES[kind]}
        or run.suspended_state is None
        or payload.get("admitted_state") != run.suspended_state.value
    ):
        raise CommandRecoveryRequired("suspended delivery receipt binding is invalid")
    try:
        step_id = UUID(str(payload.get("step_id")))
    except TypeError, ValueError:
        raise CommandRecoveryRequired(
            "suspended delivery receipt step identifier is invalid"
        ) from None
    if payload.get("published") is True:
        if store is None:
            raise CommandRecoveryRequired("published receipt requires artifact storage")
        durable_source = await work.commands.get(source.id)
        published = await published_stage(work, store, run, durable_source)
        if published is None or (
            published[0] != step_id
            or (str(published[1]) if published[1] is not None else None)
            != payload.get("execution_id")
            or str(published[2]) != payload.get("output_artifact_id")
        ):
            raise CommandRecoveryRequired("published receipt lineage differs")
        return
    if kind == "validate":
        step = await work.controller_steps.get(run.id, step_id)
        if (
            step is None
            or step.kind != kind
            or step.attempt != semantic_attempt
            or step.status is not ExecutionStatus.CANCELLED
            or payload.get("execution_id") is not None
            or (str(step.output_artifact_id) if step.output_artifact_id is not None else None)
            != payload.get("output_artifact_id")
        ):
            raise CommandRecoveryRequired("suspended controller lineage is not terminal")
        return
    outcome = await work.executions.get_outcome(run.id, kind, semantic_attempt)
    if (
        outcome is None
        or outcome.status is not ExecutionStatus.CANCELLED
        or outcome.step_id != step_id
        or str(outcome.agent_execution_id) != payload.get("execution_id")
        or (str(outcome.output_artifact_id) if outcome.output_artifact_id is not None else None)
        != payload.get("output_artifact_id")
    ):
        raise CommandRecoveryRequired("suspended delivery execution lineage is not terminal")


__all__ = ["ResumeReconciler"]
