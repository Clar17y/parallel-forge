"""Fail-closed continuation of interrupted worktree preparation deliveries."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from forge.application.ports.commands import CommandLeaseLost, CommandRecoveryRequired
from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.ports.worktrees import PreparedWorktreeInspector
from forge.application.services.approved_plan import (
    ApprovedPlan,
    ApprovedPlanError,
    ApprovedPlanLoader,
)
from forge.application.services.resume_source import continuation_binding, resume_command_ids
from forge.domain.command import CommandEnvelope, CommandStatus
from forge.domain.event import RunEvent
from forge.domain.run import RunState
from forge.persistence.repositories.commands import CommandNotFound


@dataclass(frozen=True, slots=True)
class PreparationContinuation:
    source: CommandEnvelope
    queued: CommandEnvelope


class PreparationResumeService:
    """Continue preparations after proving their resource effects are settled."""

    def __init__(
        self, approved_plans: ApprovedPlanLoader, inspector: PreparedWorktreeInspector | None = None
    ) -> None:
        self._approved_plans = approved_plans
        self._inspector = inspector

    async def settle_and_enqueue(
        self, work: UnitOfWork, resume: CommandEnvelope, source: CommandEnvelope
    ) -> PreparationContinuation:
        _validate_resume(resume)
        try:
            fenced = await work.commands.assert_current_lease(resume)
        except CommandLeaseLost, CommandNotFound:
            raise CommandRecoveryRequired("resume command lease is no longer current") from None
        if not _same_delivery(resume, fenced):
            raise CommandRecoveryRequired("resume delivery differs from its lease")
        run = await work.runs.get_for_update(resume.run_id)
        if (
            run.state is not RunState.PAUSED
            or run.suspended_state is not RunState.PREPARING_WORKTREE
            or run.version != resume.expected_run_version
        ):
            raise CommandRecoveryRequired("preparation resume requires the exact paused run")
        pause = await _pause_authority(work, run.id, run.version)
        try:
            approved = await self._approved_plans.load(work, run.id)
        except ApprovedPlanError:
            raise CommandRecoveryRequired("preparation approval requires recovery") from None
        history = await preparation_history(work, source, approved)
        bound = await preparation_branch_source(work, history, approved)
        events = await work.events.list_after(run.id, 0)
        effects = run.worktree_path is not None or any(
            event.event_type.startswith("resource.") for event in events
        )
        expected_version = source.expected_run_version + (
            1 if bound is not None and bound.id == source.id else 0
        )
        if (not effects and run.version != expected_version + 1) or run.version <= expected_version:
            raise CommandRecoveryRequired("preparation source version differs from pause")
        if source.status is CommandStatus.PENDING and source.attempt == 0:
            settled = await work.commands.cancel_pending_unstarted(source)
        elif source.status is CommandStatus.LEASED and source.attempt > 0:
            settled = await work.commands.cancel_expired_observed_lease(
                source, reason="preparation stopped during pause reconciliation"
            )
        else:
            settled = None
        if settled is None:
            raise CommandRecoveryRequired("preparation delivery changed before settlement")
        proof = await work.runs.prove_quiescent(run.id, exclude_command_id=resume.id)
        if not proof.is_quiescent:
            raise CommandRecoveryRequired("paused preparation has unresolved durable effects")
        if effects:
            if self._inspector is None:
                raise CommandRecoveryRequired("preparation resources require inspection recovery")
            try:
                prepared = await self._inspector.inspect_prepared(run.id, approved.policy)
            except Exception:  # noqa: BLE001 - uncertain resources never authorize continuation
                raise CommandRecoveryRequired("preparation resource inspection failed") from None
            if (
                prepared.identity.run_id != run.id
                or prepared.identity.project_id != run.project_id
                or prepared.identity.branch != run.branch_name
                or str(prepared.path) != run.worktree_path
                or prepared.base_sha != run.base_sha
            ):
                raise CommandRecoveryRequired("preparation inspected resource differs")
        receipts = [
            event
            for event in events
            if event.event_type == "preparation.suspended"
            and event.payload.get("command_id") == str(source.id)
        ]
        if receipts:
            raise CommandRecoveryRequired("preparation source was already settled")
        await work.events.append(
            RunEvent(
                run_id=run.id,
                run_version=run.version,
                event_type="preparation.suspended",
                payload=_receipt_payload(source, pause.id, approved),
                actor_class="worker",
            )
        )
        attempt = source.payload.get("semantic_attempt", 1)
        if type(attempt) is not int or attempt < 1:
            raise CommandRecoveryRequired("preparation continuation attempt is invalid")
        successor = attempt + (0 if source.attempt == 0 else 1)
        payload = {
            "semantic_attempt": successor,
            "resume_command_id": str(resume.id),
            "source_command_id": str(source.id),
        }
        key = f"{run.id}:resume:{resume.id}:prepare_worktree:{successor}"
        queued = await work.commands.enqueue(
            run_id=run.id,
            command_type="prepare_worktree",
            idempotency_key=key,
            payload=payload,
            expected_run_version=run.version + 1,
            actor_id=approved.approval_actor_id,
        )
        if (
            queued.command_type != "prepare_worktree"
            or queued.idempotency_key != key
            or queued.payload != payload
            or queued.status is not CommandStatus.PENDING
            or queued.actor_id != approved.approval_actor_id
            or queued.expected_run_version != run.version + 1
        ):
            raise CommandRecoveryRequired("preparation continuation differs")
        return PreparationContinuation(settled, queued)


async def preparation_history(
    work: UnitOfWork, command: CommandEnvelope, approved: ApprovedPlan
) -> tuple[CommandEnvelope, ...]:
    """Prove every preparation continuation before resolving original approval."""
    if command != await work.commands.get(command.id):
        raise CommandRecoveryRequired("preparation source differs from durable command")
    history = [command]
    events = await work.events.list_after(command.run_id, 0)
    current = command
    while (ids := resume_command_ids(current.payload)) is not None:
        resume, previous = await work.commands.get(ids[0]), await work.commands.get(ids[1])
        attempt, prior = (
            current.payload.get("semantic_attempt"),
            previous.payload.get("semantic_attempt", 1),
        )
        if (
            current.command_type != "prepare_worktree"
            or current.payload_schema_version != 1
            or set(current.payload)
            != {"semantic_attempt", "resume_command_id", "source_command_id"}
            or type(attempt) is not int
            or type(prior) is not int
            or attempt != prior + (0 if previous.attempt == 0 else 1)
            or current.actor_id != approved.approval_actor_id
            or current.run_id != approved.run.id
            or previous.run_id != current.run_id
            or previous.command_type != "prepare_worktree"
            or previous.payload_schema_version != 1
            or previous.actor_id != current.actor_id
            or previous.status is not CommandStatus.CANCELLED
            or resume.run_id != current.run_id
            or resume.command_type != "resume"
            or resume.status is not CommandStatus.COMPLETED
            or resume.payload != {}
            or resume.payload_schema_version != 1
            or resume.actor_id is None
            or resume.expected_run_version + 1 != current.expected_run_version
            or previous.expected_run_version >= resume.expected_run_version
            or current.idempotency_key
            != f"{current.run_id}:resume:{resume.id}:prepare_worktree:{attempt}"
        ):
            raise CommandRecoveryRequired("preparation continuation authority differs")
        pause = await _pause_authority(work, command.run_id, resume.expected_run_version)
        restored = [
            event
            for event in events
            if event.event_type == "run.resumed"
            and event.run_version == current.expected_run_version
        ]
        stopped = [
            event
            for event in events
            if event.event_type == "preparation.suspended"
            and event.run_version == resume.expected_run_version
            and event.payload.get("command_id") == str(previous.id)
        ]
        if len(restored) != 1 or len(stopped) != 1:
            raise CommandRecoveryRequired("preparation continuation has no unique causal receipt")
        event, receipt = restored[0], stopped[0]
        if (
            event.actor_class != "operator"
            or event.actor_id != resume.actor_id
            or event.payload_schema_version != 1
            or event.payload.get("command_id") != str(resume.id)
            or event.payload.get("command_type") != "resume"
            or event.payload.get("command_payload") != {}
            or event.payload.get("expected_run_version") != resume.expected_run_version
            or event.payload.get("paused_version") != resume.expected_run_version
            or event.payload.get("pause_command_id") != str(pause.id)
            or event.payload.get("restored_state") != RunState.PREPARING_WORKTREE.value
            or event.payload.get("continuation") != continuation_binding(current, previous.id)
            or receipt.actor_class != "worker"
            or receipt.actor_id is not None
            or receipt.payload_schema_version != 1
            or receipt.payload != _receipt_payload(previous, pause.id, approved)
        ):
            raise CommandRecoveryRequired("preparation continuation causal evidence differs")
        history.append(previous)
        current = previous
    expected = approved.approval_version + 1
    if (
        current.run_id != approved.run.id
        or current.command_type != "prepare_worktree"
        or current.idempotency_key != f"{current.run_id}:prepare-worktree:{expected}"
        or current.payload != {}
        or current.payload_schema_version != 1
        or current.expected_run_version != expected
        or current.actor_id != approved.approval_actor_id
    ):
        raise CommandRecoveryRequired("original preparation approval authority differs")
    return tuple(history)


async def preparation_branch_source(
    work: UnitOfWork, history: tuple[CommandEnvelope, ...], approved: ApprovedPlan
) -> CommandEnvelope | None:
    events = [
        event
        for event in await work.events.list_after(approved.run.id, 0)
        if event.event_type == "run.preparation_branch_bound"
    ]
    if not events:
        if approved.run.branch_name is not None:
            raise CommandRecoveryRequired("preparation branch has no causal binding")
        return None
    if len(events) != 1:
        raise CommandRecoveryRequired("preparation branch binding is ambiguous")
    event = events[0]
    source = next(
        (item for item in history if str(item.id) == event.payload.get("source_command_id")), None
    )
    branch = f"forge/run/{approved.run.id.hex}"
    if (
        source is None
        or approved.run.branch_name != branch
        or event.run_version != source.expected_run_version + 1
        or event.actor_class != "worker"
        or event.actor_id != source.actor_id
        or event.payload_schema_version != 1
        or event.payload
        != {
            "source_command_id": str(source.id),
            "approval_id": str(approved.approval_id),
            "branch_name": branch,
        }
    ):
        raise CommandRecoveryRequired("preparation branch binding differs")
    return source


def _receipt_payload(
    source: CommandEnvelope, pause_id: UUID, approved: ApprovedPlan
) -> dict[str, object]:
    return {
        "command_id": str(source.id),
        "command_type": source.command_type,
        "command_payload": dict(source.payload),
        "idempotency_key": source.idempotency_key,
        "delivery_attempt": source.attempt,
        "expected_run_version": source.expected_run_version,
        "pause_command_id": str(pause_id),
        "approval_id": str(approved.approval_id),
        "approval_version": approved.approval_version,
    }


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


async def _pause_authority(work: UnitOfWork, run_id: UUID, version: int) -> CommandEnvelope:
    events = [
        event
        for event in await work.events.list_for_version(run_id, version)
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
        or pause.run_id != run_id
        or pause.command_type != "pause"
        or pause.status is not CommandStatus.COMPLETED
        or pause.actor_id is None
        or pause.actor_id != event.actor_id
        or pause.payload_schema_version != 1
        or pause.payload != {}
        or pause.expected_run_version != version - 1
        or event.payload
        != {
            "command_id": str(pause.id),
            "command_type": "pause",
            "command_payload": {},
            "expected_run_version": version - 1,
        }
    ):
        raise CommandRecoveryRequired("causal pause command binding is invalid")
    return pause


__all__ = ["PreparationContinuation", "PreparationResumeService"]
