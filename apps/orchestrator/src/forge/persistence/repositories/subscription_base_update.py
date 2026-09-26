"""Reserve a primary repair before effects and reopen only after base adoption."""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from forge.application.ports.subscription_acceptance import (
    PreparedSubscriptionAcceptance,
    RetainedSubscriptionAcceptance,
)
from forge.application.ports.subscription_base_update import BaseUpdateReservation
from forge.application.ports.subscription_decisions import SubscriptionDecisionError
from forge.application.ports.subscription_validation import AcceptanceValidationRepair
from forge.domain.artifact import validate_artifact_digest
from forge.domain.subscription import HandoffStatus, TaskHandoff
from forge.persistence.models.scheduling import SubscriptionScheduledTask, SubscriptionSchedulerRun
from forge.persistence.models.subscription import SubscriptionAttempt, SubscriptionTask
from forge.persistence.models.subscription_results import SubscriptionRepairDebit
from forge.persistence.repositories.subscription import PostgresSubscriptionRepository
from forge.persistence.repositories.subscription_budget import PostgresSubscriptionBudgetRepository
from forge.persistence.repositories.subscription_validation import acceptance_remote_source
from forge.persistence.repositories.subscription_validation_repair import (
    _reopen_candidate,
    _verify_reopening,
)


async def reserve_acceptance_base(
    session: AsyncSession, proposal: PreparedSubscriptionAcceptance
) -> BaseUpdateReservation | None:
    current, _ = await acceptance_remote_source(session, proposal.attempt_id)
    scheduled = await session.get(SubscriptionScheduledTask, proposal.decision.task_id)
    contract = await PostgresSubscriptionRepository(session).get_task(
        proposal.decision.run_id, proposal.decision.task_id
    )
    if (
        current != proposal
        or scheduled is None
        or scheduled.max_repairs != contract.max_repairs
        or await session.get(SubscriptionRepairDebit, proposal.attempt_id) is not None
    ):
        raise SubscriptionDecisionError("base update repair reservation source differs")
    if scheduled.repairs >= scheduled.max_repairs or not (
        await PostgresSubscriptionBudgetRepository(session).try_debit_repair(
            proposal.decision.run_id, proposal.decision.task_id, proposal.attempt_id
        )
    ):
        return None
    return BaseUpdateReservation(
        proposal.task_version, scheduled.repairs, proposal.review.candidate_epoch
    )


async def verify_base_reservation(
    session: AsyncSession,
    source: PreparedSubscriptionAcceptance | RetainedSubscriptionAcceptance,
    reservation: BaseUpdateReservation,
    *,
    live: bool = False,
) -> None:
    attempt = await session.get(SubscriptionAttempt, source.attempt_id)
    task = await session.get(SubscriptionTask, source.decision.task_id)
    scheduled = await session.get(SubscriptionScheduledTask, source.decision.task_id)
    scheduler = await session.get(SubscriptionSchedulerRun, source.decision.run_id)
    debit = await session.get(SubscriptionRepairDebit, source.attempt_id)
    contract = await PostgresSubscriptionRepository(session).get_task(
        source.decision.run_id, source.decision.task_id
    )
    if (
        attempt is None
        or attempt.task_version is None
        or task is None
        or scheduled is None
        or scheduler is None
        or debit is None
        or reservation.primary_task_version != attempt.task_version + 2
        or reservation.candidate_epoch != source.review.candidate_epoch
        or reservation.scheduled_repairs >= contract.max_repairs
        or scheduled.max_repairs != contract.max_repairs
        or task.version < reservation.primary_task_version
        or scheduled.repairs < reservation.scheduled_repairs
        or scheduler.candidate_epoch < reservation.candidate_epoch
        or (
            live
            and (
                task.version != reservation.primary_task_version
                or scheduled.repairs != reservation.scheduled_repairs
                or scheduler.candidate_epoch != reservation.candidate_epoch
            )
        )
    ):
        raise SubscriptionDecisionError("base update repair reservation differs")


def _handoff(
    source: PreparedSubscriptionAcceptance | RetainedSubscriptionAcceptance,
    pr_digest: str,
    target: str,
    update_id: UUID,
    adoption_id: UUID,
) -> TaskHandoff:
    validate_artifact_digest(pr_digest)
    if len(target) != 40 or any(c not in "0123456789abcdef" for c in target):
        raise SubscriptionDecisionError("adopted base identity differs")
    return TaskHandoff(
        run_id=source.decision.run_id,
        task_id=source.decision.task_id,
        attempt_id=source.attempt_id,
        status=HandoffStatus.FAILED,
        summary=f"The controlled base update adopted {target}. "
        + f"Previous PR evidence: {pr_digest}. Update operation: {update_id}; "
        + f"local adoption operation: {adoption_id}. "
        + "Inspect the adopted candidate within approved scope and obtain fresh snapshot "
        + "evidence, review selection and acceptance before final checks and publication.",
    )


async def reopen_acceptance_base(
    session: AsyncSession,
    proposal: PreparedSubscriptionAcceptance,
    command_id: UUID,
    pr_digest: str,
    target: str,
    update_id: UUID,
    adoption_id: UUID,
    reservation: BaseUpdateReservation,
) -> AcceptanceValidationRepair:
    current, _ = await acceptance_remote_source(session, proposal.attempt_id)
    if current != proposal:
        raise SubscriptionDecisionError("base adoption acceptance source changed")
    await verify_base_reservation(session, proposal, reservation, live=True)
    receipt = await _reopen_candidate(
        session,
        proposal,
        key=f"base-adoption:{command_id}",
        local_limit=None,
        handoff=lambda _: _handoff(proposal, pr_digest, target, update_id, adoption_id),
    )
    if not receipt.repaired:
        raise SubscriptionDecisionError("reserved base adoption repair is unavailable")
    return receipt


async def verify_acceptance_base(
    session: AsyncSession,
    source: RetainedSubscriptionAcceptance,
    command_id: UUID,
    pr_digest: str,
    target: str,
    update_id: UUID,
    adoption_id: UUID,
    reservation: BaseUpdateReservation,
    receipt: AcceptanceValidationRepair,
) -> None:
    await verify_base_reservation(session, source, reservation)
    if (
        not receipt.repaired
        or receipt.primary_task_version != reservation.primary_task_version + 1
        or receipt.scheduled_repairs != reservation.scheduled_repairs + 1
        or receipt.candidate_epoch != reservation.candidate_epoch + 1
    ):
        raise SubscriptionDecisionError("base adoption repair differs")
    await _verify_reopening(
        session,
        source,
        f"base-adoption:{command_id}",
        _handoff(source, pr_digest, target, update_id, adoption_id),
        receipt,
    )
