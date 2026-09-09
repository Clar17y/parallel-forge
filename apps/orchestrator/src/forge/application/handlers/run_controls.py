"""Durable worker handlers for operator pause and cancel commands."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict
from uuid import UUID

from forge.application.ports.artifacts import ArtifactStore
from forge.application.ports.commands import CommandLeaseLost, CommandRecoveryRequired
from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.ports.worktrees import PreparedWorktreeInspector
from forge.application.services.approved_plan import ApprovedPlanLoader
from forge.application.services.base_update_replay import settle_base_updates
from forge.application.services.failed_resume import settle_failed_delivery, validate_failed_receipt
from forge.application.services.monitor_resume import (
    monitor_origin,
    resume_monitoring,
    settle_observed_monitors,
)
from forge.application.services.paused_approvals import settle_paused_approvals
from forge.application.services.preparation_resume import PreparationResumeService
from forge.application.services.release_resume import (
    resume_release,
    resumed_release_origin,
    settle_published_deliveries,
    settle_reviewed_push_deliveries,
)
from forge.application.services.resume_continuation import enqueue_resumed_stage
from forge.application.services.resume_reconciliation import ResumeReconciler
from forge.application.services.resume_source import continuation_binding, resume_command_ids
from forge.application.services.runs import cancellation_rejection_reason
from forge.application.services.state_engine import StateEngine
from forge.domain.command import CommandEnvelope, CommandStatus
from forge.domain.event import RunEvent
from forge.domain.run import RunSnapshot, RunState
from forge.persistence.repositories.commands import CommandNotFound

_ACTIVE_RESUME_STATES = frozenset(
    {
        RunState.CREATED,
        RunState.PREPARING_WORKTREE,
        RunState.PLANNING,
        RunState.IMPLEMENTING,
        RunState.REMEDIATING,
        RunState.VALIDATING,
        RunState.REVIEWING,
    }
)


class ControlCommandRejected(RuntimeError):
    """A current delivery conclusively lacks authority for this run version."""


class PauseRunHandler:
    """Fence and apply one operator-authorized pause transition."""

    async def __call__(self, command: CommandEnvelope, work: UnitOfWork) -> None:
        await _execute(command, work, target=RunState.PAUSED)


class CancelRunHandler:
    """Fence and apply one operator-authorized cancellation transition."""

    async def __call__(self, command: CommandEnvelope, work: UnitOfWork) -> None:
        await _execute(command, work, target=RunState.CANCELLED)


class ResumeRunHandler:
    """Restore a reconciled run and atomically queue its local continuation."""

    def __init__(
        self,
        *,
        artifact_store: ArtifactStore | None = None,
        preparation_inspector: PreparedWorktreeInspector | None = None,
    ) -> None:
        self._store = artifact_store
        self._reconciler = ResumeReconciler(artifact_store)
        self._preparation = (
            PreparationResumeService(ApprovedPlanLoader(artifact_store), preparation_inspector)
            if artifact_store is not None
            else None
        )

    async def __call__(self, command: CommandEnvelope, work: UnitOfWork) -> None:
        _validate_resume_command(command)
        try:
            fenced = await work.commands.assert_current_lease(command)
        except CommandLeaseLost, CommandNotFound:
            raise CommandRecoveryRequired("resume command lease is no longer current") from None
        if not _same_delivery(command, fenced):
            raise CommandRecoveryRequired("resume command delivery differs from its lease")

        run = await work.runs.get_for_update(command.run_id)
        if run.state is not RunState.PAUSED:
            resumed = await _load_resume_event(work, command)
            expected_state = resumed[0] if resumed is not None else None
            pause_authority = (
                await _validate_pause_authority_at_version(
                    work, command.run_id, command.expected_run_version
                )
                if resumed is not None
                else None
            )
            if (
                expected_state is None
                or pause_authority is None
                or run.version != command.expected_run_version + 1
                or run.state is not expected_state
                or not await _replayed(
                    work,
                    command,
                    run.version,
                    "run.resumed",
                    await _resume_replay_payload(
                        work,
                        command,
                        run,
                        pause_authority.id,
                        resumed[2] if resumed is not None else None,
                    ),
                )
            ):
                raise CommandRecoveryRequired("resume replay requires recovery")
            await work.commit()
            return

        if run.version != command.expected_run_version:
            raise ControlCommandRejected("resume command version is stale")
        target = _restored_state(run)
        if target not in _ACTIVE_RESUME_STATES | {
            RunState.MONITORING_PR,
            RunState.PUBLISHING_PR,
            RunState.MERGING,
            RunState.AWAITING_PLAN_APPROVAL,
            RunState.AWAITING_PR_APPROVAL,
            RunState.AWAITING_MERGE_APPROVAL,
            RunState.AWAITING_HUMAN_INTERVENTION,
        }:
            raise CommandRecoveryRequired("paused active phase requires recovery")

        pause_command = await _validate_pause_authority(work, run)
        await _settle_redundant_resumes(work, command, pause_command)
        await settle_published_deliveries(work, command, run)
        await settle_reviewed_push_deliveries(work, command, run, self._store)
        await settle_base_updates(work, command, run, self._store)
        await settle_observed_monitors(work, command, run, self._store)
        from forge.application.services.queue_resume import settle_queue_admission_deliveries

        await settle_queue_admission_deliveries(work, command, run)
        payload = _resume_payload(command, target, pause_command.id, run=run)
        if target in {RunState.PUBLISHING_PR, RunState.MERGING}:
            source, queued = await resume_release(work, command, run, pause_command)
            payload["continuation"] = continuation_binding(queued, source.id)
        elif (
            target is RunState.REMEDIATING
            and any(
                source.command_type == "update_base"
                for source in await work.commands.list_outstanding_normal(
                    run_id=run.id, exclude_command_id=command.id
                )
            )
        ) or (
            target is RunState.MONITORING_PR
            and any(
                source.command_type == "push_reviewed_pr"
                for source in await work.commands.list_outstanding_normal(
                    run_id=run.id, exclude_command_id=command.id
                )
            )
        ):
            source, queued = await resume_release(
                work, command, run, pause_command, store=self._store
            )
            payload["continuation"] = continuation_binding(queued, source.id)
        elif target is RunState.MONITORING_PR:
            source, queued = await resume_monitoring(work, command, run, pause_command)
            payload["continuation"] = continuation_binding(queued, source.id)
        elif target is RunState.PREPARING_WORKTREE:
            sources = await work.commands.list_outstanding_normal(
                run_id=run.id, exclude_command_id=command.id
            )
            if self._preparation is None or len(sources) != 1:
                raise CommandRecoveryRequired("preparation resume has no unique source")
            prepared = await self._preparation.settle_and_enqueue(work, command, sources[0])
            payload["continuation"] = continuation_binding(prepared.queued, prepared.source.id)
        elif target in _ACTIVE_RESUME_STATES:
            sources = await self._reconciler.reconcile(work, command)
            if not sources:
                sources = await settle_failed_delivery(work, command, run)
            queued = await enqueue_resumed_stage(work, command, run, sources)
            payload["continuation"] = continuation_binding(queued, sources[0].id)
        else:
            await settle_paused_approvals(work, command, run, pause_command)
            proof = await work.runs.prove_quiescent(run.id, exclude_command_id=command.id)
            if not proof.is_quiescent:
                raise CommandRecoveryRequired("paused run has unsettled durable work")

        await work.runs.resume(
            run.id,
            run.version,
            "run.resumed",
            payload,
            actor_class="operator",
            actor_id=command.actor_id,
        )
        await work.commit()


async def _settle_redundant_resumes(
    work: UnitOfWork, resume: CommandEnvelope, pause: CommandEnvelope
) -> None:
    """Coalesce unstarted identical intentions inside the resume transaction.

    The caller holds the exact paused run and resume lease. Other stage work
    remains visible to reconciliation and quiescence; failed restoration rolls
    back these settlements together with the run transition.
    """
    outstanding = await work.commands.list_outstanding_normal(
        run_id=resume.run_id, exclude_command_id=resume.id
    )
    for other in outstanding:
        if other.command_type != "resume":
            continue
        if (
            other.run_id != resume.run_id
            or other.expected_run_version != resume.expected_run_version
            or other.status is not CommandStatus.PENDING
            or other.attempt != 0
            or other.payload_schema_version != 1
            or other.payload != {}
            or other.actor_id is None
            or other.lease_owner is not None
            or other.lease_expires_at is not None
            or other.completed_at is not None
        ):
            raise CommandRecoveryRequired("competing resume is not an unstarted identical intent")
        if await work.commands.cancel_pending_unstarted(other) is None:
            raise CommandRecoveryRequired("competing resume changed before supersession")
        await work.events.append(
            RunEvent(
                run_id=resume.run_id,
                run_version=resume.expected_run_version,
                event_type="resume.superseded",
                actor_class="worker",
                payload={
                    "command_id": str(other.id),
                    "command_type": other.command_type,
                    "idempotency_key": other.idempotency_key,
                    "command_payload": {},
                    "payload_schema_version": other.payload_schema_version,
                    "expected_run_version": other.expected_run_version,
                    "delivery_attempt": other.attempt,
                    "actor_id": str(other.actor_id),
                    "resume_command_id": str(resume.id),
                    "pause_command_id": str(pause.id),
                },
            )
        )


async def _execute(command: CommandEnvelope, work: UnitOfWork, *, target: RunState) -> None:
    _validate_command(command, target)
    try:
        fenced = await work.commands.assert_current_lease(command)
    except CommandLeaseLost, CommandNotFound:
        raise CommandRecoveryRequired("control command lease is no longer current") from None
    if not _same_delivery(command, fenced):
        raise CommandRecoveryRequired("control command delivery differs from its lease")
    run = await work.runs.get_for_update(command.run_id)
    if target is RunState.CANCELLED:
        reason = cancellation_rejection_reason(run)
        if reason is not None:
            raise ControlCommandRejected(reason)
    payload = _event_payload(command)
    event_type = "run.paused" if target is RunState.PAUSED else "run.cancelled"

    if run.state is target:
        if run.version != command.expected_run_version + 1 or not await _replayed(
            work, command, run.version, event_type, payload
        ):
            raise CommandRecoveryRequired("control command replay requires recovery")
        await work.commit()
        return
    if run.version != command.expected_run_version:
        raise ControlCommandRejected("control command version is stale")
    if target is RunState.PAUSED:
        await work.runs.pause(
            run.id,
            run.version,
            event_type,
            payload,
            actor_class="operator",
            actor_id=command.actor_id,
        )
    else:
        await work.runs.transition(
            run.id,
            run.version,
            RunState.CANCELLED,
            event_type,
            payload,
            actor_class="operator",
            actor_id=command.actor_id,
        )
    await work.commit()


def _validate_command(command: CommandEnvelope, target: RunState) -> None:
    wanted = "pause" if target is RunState.PAUSED else "cancel"
    if (
        not isinstance(command, CommandEnvelope)
        or command.command_type != wanted
        or command.status is not CommandStatus.LEASED
        or command.payload_schema_version != 1
        or command.payload != {}
        or command.actor_id is None
    ):
        raise CommandRecoveryRequired("control command authority is invalid")


def _validate_resume_command(command: CommandEnvelope) -> None:
    if (
        not isinstance(command, CommandEnvelope)
        or command.command_type != "resume"
        or command.status is not CommandStatus.LEASED
        or command.payload_schema_version != 1
        or command.payload != {}
        or command.actor_id is None
    ):
        raise CommandRecoveryRequired("resume command authority is invalid")


def _same_delivery(command: CommandEnvelope, fenced: CommandEnvelope) -> bool:
    """Accept a lease renewal while rejecting substituted command authority."""

    return (
        fenced.id == command.id
        and fenced.run_id == command.run_id
        and fenced.command_type == command.command_type
        and fenced.idempotency_key == command.idempotency_key
        and fenced.payload == command.payload
        and fenced.status is CommandStatus.LEASED
        and fenced.expected_run_version == command.expected_run_version
        and fenced.actor_id == command.actor_id
        and fenced.payload_schema_version == command.payload_schema_version
        and fenced.attempt == command.attempt
        and fenced.lease_owner == command.lease_owner
    )


def _event_payload(command: CommandEnvelope) -> dict[str, object]:
    return {
        "command_id": str(command.id),
        "command_type": command.command_type,
        "command_payload": dict(command.payload),
        "expected_run_version": command.expected_run_version,
    }


def _restored_state(run: RunSnapshot) -> RunState | None:
    context = run.suspension_context
    target = context.state if context is not None else run.suspended_state
    return target if isinstance(target, RunState) else None


async def _validate_pause_authority(work: UnitOfWork, run: RunSnapshot) -> CommandEnvelope:
    return await _validate_pause_authority_at_version(work, run.id, run.version)


async def _validate_pause_authority_at_version(
    work: UnitOfWork, run_id: UUID, paused_version: int
) -> CommandEnvelope:
    events = [
        event
        for event in await work.events.list_for_version(run_id, paused_version)
        if event.event_type == "run.paused"
    ]
    if len(events) != 1:
        raise CommandRecoveryRequired("paused run has no unique causal pause event")
    event = events[0]
    if event.actor_class != "operator" or event.payload_schema_version != 1:
        raise CommandRecoveryRequired("causal pause event authority is invalid")
    payload = event.payload
    raw_id = payload.get("command_id")
    try:
        pause_id = UUID(str(raw_id))
    except AttributeError, TypeError, ValueError:
        raise CommandRecoveryRequired("causal pause command identifier is invalid") from None
    try:
        pause = await work.commands.get(pause_id)
    except CommandNotFound:
        raise CommandRecoveryRequired("causal pause command is missing") from None
    if (
        pause.run_id != run_id
        or pause.command_type != "pause"
        or pause.status is not CommandStatus.COMPLETED
        or pause.actor_id is None
        or pause.actor_id != event.actor_id
        or pause.payload_schema_version != 1
        or pause.payload != {}
        or pause.expected_run_version != paused_version - 1
        or payload != _event_payload(pause)
    ):
        raise CommandRecoveryRequired("causal pause command binding is invalid")
    return pause


def _resume_payload(
    command: CommandEnvelope,
    target: RunState,
    pause_command_id: UUID,
    *,
    run: RunSnapshot,
) -> dict[str, object]:
    restored = StateEngine().resume(run) if run.state is RunState.PAUSED else run
    payload: dict[str, object] = {
        "command_id": str(command.id),
        "command_type": "resume",
        "command_payload": {},
        "expected_run_version": command.expected_run_version,
        "paused_version": command.expected_run_version,
        "pause_command_id": str(pause_command_id),
        "restored_state": target.value,
        "restored_snapshot_digest": hashlib.sha256(
            json.dumps(
                asdict(restored), default=str, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest(),
    }
    if restored.pending_gate is not None and restored.pending_gate.value in {"pr", "merge"}:
        payload["approval_gate"] = {
            "gate": restored.pending_gate.value,
            "evidence_digest": restored.pending_evidence_digest,
            "source_version": command.expected_run_version - 1,
        }
    return payload


async def _load_resume_event(
    work: UnitOfWork, command: CommandEnvelope
) -> tuple[RunState, UUID, object] | None:
    events = [
        event
        for event in await work.events.list_for_version(
            command.run_id, command.expected_run_version + 1
        )
        if event.event_type == "run.resumed"
    ]
    if (
        len(events) != 1
        or events[0].actor_class != "operator"
        or events[0].actor_id != command.actor_id
    ):
        return None
    payload = events[0].payload
    raw = payload.get("restored_state")
    pause_id = payload.get("pause_command_id")
    try:
        return RunState(raw), UUID(str(pause_id)), payload.get("continuation")  # type: ignore[arg-type]
    except TypeError, ValueError:
        return None


async def _resume_replay_payload(
    work: UnitOfWork,
    command: CommandEnvelope,
    run: RunSnapshot,
    pause_id: UUID,
    continuation: object,
) -> dict[str, object]:
    payload = _resume_payload(command, run.state, pause_id, run=run)
    if run.state in {RunState.MONITORING_PR, RunState.PUBLISHING_PR, RunState.MERGING}:
        if not isinstance(continuation, Mapping):
            raise CommandRecoveryRequired("resumed monitor continuation is missing")
        try:
            queued = await work.commands.get(UUID(str(continuation.get("command_id"))))
        except ValueError, CommandNotFound:
            raise CommandRecoveryRequired("resumed monitor continuation is invalid") from None
        if queued.expected_run_version != run.version or queued.payload.get(
            "resume_command_id"
        ) != str(command.id):
            raise CommandRecoveryRequired("resumed monitor continuation authority differs")
        if queued.command_type == "push_reviewed_pr":
            await resumed_release_origin(work, queued, replaying_resume=command.id)
        elif run.state is RunState.MONITORING_PR:
            await monitor_origin(work, queued, replaying_resume=command.id)
        else:
            await resumed_release_origin(work, queued, replaying_resume=command.id)
        payload["continuation"] = dict(continuation)
        return payload
    if run.state not in _ACTIVE_RESUME_STATES:
        if continuation is not None:
            raise CommandRecoveryRequired("quiescent resume has unexpected continuation")
        return payload
    if not isinstance(continuation, Mapping):
        raise CommandRecoveryRequired("resumed stage continuation is missing")
    try:
        queued = await work.commands.get(UUID(str(continuation.get("command_id"))))
        identities = resume_command_ids(queued.payload)
        if identities is None or identities[0] != command.id:
            raise ValueError
        source = await work.commands.get(identities[1])
    except CommandNotFound, ValueError, TypeError:
        raise CommandRecoveryRequired("resumed stage continuation is invalid") from None
    if queued.command_type == "update_base" and run.state is RunState.REMEDIATING:
        if queued.expected_run_version != run.version:
            raise CommandRecoveryRequired("resumed base continuation version differs")
        await resumed_release_origin(work, queued, replaying_resume=command.id)
        payload["continuation"] = continuation_binding(queued, source.id)
        return payload
    if (
        queued.run_id != run.id
        or source.run_id != run.id
        or queued.status is not CommandStatus.PENDING
        or queued.expected_run_version != run.version
        or queued.actor_id != source.actor_id
        or queued.command_type != source.command_type
        or source.status
        not in {CommandStatus.COMPLETED, CommandStatus.CANCELLED, CommandStatus.FAILED}
    ):
        raise CommandRecoveryRequired("resumed stage continuation changed")
    if source.status is CommandStatus.FAILED:
        await validate_failed_receipt(
            work,
            source,
            paused_version=command.expected_run_version,
            pause_id=pause_id,
            state=run.state,
        )
    payload["continuation"] = continuation_binding(queued, source.id)
    return payload


async def _replayed(
    work: UnitOfWork,
    command: CommandEnvelope,
    version: int,
    event_type: str,
    payload: Mapping[str, object],
) -> bool:
    events = [
        event
        for event in await work.events.list_for_version(command.run_id, version)
        if event.event_type == event_type
    ]
    return (
        len(events) == 1
        and events[0].payload == payload
        and events[0].actor_class == "operator"
        and events[0].actor_id == command.actor_id
        and events[0].payload_schema_version == 1
    )


__all__ = [
    "CancelRunHandler",
    "ControlCommandRejected",
    "PauseRunHandler",
    "ResumeRunHandler",
]
