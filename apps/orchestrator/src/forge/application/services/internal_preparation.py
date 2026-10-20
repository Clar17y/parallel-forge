"""Shared provenance for internal (non-human-approval) plan preparation.

Internal preparation is authorized by the authenticated owner action that
originated the run (creation, or a later workflow update), never by an
invented worker identity.  The worker only executes it; durable evidence
keeps worker execution, request authorization, and actual human plan
approval distinguishable.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID, uuid4

from forge.application.ports.unit_of_work import UnitOfWork
from forge.application.services.plan_evidence import PlanEvidenceValidator
from forge.domain.approval import ApprovalGate
from forge.domain.event import RunEvent
from forge.domain.plan import material_questions
from forge.domain.run import RunSnapshot, RunState
from forge.domain.task_usage_contract import SubjectKey

INTERNAL_PREPARATION_AUTHORITY = "internal_preparation"
INTERNAL_PREPARATION_EVENT = "run.plan_internally_prepared"


class InternalPreparationError(RuntimeError):
    """Internal preparation lacks durable originating-owner provenance."""


def originating_operator_id(events: Sequence[RunEvent]) -> UUID | None:
    """Return the authenticated operator who created the run, if recorded."""
    created = [event for event in events if event.event_type == "run.created"]
    if len(created) != 1:
        return None
    event = created[0]
    if event.actor_class != "operator" or event.actor_id is None or event.run_version != 0:
        return None
    return event.actor_id


async def record_internal_preparation_approval(
    work: UnitOfWork,
    run: RunSnapshot,
    *,
    evidence_digest: str,
    authorized_by: UUID,
    authorization: str,
) -> tuple[UUID, UUID, dict[str, object]]:
    """Create the plan preparation record bound to the originating owner.

    Returns ``(approval_id, originating_actor_id, event_payload)``.  The
    caller performs its state transition with the returned payload and then
    enqueues ``prepare_worktree`` for the originating actor.
    """
    events = await work.events.list_after(run.id, 0)
    originator = originating_operator_id(events)
    if originator is None or run.policy_version is None:
        raise InternalPreparationError(
            "internal preparation requires recorded run-creation provenance"
        )
    approval_id = uuid4()
    await work.auth.create_approval(
        id=approval_id,
        run_id=run.id,
        gate="plan",
        evidence_digest=evidence_digest,
        run_version=run.version,
        policy_version=run.policy_version,
        authenticated_actor_id=originator,
    )
    payload: dict[str, object] = {
        "approval_id": str(approval_id),
        "authority": INTERNAL_PREPARATION_AUTHORITY,
        "originating_actor_id": str(originator),
        "authorized_by": str(authorized_by),
        "owner_action": authorization,
    }
    return approval_id, originator, payload


async def enqueue_prepare_worktree(
    work: UnitOfWork, *, run_id: UUID, run_version: int, actor_id: UUID
) -> None:
    await work.commands.enqueue(
        run_id=run_id,
        command_type="prepare_worktree",
        idempotency_key=f"{run_id}:prepare-worktree:{run_version}",
        payload={},
        expected_run_version=run_version,
        actor_id=actor_id,
    )


__all__ = [
    "INTERNAL_PREPARATION_AUTHORITY",
    "INTERNAL_PREPARATION_EVENT",
    "InternalPreparationError",
    "enqueue_prepare_worktree",
    "originating_operator_id",
    "record_internal_preparation_approval",
]


async def continue_pending_plan(
    work: UnitOfWork,
    run: RunSnapshot,
    *,
    validator: PlanEvidenceValidator,
    authorized_by: UUID,
    authorization: str,
    continuation_command_id: UUID | None = None,
) -> RunSnapshot:
    """Continue stored ordinary intent only when all current fences permit it."""
    if (
        run.state is not RunState.AWAITING_PLAN_APPROVAL
        or run.pending_gate is not ApprovalGate.PLAN
        or run.workflow is None
        or run.workflow.review_plan_first
    ):
        return run
    if await work.commands.has_pending_current_control_stop(
        run_id=run.id, expected_run_version=run.version
    ):
        return run
    if (
        await work.task_usage_checkpoints.active_for_subject(
            SubjectKey(project_id=run.project_id, kind="run", subject_id=run.id)
        )
        is not None
    ):
        return run
    evidence = await validator.validate(work, run.id)
    plan = await validator.load_plan(work, run.id)
    if material_questions(plan):
        return run
    if run.pending_evidence_digest is None:
        raise InternalPreparationError("pending plan evidence is missing")
    _, originator, payload = await record_internal_preparation_approval(
        work,
        run,
        evidence_digest=run.pending_evidence_digest,
        authorized_by=authorized_by,
        authorization=authorization,
    )
    if continuation_command_id is not None:
        payload["continuation_command_id"] = str(continuation_command_id)
    prepared = await work.runs.approve_plan(
        run.id,
        run.version,
        evidence,
        INTERNAL_PREPARATION_EVENT,
        payload,
        actor_class="worker",
        actor_id=None,
    )
    await enqueue_prepare_worktree(
        work,
        run_id=run.id,
        run_version=prepared.version,
        actor_id=originator,
    )
    return prepared
