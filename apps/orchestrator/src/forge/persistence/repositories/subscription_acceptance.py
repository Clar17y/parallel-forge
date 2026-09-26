"""Current authority for inspecting a prepared final acceptance intent."""

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forge.application.ports.subscription_acceptance import PreparedSubscriptionAcceptance
from forge.application.ports.subscription_acceptance_receipts import AcceptanceReceiptClaimError
from forge.application.ports.subscription_candidate import CandidateInspection
from forge.application.ports.subscription_decisions import SubscriptionDecisionError
from forge.application.ports.subscription_execution import SubscriptionSettlement
from forge.application.ports.worktrees import GitWorkingTreeSnapshot, ManagedWorktree
from forge.domain.approval import ApprovalGate
from forge.domain.operation import canonical_digest
from forge.domain.policy import ProjectPolicy
from forge.domain.resource import WorktreeIdentity
from forge.domain.run import RunState
from forge.domain.subscription import AcceptDecision, decode_subscription_record
from forge.domain.subscription_execution import SUBSCRIPTION_WORK_STATES
from forge.persistence.models.scheduling import (
    SubscriptionScheduledEffect,
    SubscriptionScheduledTask,
    SubscriptionSchedulerRun,
)
from forge.persistence.models.subscription import SubscriptionAttempt, SubscriptionTask
from forge.persistence.models.subscription_results import SubscriptionAttemptResult
from forge.persistence.repositories.commands import PostgresCommandRepository
from forge.persistence.repositories.projects import PostgresProjectRepository
from forge.persistence.repositories.runs import PostgresRunRepository
from forge.persistence.repositories.scheduling import PostgresSchedulingRepository
from forge.persistence.repositories.subscription_review import (
    candidate_review_evidence,
    historical_candidate_review_evidence,
)


async def acceptance_proposal(
    session: AsyncSession, attempt_id: UUID
) -> PreparedSubscriptionAcceptance:
    return await _acceptance_proposal(session, attempt_id, None)


async def acceptance_evidence_proposal(
    session: AsyncSession, attempt_id: UUID
) -> PreparedSubscriptionAcceptance:
    """Re-prove the stopped acceptance source during controller validation."""
    return await _acceptance_proposal(session, attempt_id, RunState.VALIDATING)


async def acceptance_revision_proposal(
    session: AsyncSession, attempt_id: UUID
) -> PreparedSubscriptionAcceptance:
    """Current closed primary authority at the human PR gate, not historical proof."""
    return await _acceptance_proposal(session, attempt_id, RunState.AWAITING_PR_APPROVAL)


async def acceptance_remote_proposal(
    session: AsyncSession, attempt_id: UUID, *, allow_paused: bool = False
) -> PreparedSubscriptionAcceptance:
    """Current closed source after the monitor admitted a remote repair."""
    return await _acceptance_proposal(
        session, attempt_id, RunState.REMEDIATING, allow_paused=allow_paused
    )


async def _acceptance_proposal(
    session: AsyncSession, attempt_id: UUID, phase: RunState | None, *, allow_paused: bool = False
) -> PreparedSubscriptionAcceptance:
    from forge.persistence.repositories.subscription_decisions import (
        PostgresSubscriptionDecisionRepository,
    )

    result = await session.get(SubscriptionAttemptResult, attempt_id, populate_existing=True)
    if result is None or result.disposition != "acceptance_prepared":
        raise SubscriptionDecisionError("final acceptance intent is not prepared")
    await PostgresSubscriptionDecisionRepository(session).prepare_acceptance(attempt_id)
    attempt = await session.get(SubscriptionAttempt, attempt_id, populate_existing=True)
    assert attempt is not None
    run = await PostgresRunRepository(session).get_for_update(attempt.run_id)
    task = await session.get(SubscriptionTask, attempt.task_row_id, populate_existing=True)
    scheduled = await session.get(
        SubscriptionScheduledTask, attempt.task_row_id, populate_existing=True
    )
    scheduler = await session.get(SubscriptionSchedulerRun, attempt.run_id, populate_existing=True)
    # Resume may inspect an untouched remote source while paused. Reopening and
    # every effect admission still call this verifier with its default phase fence.
    phase_matches = (
        run.state is phase if phase is not None else run.state in SUBSCRIPTION_WORK_STATES
    ) or (
        allow_paused
        and phase is RunState.REMEDIATING
        and run.state is RunState.PAUSED
        and run.suspended_state is phase
    )
    if (
        not phase_matches
        or (
            (run.pending_gate is not ApprovalGate.PR or not run.pending_evidence_digest)
            if phase is RunState.AWAITING_PR_APPROVAL
            else run.pending_gate is not None
        )
        or not run.worktree_path
        or not run.base_sha
        or run.policy_version is None
        or task is None
        or scheduled is None
        or scheduler is None
        or task.run_id != run.id
        or scheduled.run_id != run.id
        or task.state != "blocked"
        or scheduled.state != "blocked"
        or task.pause_requested
        or task.cancel_requested
        or scheduled.pause_requested
        or scheduled.cancel_requested
        or scheduled.lease_owner is not None
        or scheduled.lease_expires_at is not None
        or attempt.task_version is None
        or task.version != attempt.task_version + 2
        or canonical_digest(task.payload) != attempt.task_digest
        or not scheduler.admitted
        or scheduler.candidate_state != "closed"
        or scheduler.candidate_epoch != attempt.candidate_epoch
        or not await PostgresSchedulingRepository(session)._is_coordinator(scheduled)
        or await PostgresCommandRepository(session=session).has_pending_current_control_stop(
            run_id=run.id, expected_run_version=run.version
        )
    ):
        raise SubscriptionDecisionError("final acceptance source is no longer current")
    pending_task = await session.scalar(
        select(SubscriptionTask.id)
        .where(
            SubscriptionTask.run_id == run.id,
            SubscriptionTask.id != task.id,
            SubscriptionTask.state != "terminal",
        )
        .limit(1)
    )
    pending_schedule = await session.scalar(
        select(SubscriptionScheduledTask.task_id)
        .where(
            SubscriptionScheduledTask.run_id == run.id,
            SubscriptionScheduledTask.task_id != task.id,
            SubscriptionScheduledTask.state != "terminal",
        )
        .limit(1)
    )
    pending_effect = await session.scalar(
        select(SubscriptionScheduledEffect.id)
        .where(
            SubscriptionScheduledEffect.run_id == run.id,
            SubscriptionScheduledEffect.state.in_(("admitted", "reconciling")),
        )
        .limit(1)
    )
    if (
        pending_task is not None
        or pending_schedule is not None
        or pending_effect is not None
        or await PostgresSchedulingRepository(session)._has_exclusive_effect_barrier(scheduled)
    ):
        raise SubscriptionDecisionError("final acceptance candidate is not quiescent")
    projects = PostgresProjectRepository(session)
    project = await projects.get(run.project_id)
    record = await projects.get_policy(run.project_id, run.policy_version)
    policy = ProjectPolicy.model_validate(record.document)
    if (
        record.document_schema_version != 1
        or project.current_policy_version != run.policy_version
        or (policy.id, policy.version) != (run.project_id, run.policy_version)
        or record.policy_digest != canonical_digest(record.document)
    ):
        raise SubscriptionDecisionError("final acceptance policy differs")
    worktree = ManagedWorktree(
        identity=WorktreeIdentity.for_run(
            run.project_id, run.id, run.branch_name or "", policy.database.enabled
        ),
        path=Path(run.worktree_path),
        base_sha=run.base_sha,
    )
    if scheduled.worktree_id != worktree.identity.worktree_name:
        raise SubscriptionDecisionError("final acceptance worktree differs")
    if run.state is RunState.PAUSED:
        assert allow_paused and attempt.candidate_epoch is not None
        review = await historical_candidate_review_evidence(
            session, run.id, task.id, attempt.candidate_epoch
        )
        if review is None:
            raise SubscriptionDecisionError("paused remote candidate review source differs")
    else:
        review = await candidate_review_evidence(session, run.id, task.id, _phase=run.state)
    if review.candidate.base_sha != worktree.base_sha:
        raise SubscriptionDecisionError("final acceptance candidate base differs")
    value = result.result_payload["decision"]
    assert isinstance(value, Mapping) and result.application_payload is not None
    decision = decode_subscription_record(value)
    assert isinstance(decision, AcceptDecision)
    receipt = result.application_payload
    inspection = (
        CandidateInspection.from_payload(receipt["observation"])
        if "observation" in receipt
        else None
    )
    return PreparedSubscriptionAcceptance(
        attempt_id,
        decision,
        result.result_digest,
        review,
        task.version,
        run.version,
        policy,
        worktree,
        inspection,
    )


async def record_acceptance_inspection(
    session: AsyncSession,
    proposal: PreparedSubscriptionAcceptance,
    snapshot: GitWorkingTreeSnapshot,
) -> CandidateInspection:
    current = await acceptance_proposal(session, proposal.attempt_id)
    if replace(current, inspection=None) != replace(proposal, inspection=None):
        raise SubscriptionDecisionError("acceptance source changed during inspection")
    observed = CandidateInspection.from_snapshot(snapshot)
    if observed.base_sha != current.worktree.base_sha:
        raise SubscriptionDecisionError("acceptance observation base differs")
    if current.inspection is not None:
        if observed != current.inspection:
            raise SubscriptionDecisionError("acceptance observation replay differs")
        return current.inspection
    result = await session.get(SubscriptionAttemptResult, proposal.attempt_id)
    assert result is not None and result.application_payload is not None
    result.application_payload = {**result.application_payload, "observation": observed.payload()}
    result.application_digest = canonical_digest(result.application_payload)
    await session.flush()
    return observed


async def reject_acceptance_mismatch(
    session: AsyncSession,
    proposal: PreparedSubscriptionAcceptance,
    snapshot: GitWorkingTreeSnapshot | None = None,
) -> SubscriptionSettlement:
    from forge.persistence.repositories.subscription_decisions import (
        PostgresSubscriptionDecisionRepository,
    )

    prior = await PostgresSubscriptionDecisionRepository(session).prepare_acceptance(
        proposal.attempt_id
    )
    if not prior.accepted:
        return prior
    current = await acceptance_proposal(session, proposal.attempt_id)
    if replace(current, inspection=None) != replace(proposal, inspection=None) or (
        snapshot is None and current.inspection != proposal.inspection
    ):
        raise SubscriptionDecisionError("acceptance source changed before rejection")
    observed = (
        CandidateInspection.from_snapshot(snapshot) if snapshot is not None else current.inspection
    )
    if (
        observed is None
        or observed.base_sha != current.worktree.base_sha
        or observed == current.review.candidate
    ):
        raise SubscriptionDecisionError("acceptance has no observed candidate mismatch")
    return await _reject_prepared_acceptance(
        session,
        current,
        {"reason": "candidate_identity_differs", "observation": observed.payload()},
        reopen=True,
    )


async def reject_acceptance_receipt_claims(
    session: AsyncSession, attempt_id: UUID
) -> SubscriptionSettlement:
    from forge.persistence.repositories.subscription_acceptance_receipts import _receipt_sources
    from forge.persistence.repositories.subscription_decisions import (
        PostgresSubscriptionDecisionRepository,
    )

    prior = await PostgresSubscriptionDecisionRepository(session).prepare_acceptance(attempt_id)
    if not prior.accepted:
        return prior
    proposal = await acceptance_proposal(session, attempt_id)
    if proposal.inspection is not None and proposal.inspection != proposal.review.candidate:
        raise SubscriptionDecisionError("candidate mismatch requires candidate repair")
    try:
        await _receipt_sources(session, proposal)
    except AcceptanceReceiptClaimError as issue:
        return await _reject_prepared_acceptance(
            session,
            proposal,
            {
                "reason": "receipt_claim_invalid",
                "candidate_epoch": proposal.review.candidate_epoch,
                **issue.payload(),
            },
            reopen=False,
            receipt_issue=issue,
        )
    raise SubscriptionDecisionError("acceptance has no invalid receipt claim")


async def _reject_prepared_acceptance(
    session: AsyncSession,
    proposal: PreparedSubscriptionAcceptance,
    rejection: Mapping[str, object],
    *,
    reopen: bool,
    receipt_issue: AcceptanceReceiptClaimError | None = None,
) -> SubscriptionSettlement:
    """Caller has proved a definite contradiction under current acceptance authority."""
    from forge.persistence.repositories.subscription import PostgresSubscriptionRepository
    from forge.persistence.repositories.subscription_budget import (
        PostgresSubscriptionBudgetRepository,
    )
    from forge.persistence.repositories.subscription_decisions import _rejection_handoff

    attempt = await session.get(SubscriptionAttempt, proposal.attempt_id)
    assert attempt is not None
    task = await session.get(SubscriptionTask, attempt.task_row_id)
    scheduled = await session.get(SubscriptionScheduledTask, attempt.task_row_id)
    scheduler = await session.get(SubscriptionSchedulerRun, attempt.run_id)
    result = await session.get(SubscriptionAttemptResult, attempt.id)
    assert task is not None and scheduled is not None and scheduler is not None
    assert result is not None and result.application_payload is not None
    repair = (
        scheduled.repairs < scheduled.max_repairs
        and await PostgresSubscriptionBudgetRepository(session).try_debit_repair(
            task.run_id, task.id, attempt.id
        )
    )
    await PostgresSubscriptionRepository(session).record_decision(
        _rejection_handoff(attempt, repair=repair, acceptance=True, receipt_issue=receipt_issue),
        idempotency_key=f"decision-rejection:{attempt.id}",
    )
    if repair:
        scheduled.repairs += 1
    task.state = scheduled.state = "queued" if repair else "terminal"
    task.version += 1
    rejection = {**rejection, "repair": repair}
    if reopen:
        scheduler.candidate_state = "open"
        scheduler.candidate_epoch += 1
        rejection = {**rejection, "reopened_epoch": scheduler.candidate_epoch}
    result.accepted = False
    result.disposition = "acceptance_repair_queued" if repair else "acceptance_rejected"
    result.application_payload = {
        **result.application_payload,
        "rejection": dict(rejection),
    }
    result.application_digest = canonical_digest(result.application_payload)
    await session.flush()
    return SubscriptionSettlement(False, result.disposition)
