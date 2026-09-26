"""Read exact current review evidence without fabricating legacy executions."""

from collections.abc import Mapping
from dataclasses import replace
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forge.application.ports.subscription_candidate import CandidateInspection
from forge.application.ports.subscription_decisions import SubscriptionDecisionError
from forge.application.ports.subscription_review import CandidateReviewEvidence
from forge.domain.approval import ApprovalGate
from forge.domain.run import RunState
from forge.domain.subscription import (
    LogicalTaskContract,
    ReviewedTaskHandoff,
    ReviewSelection,
    decode_subscription_record,
)
from forge.domain.subscription_execution import SUBSCRIPTION_WORK_STATES
from forge.persistence.models.scheduling import SubscriptionSchedulerRun
from forge.persistence.models.subscription import SubscriptionAttempt, SubscriptionTask
from forge.persistence.models.subscription_results import SubscriptionAttemptResult
from forge.persistence.repositories.runs import PostgresRunRepository
from forge.persistence.repositories.subscription import PostgresSubscriptionRepository


async def candidate_review_evidence(
    session: AsyncSession,
    run_id: UUID,
    primary_task_id: UUID,
    *,
    _phase: RunState | None = None,
) -> CandidateReviewEvidence:
    if _phase is not None and _phase not in SUBSCRIPTION_WORK_STATES | {
        RunState.VALIDATING,
        RunState.AWAITING_PR_APPROVAL,
    }:
        raise SubscriptionDecisionError("candidate review phase is invalid")
    run = await PostgresRunRepository(session).get_for_update(run_id)
    scheduler = await session.get(
        SubscriptionSchedulerRun, run_id, with_for_update=True, populate_existing=True
    )
    envelope = await PostgresSubscriptionRepository(session).envelope_for_run(run_id)
    if (
        (
            run.state is not _phase
            if _phase is not None
            else run.state not in SUBSCRIPTION_WORK_STATES
        )
        or (
            (run.pending_gate is not ApprovalGate.PR or not run.pending_evidence_digest)
            if _phase is RunState.AWAITING_PR_APPROVAL
            else run.pending_gate is not None
        )
        or scheduler is None
        or not scheduler.admitted
        or scheduler.candidate_state != "closed"
        or envelope is None
        or run.policy_version != envelope.safety_policy_version
    ):
        raise SubscriptionDecisionError("candidate review evidence is not current")
    evidence = await historical_candidate_review_evidence(
        session,
        run_id,
        primary_task_id,
        scheduler.candidate_epoch,
    )
    assert evidence is not None
    return evidence


async def historical_candidate_review_evidence(
    session: AsyncSession,
    run_id: UUID,
    primary_task_id: UUID,
    candidate_epoch: int,
    *,
    allow_missing: bool = False,
) -> CandidateReviewEvidence | None:
    """Historical source proof only; caller must separately fence current authority."""
    from forge.persistence.repositories.subscription_decisions import (
        PostgresSubscriptionDecisionRepository,
    )

    await PostgresRunRepository(session).get_for_update(run_id)
    sources = (
        await session.scalars(
            select(SubscriptionAttemptResult.attempt_id)
            .join(
                SubscriptionAttempt, SubscriptionAttempt.id == SubscriptionAttemptResult.attempt_id
            )
            .where(
                SubscriptionAttempt.run_id == run_id,
                SubscriptionAttempt.task_row_id == primary_task_id,
                SubscriptionAttempt.candidate_epoch == candidate_epoch - 1,
                SubscriptionAttemptResult.disposition == "review_selected",
            )
            .limit(2)
        )
    ).all()
    if not sources and allow_missing:
        return None
    if len(sources) != 1:
        raise SubscriptionDecisionError("candidate review selection source differs")
    decisions = PostgresSubscriptionDecisionRepository(session)
    await decisions.prepare_review_selection(sources[0])
    source = await session.get(SubscriptionAttemptResult, sources[0], populate_existing=True)
    assert (
        source is not None
        and source.application_payload is not None
        and source.application_digest is not None
    )
    receipt = source.application_payload
    selection_value = source.result_payload["decision"]
    if not isinstance(selection_value, Mapping):
        raise SubscriptionDecisionError("candidate review selection shape differs")
    selection = decode_subscription_record(selection_value)
    assert isinstance(selection, ReviewSelection)
    if receipt["candidate_epoch"] != candidate_epoch:
        raise SubscriptionDecisionError("candidate review selection epoch differs")
    candidate = CandidateInspection.from_payload(receipt["observation"])
    binding = CandidateReviewEvidence(
        run_id=run_id,
        primary_task_id=primary_task_id,
        candidate_epoch=candidate_epoch,
        candidate=candidate,
        selection=selection,
        selection_attempt_id=sources[0],
        selection_result_digest=source.result_digest,
        selection_application_digest=source.application_digest,
    )
    if not selection.review_required:
        return binding
    selected = receipt["selection"]
    assert isinstance(selected, dict)
    child = decode_subscription_record(selected["review_task"])
    assert isinstance(child, LogicalTaskContract)
    task = await session.get(SubscriptionTask, child.task_id, populate_existing=True)
    attempt = await session.scalar(
        select(SubscriptionAttempt)
        .where(
            SubscriptionAttempt.run_id == run_id,
            SubscriptionAttempt.task_row_id == child.task_id,
        )
        .order_by(SubscriptionAttempt.attempt_number.desc())
        .limit(1)
    )
    if (
        task is None
        or task.state != "terminal"
        or attempt is None
        or attempt.status != "terminal"
        or attempt.candidate_epoch != candidate_epoch
    ):
        raise SubscriptionDecisionError("selected reviewer has not completed")
    settled = await decisions.handoff_replay(attempt.id)
    if settled is None or not settled.accepted or settled.disposition != "handoff_completed":
        raise SubscriptionDecisionError("selected reviewer has no completed evidence")
    result = await session.get(SubscriptionAttemptResult, attempt.id, populate_existing=True)
    assert (
        result is not None
        and result.application_payload is not None
        and result.application_digest is not None
    )
    handoff_value = result.result_payload["decision"]
    if not isinstance(handoff_value, Mapping):
        raise SubscriptionDecisionError("candidate review handoff shape differs")
    handoff = decode_subscription_record(handoff_value)
    if (
        not isinstance(handoff, ReviewedTaskHandoff)
        or result.application_payload.get("selected_candidate") != candidate.payload()
        or handoff.candidate_tree_digest != candidate.tree_digest
        or (handoff.candidate_commit is not None and handoff.candidate_commit != candidate.head_sha)
    ):
        raise SubscriptionDecisionError("selected reviewer report binding differs")
    return replace(
        binding,
        review_attempt_id=attempt.id,
        review_handoff=handoff,
        review_result_digest=result.result_digest,
        review_application_digest=result.application_digest,
    )
