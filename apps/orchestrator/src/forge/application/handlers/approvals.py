"""Durable worker handlers for plan approval and revision commands."""

from __future__ import annotations

from typing import cast
from uuid import UUID

from forge.application.ports.artifacts import ArtifactStore
from forge.application.ports.clock import Clock, SystemClock
from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.services.approvals import ApprovalCommandValidationError
from forge.application.services.plan_evidence import (
    PlanEvidenceValidationError,
    PlanEvidenceValidator,
)
from forge.application.services.plan_restart import restart_source
from forge.application.services.plan_revision import PlanRevisionService
from forge.domain.command import CommandEnvelope, CommandStatus
from forge.domain.run import RunState
from forge.persistence.models import Approval


class ApprovePlanHandler:
    """Consume one existing, API-authorized plan approval exactly once."""

    def __init__(
        self, evidence_validator: PlanEvidenceValidator, *, clock: Clock | None = None
    ) -> None:
        self._evidence_validator = evidence_validator
        self._clock = clock or SystemClock()

    async def __call__(self, command: CommandEnvelope, work: UnitOfWork) -> None:
        approval_id = _approval_id(command)
        run = await work.runs.get_for_update(command.run_id)
        approval = cast(
            Approval | None,
            await work.auth.get_approval(approval_id=approval_id, for_update=True),
        )
        if (
            approval is None
            or approval.run_id != run.id
            or approval.gate != "plan"
            or approval.authenticated_actor_id != command.actor_id
            or approval.run_version != command.expected_run_version
        ):
            raise ApprovalCommandValidationError("approval evidence is stale")
        if (
            run.state is RunState.PLANNING
            and run.version == command.expected_run_version + 1
            and await self._committed_restart(command, work, approval)
        ):
            await work.commit()
            return
        if (
            approval.policy_version != run.policy_version
            or approval.invalidated_at is not None
            or run.state is not RunState.AWAITING_PLAN_APPROVAL
            or run.version != approval.run_version
            or run.pending_evidence_digest != approval.evidence_digest
        ):
            raise ApprovalCommandValidationError("approval evidence is stale")
        try:
            await self._evidence_validator.validate(work, run.id)
        except PlanEvidenceValidationError:
            # This is an owned, otherwise-valid approval whose authoritative
            # source drifted after API authorization.  Settle the stale gate
            # instead of raising and rolling the invalidation back.
            source = await self._evidence_validator.current_source(work, run.id)
            await work.auth.invalidate_plan_gate(
                run_id=run.id, run_version=run.version, at=self._clock.now()
            )
            attempt = max(2, await work.executions.next_attempt(run.id, "plan"))
            queued = await work.commands.enqueue(
                run_id=run.id,
                command_type="start_planning",
                idempotency_key=f"{run.id}:start-planning:{attempt}",
                payload={"semantic_attempt": attempt},
                expected_run_version=run.version + 1,
                actor_id=command.actor_id,
            )
            await work.runs.restart_planning(
                run.id,
                run.version,
                policy_version=source.policy_version,
                base_ref=source.base_ref,
                base_sha=source.base_sha,
                event_type="approval.stale",
                event_payload={
                    "approval_id": str(approval.id),
                    "approval_evidence_digest": approval.evidence_digest,
                    "approval_policy_version": approval.policy_version,
                    "command_id": str(command.id),
                    "planning_command_id": str(queued.id),
                    "planning_payload": dict(queued.payload),
                    "semantic_attempt": attempt,
                },
                actor_class="worker",
                actor_id=command.actor_id,
                occurred_at=self._clock.now(),
            )
            await work.commit()
            return
        transitioned = await work.runs.transition(
            run.id,
            run.version,
            RunState.PREPARING_WORKTREE,
            "run.plan_approved",
            {"approval_id": str(approval.id)},
            actor_class="operator",
            actor_id=command.actor_id,
            occurred_at=self._clock.now(),
        )
        await work.commands.enqueue(
            run_id=run.id,
            command_type="prepare_worktree",
            idempotency_key=f"{run.id}:prepare-worktree:{transitioned.version}",
            payload={},
            expected_run_version=transitioned.version,
            actor_id=command.actor_id,
            available_at=self._clock.now(),
        )
        await work.commit()

    async def _committed_restart(
        self, command: CommandEnvelope, work: UnitOfWork, approval: Approval
    ) -> bool:
        if approval.invalidated_at is None:
            return False
        attempt = await work.executions.next_attempt(command.run_id, "plan")
        queued = await work.commands.get_by_idempotency_key(
            f"{command.run_id}:start-planning:{attempt}"
        )
        if (
            queued is None
            or queued.command_type != "start_planning"
            or queued.status is not CommandStatus.PENDING
            or queued.run_id != command.run_id
            or queued.actor_id != command.actor_id
            or queued.expected_run_version != command.expected_run_version + 1
            or queued.payload != {"semantic_attempt": attempt}
        ):
            return False
        source = await restart_source(work, queued, source_status=CommandStatus.LEASED)
        if source is None or source.id != command.id:
            return False
        events = [
            event
            for event in await work.events.list_for_version(
                command.run_id, queued.expected_run_version
            )
            if event.event_type == "approval.stale"
        ]
        return (
            len(events) == 1
            and events[0].payload.get("approval_id") == str(approval.id)
            and events[0].payload.get("approval_evidence_digest") == approval.evidence_digest
            and events[0].payload.get("approval_policy_version") == approval.policy_version
        )


class RequestPlanRevisionHandler:
    """Compatibility worker entry point; semantic revision service owns the transition."""

    def __init__(
        self,
        artifact_store: ArtifactStore,
        evidence_validator: PlanEvidenceValidator,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._service = PlanRevisionService(artifact_store, evidence_validator, clock=clock)

    async def __call__(self, command: CommandEnvelope, work: UnitOfWork) -> None:
        await self._service.execute(command, work)


def _approval_id(command: CommandEnvelope) -> UUID:
    if (
        command.command_type != "approve_plan"
        or command.status is not CommandStatus.LEASED
        or set(command.payload) != {"approval_id"}
    ):
        raise ApprovalCommandValidationError("approval command is invalid")
    try:
        return UUID(str(command.payload["approval_id"]))
    except TypeError, ValueError:
        raise ApprovalCommandValidationError("approval command is invalid") from None


__all__ = ["ApprovePlanHandler", "RequestPlanRevisionHandler"]
