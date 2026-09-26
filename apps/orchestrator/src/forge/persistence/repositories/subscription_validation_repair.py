"""Requeue failed validation without changing the dispatched acceptance source."""

from collections.abc import Callable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forge.application.ports.subscription_acceptance import (
    PreparedSubscriptionAcceptance,
    RetainedSubscriptionAcceptance,
)
from forge.application.ports.subscription_decisions import SubscriptionDecisionError
from forge.application.ports.subscription_validation import AcceptanceValidationRepair
from forge.domain.operation import canonical_digest
from forge.domain.subscription import HandoffStatus, TaskHandoff, encode_subscription_record
from forge.persistence.models.scheduling import SubscriptionScheduledTask, SubscriptionSchedulerRun
from forge.persistence.models.subscription import (
    SubscriptionAttempt,
    SubscriptionDecisionRecord,
    SubscriptionTask,
)
from forge.persistence.models.subscription_results import SubscriptionRepairDebit
from forge.persistence.repositories.runs import PostgresRunRepository
from forge.persistence.repositories.subscription import PostgresSubscriptionRepository
from forge.persistence.repositories.subscription_budget import PostgresSubscriptionBudgetRepository
from forge.persistence.repositories.subscription_validation import acceptance_validation_source


def _handoff(
    source: PreparedSubscriptionAcceptance | RetainedSubscriptionAcceptance,
    validation_digest: str,
    failed_checks: tuple[str, ...],
    repaired: bool,
) -> TaskHandoff:
    if (
        not failed_checks
        or len(set(failed_checks)) != len(failed_checks)
        or not set(failed_checks) <= {spec.name for spec in source.policy.required_checks}
    ):
        raise SubscriptionDecisionError("validation failure checks differ")
    return TaskHandoff(
        run_id=source.decision.run_id,
        task_id=source.decision.task_id,
        attempt_id=source.attempt_id,
        status=HandoffStatus.FAILED if repaired else HandoffStatus.REPAIRS_EXHAUSTED,
        summary="Controller final validation failed: "
        + ", ".join(failed_checks)
        + f". Validation evidence: {validation_digest}. "
        + (
            "Repair within the approved scope and obtain fresh candidate acceptance."
            if repaired
            else "Automatic repair budget is exhausted; human intervention is required."
        ),
    )


async def reject_acceptance_validation(
    session: AsyncSession,
    proposal: PreparedSubscriptionAcceptance,
    command_id: UUID,
    validation_digest: str,
    failed_checks: tuple[str, ...],
    repair_limit: int,
) -> AcceptanceValidationRepair:
    current, _ = await acceptance_validation_source(session, proposal.attempt_id)
    if (
        current != proposal
        or type(repair_limit) is not int
        or not 0 <= repair_limit <= proposal.policy.local_remediation_limit
    ):
        raise SubscriptionDecisionError("validation repair source changed")
    return await _reopen_candidate(
        session,
        proposal,
        key=f"validation-rejection:{command_id}",
        local_limit=repair_limit,
        handoff=lambda repaired: _handoff(proposal, validation_digest, failed_checks, repaired),
    )


async def _reopen_candidate(
    session: AsyncSession,
    proposal: PreparedSubscriptionAcceptance,
    *,
    key: str,
    local_limit: int | None,
    handoff: Callable[[bool], TaskHandoff],
) -> AcceptanceValidationRepair:
    """Caller must first prove current closed acceptance and the revision cause."""
    run = await PostgresRunRepository(session).get_for_update(proposal.decision.run_id)
    task = await session.get(SubscriptionTask, proposal.decision.task_id, populate_existing=True)
    scheduled = await session.get(
        SubscriptionScheduledTask, proposal.decision.task_id, populate_existing=True
    )
    scheduler = await session.get(
        SubscriptionSchedulerRun, proposal.decision.run_id, populate_existing=True
    )
    assert task is not None and scheduled is not None and scheduler is not None
    repaired = (
        (local_limit is None or run.local_remediation_count < local_limit)
        and scheduled.repairs < scheduled.max_repairs
        and await PostgresSubscriptionBudgetRepository(session).try_debit_repair(
            run.id, task.id, proposal.attempt_id
        )
    )
    await PostgresSubscriptionRepository(session).record_decision(
        handoff(repaired),
        idempotency_key=key,
    )
    if repaired:
        scheduled.repairs += 1
        task.state = scheduled.state = "queued"
        task.version += 1
        scheduler.candidate_state = "open"
        scheduler.candidate_epoch += 1
    await session.flush()
    return AcceptanceValidationRepair(
        repaired, task.version, scheduled.repairs, scheduler.candidate_epoch
    )


async def verify_acceptance_validation_rejection(
    session: AsyncSession,
    source: RetainedSubscriptionAcceptance,
    command_id: UUID,
    validation_digest: str,
    failed_checks: tuple[str, ...],
    receipt: AcceptanceValidationRepair,
) -> None:
    await _verify_reopening(
        session,
        source,
        f"validation-rejection:{command_id}",
        _handoff(source, validation_digest, failed_checks, receipt.repaired),
        receipt,
    )


async def _verify_reopening(
    session: AsyncSession,
    source: RetainedSubscriptionAcceptance,
    key: str,
    handoff: TaskHandoff,
    receipt: AcceptanceValidationRepair,
) -> None:
    record = await session.scalar(
        select(SubscriptionDecisionRecord).where(
            SubscriptionDecisionRecord.run_id == source.decision.run_id,
            SubscriptionDecisionRecord.idempotency_key == key,
        )
    )
    attempt = await session.get(SubscriptionAttempt, source.attempt_id)
    task = await session.get(SubscriptionTask, source.decision.task_id)
    scheduled = await session.get(SubscriptionScheduledTask, source.decision.task_id)
    scheduler = await session.get(SubscriptionSchedulerRun, source.decision.run_id)
    if (
        record is None
        or record.record_type != "TaskHandoff"
        or record.attempt_id != source.attempt_id
        or record.task_row_id != source.decision.task_id
        or canonical_digest(record.payload) != canonical_digest(encode_subscription_record(handoff))
        or attempt is None
        or attempt.task_version is None
        or task is None
        or scheduled is None
        or scheduler is None
        or receipt.primary_task_version != attempt.task_version + 2 + int(receipt.repaired)
        or task.version < receipt.primary_task_version
        or scheduled.repairs < receipt.scheduled_repairs
        or receipt.candidate_epoch != source.review.candidate_epoch + int(receipt.repaired)
        or scheduler.candidate_epoch < receipt.candidate_epoch
        or (
            receipt.repaired
            and (
                receipt.scheduled_repairs < 1
                or receipt.scheduled_repairs > scheduled.max_repairs
                or await session.get(SubscriptionRepairDebit, source.attempt_id) is None
            )
        )
    ):
        raise SubscriptionDecisionError("validation repair history differs")
