"""Consume an existing operator PR approval without creating authority."""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast
from uuid import UUID

from forge.application.ports.clock import Clock, SystemClock
from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.services.approvals import ApprovalCommandValidationError
from forge.application.services.approved_plan import ApprovedPlanLoader
from forge.application.services.pr_evidence import PrEvidenceValidationError, PrEvidenceValidator
from forge.application.services.validation import _fence_command
from forge.domain.command import CommandEnvelope, CommandStatus
from forge.domain.event import RunEvent
from forge.domain.run import RunSnapshot, RunState
from forge.persistence.models import Approval


class ApprovePrHandler:
    def __init__(
        self,
        evidence: PrEvidenceValidator,
        approved_plans: ApprovedPlanLoader,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._evidence, self._approved, self._clock = (
            evidence,
            approved_plans,
            clock or SystemClock(),
        )

    async def __call__(self, command: CommandEnvelope, work: UnitOfWork) -> None:
        approval_id = _approval_id(command)
        await _fence_command(command, work)
        run = await work.runs.get_for_update(command.run_id)
        approval = cast(
            Approval | None, await work.auth.get_approval(approval_id=approval_id, for_update=True)
        )
        if (
            approval is None
            or approval.run_id != run.id
            or approval.gate != "pr"
            or approval.authenticated_actor_id != command.actor_id
            or approval.run_version != command.expected_run_version
        ):
            raise ApprovalCommandValidationError("approval evidence is stale")
        if await self._replay(command, work, run, approval):
            await work.commit()
            return
        if (
            approval.policy_version != run.policy_version
            or approval.invalidated_at is not None
            or run.state is not RunState.AWAITING_PR_APPROVAL
            or run.version != approval.run_version
            or run.pending_evidence_digest != approval.evidence_digest
        ):
            raise ApprovalCommandValidationError("approval evidence is stale")
        try:
            await self._evidence.validate(work, run.id)
        except PrEvidenceValidationError as error:
            await _fence_command(command, work)
            await work.auth.invalidate_pr_gate(
                run_id=run.id, run_version=run.version, at=self._clock.now()
            )
            if error.category == "content_drift":
                approved = await self._approved.load(work, run.id)
                remediating = await work.runs.transition(
                    run.id,
                    run.version,
                    RunState.REMEDIATING,
                    "approval.stale",
                    {"approval_id": str(approval.id), "reason": error.category},
                    actor_class="worker",
                    actor_id=command.actor_id,
                    occurred_at=self._clock.now(),
                )
                validating = await work.runs.transition(
                    run.id,
                    remediating.version,
                    RunState.VALIDATING,
                    "run.candidate_revalidation_requested",
                    {"approval_id": str(approval.id)},
                    actor_class="worker",
                    actor_id=command.actor_id,
                    occurred_at=self._clock.now(),
                )
                attempt = await work.controller_steps.next_attempt(run.id, "validate")
                queued = await work.commands.enqueue(
                    run_id=run.id,
                    command_type="validate",
                    idempotency_key=f"{run.id}:validate:{attempt}",
                    payload={"semantic_attempt": attempt},
                    expected_run_version=validating.version,
                    actor_id=approved.approval_actor_id,
                    available_at=self._clock.now(),
                )
                await self._settled(command, work, validating, approval, queued)
                await work.commit()
                return
            intervention = await work.runs.intervene(
                run.id,
                run.version,
                "approval.stale",
                {
                    "approval_id": str(approval.id),
                    "source_command_id": str(command.id),
                    "reason": error.category,
                },
                actor_class="worker",
                actor_id=command.actor_id,
                occurred_at=self._clock.now(),
            )
            await self._settled(command, work, intervention, approval, None)
            await work.commit()
            return
        await _fence_command(command, work)
        transitioned = await work.runs.transition(
            run.id,
            run.version,
            RunState.PUBLISHING_PR,
            "run.pr_approved",
            {"approval_id": str(approval.id)},
            actor_class="operator",
            actor_id=command.actor_id,
            occurred_at=self._clock.now(),
        )
        queued = await work.commands.enqueue(
            run_id=run.id,
            command_type="publish_pr",
            idempotency_key=f"{run.id}:publish-pr:{transitioned.version}",
            payload={"approval_id": str(approval.id)},
            expected_run_version=transitioned.version,
            actor_id=command.actor_id,
            available_at=self._clock.now(),
        )
        await self._settled(command, work, transitioned, approval, queued)
        await work.commit()

    async def _settled(
        self,
        command: CommandEnvelope,
        work: UnitOfWork,
        run: RunSnapshot,
        approval: Approval,
        queued: CommandEnvelope | None,
    ) -> None:
        await _fence_command(command, work)
        payload: dict[str, object] = {
            "source_command_id": str(command.id),
            "approval_id": str(approval.id),
            "approval_digest": approval.evidence_digest,
            "target": run.state.value,
            "invalidated": approval.invalidated_at is not None,
            "queued": None
            if queued is None
            else {
                "id": str(queued.id),
                "key": queued.idempotency_key,
                "command_type": queued.command_type,
                "payload": dict(queued.payload),
                "actor_id": str(queued.actor_id),
                "version": queued.expected_run_version,
            },
        }
        await work.events.append(
            RunEvent(
                run_id=run.id,
                run_version=run.version,
                event_type="run.pr_approval_consumed",
                actor_class="worker",
                actor_id=command.actor_id,
                payload=payload,
            )
        )

    async def _replay(
        self,
        command: CommandEnvelope,
        work: UnitOfWork,
        run: RunSnapshot,
        approval: Approval,
    ) -> bool:
        events = [
            event
            for event in await work.events.list_after(run.id, 0)
            if event.event_type == "run.pr_approval_consumed"
            and event.payload.get("source_command_id") == str(command.id)
        ]
        if not events:
            return False
        if len(events) != 1:
            raise ApprovalCommandValidationError("approval replay differs")
        event = events[0]
        payload = event.payload
        expected_version = command.expected_run_version + (
            2 if run.state is RunState.VALIDATING else 1
        )
        if (
            run.state
            not in {
                RunState.PUBLISHING_PR,
                RunState.VALIDATING,
                RunState.AWAITING_HUMAN_INTERVENTION,
            }
            or run.version != expected_version
            or event.run_version != run.version
            or event.actor_class != "worker"
            or event.actor_id != command.actor_id
            or payload.get("approval_id") != str(approval.id)
            or payload.get("approval_digest") != approval.evidence_digest
            or payload.get("target") != run.state.value
            or payload.get("invalidated") != (approval.invalidated_at is not None)
            or (approval.invalidated_at is None) != (run.state is RunState.PUBLISHING_PR)
        ):
            raise ApprovalCommandValidationError("approval replay differs")
        saved = payload.get("queued")
        if run.state is RunState.AWAITING_HUMAN_INTERVENTION:
            if saved is not None:
                raise ApprovalCommandValidationError("approval replay differs")
            return True
        if not isinstance(saved, Mapping) or not isinstance(saved.get("key"), str):
            raise ApprovalCommandValidationError("approval replay differs")
        queued = await work.commands.get_by_idempotency_key(saved["key"])
        if (
            queued is None
            or {
                "id": str(queued.id),
                "key": queued.idempotency_key,
                "command_type": queued.command_type,
                "payload": dict(queued.payload),
                "actor_id": str(queued.actor_id),
                "version": queued.expected_run_version,
            }
            != saved
        ):
            raise ApprovalCommandValidationError("approval replay differs")
        return True


def _approval_id(command: CommandEnvelope) -> UUID:
    if (
        command.command_type != "approve_pr"
        or command.status is not CommandStatus.LEASED
        or command.payload_schema_version != 1
        or set(command.payload) != {"approval_id"}
    ):
        raise ApprovalCommandValidationError("approval command is invalid")
    try:
        return UUID(str(command.payload["approval_id"]))
    except TypeError, ValueError:
        raise ApprovalCommandValidationError("approval command is invalid") from None


__all__ = ["ApprovePrHandler"]
