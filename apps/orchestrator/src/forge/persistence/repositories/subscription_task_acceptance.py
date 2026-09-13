"""Primary acknowledgment of a child's retained, verified handoff.

This proof concerns a historical bounded outcome, not the current integrated tree.
The caller owns the run lock and separately proves the accepting primary's source
and current authority. Replaying an acknowledgment never changes child state.
"""

from collections.abc import Mapping
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forge.application.ports.subscription_decisions import SubscriptionDecisionError
from forge.domain.operation import canonical_digest
from forge.domain.subscription import (
    AcceptDecision,
    LogicalTaskContract,
    TaskHandoff,
    decode_subscription_record,
)
from forge.persistence.models.scheduling import SubscriptionScheduledTask
from forge.persistence.models.subscription import (
    SubscriptionAttempt,
    SubscriptionDecisionRecord,
    SubscriptionTask,
)
from forge.persistence.models.subscription_results import SubscriptionAttemptResult


async def task_acceptance_payload(
    session: AsyncSession,
    primary: LogicalTaskContract,
    decision: AcceptDecision,
    result: SubscriptionAttemptResult,
) -> dict[str, object] | None:
    from forge.persistence.repositories.subscription_decisions import (
        PostgresSubscriptionDecisionRepository,
    )

    if result.disposition in {"decision_repair_queued", "decision_rejected"}:
        return None
    replay = result.disposition == "task_accepted"
    if replay:
        try:
            assert result.application_payload is not None
            value = result.application_payload["handoff_attempt_id"]
            if not isinstance(value, str) or str(UUID(value)) != value:
                raise ValueError
            attempt = await session.get(SubscriptionAttempt, UUID(value), populate_existing=True)
        except AssertionError, KeyError, TypeError, ValueError:
            raise SubscriptionDecisionError("task acceptance source binding differs") from None
    else:
        task = await session.get(SubscriptionTask, decision.task_id, populate_existing=True)
        scheduled = await session.get(
            SubscriptionScheduledTask, decision.task_id, populate_existing=True
        )
        if (
            task is None
            or scheduled is None
            or task.run_id != primary.run_id
            or scheduled.run_id != primary.run_id
            or task.parent_task_id != primary.task_id
            or scheduled.parent_task_id != primary.task_id
            or task.state != "terminal"
            or scheduled.state != "terminal"
            or task.pause_requested
            or task.cancel_requested
            or scheduled.pause_requested
            or scheduled.cancel_requested
            or scheduled.lease_owner is not None
            or scheduled.lease_expires_at is not None
        ):
            return None
        attempt = await session.scalar(
            select(SubscriptionAttempt)
            .where(
                SubscriptionAttempt.run_id == primary.run_id,
                SubscriptionAttempt.task_row_id == decision.task_id,
            )
            .order_by(SubscriptionAttempt.attempt_number.desc())
            .limit(1)
        )
        if attempt is not None and canonical_digest(task.payload) != attempt.task_digest:
            raise SubscriptionDecisionError("task acceptance target contract differs")
    if (
        attempt is None
        or attempt.run_id != primary.run_id
        or attempt.task_row_id != decision.task_id
        or attempt.status != "terminal"
    ):
        return None
    settled = await PostgresSubscriptionDecisionRepository(session).handoff_replay(attempt.id)
    if settled is None or not settled.accepted or settled.disposition != "handoff_completed":
        return None
    source = await session.get(SubscriptionAttemptResult, attempt.id, populate_existing=True)
    assert source is not None and source.application_payload is not None
    context = source.result_payload["proposal_context"]
    assert isinstance(context, dict)
    contract_value, handoff_value = context["task"], source.result_payload["decision"]
    assert isinstance(contract_value, Mapping) and isinstance(handoff_value, Mapping)
    task_contract = decode_subscription_record(contract_value)
    handoff = decode_subscription_record(handoff_value)
    assert isinstance(task_contract, LogicalTaskContract) and isinstance(handoff, TaskHandoff)
    if (
        task_contract.parent_task_id != primary.task_id
        or decision.candidate_tree_digest != handoff.candidate_tree_digest
        or decision.candidate_commit != handoff.candidate_commit
        or len(set(decision.evidence_receipt_ids)) != len(decision.evidence_receipt_ids)
        or set(decision.evidence_receipt_ids) != set(handoff.evidence_receipt_ids)
    ):
        return None
    return {
        "schema_version": 1,
        "kind": "task_acceptance",
        "result_digest": result.result_digest,
        "primary_task_id": str(primary.task_id),
        "target_task_id": str(decision.task_id),
        "handoff_attempt_id": str(attempt.id),
        "handoff_result_digest": source.result_digest,
        "handoff_application_digest": source.application_digest,
        "handoff_candidate_epoch": attempt.candidate_epoch,
    }


async def task_acceptance_for_context(
    session: AsyncSession, run_id: UUID, task_id: UUID
) -> tuple[UUID, UUID, AcceptDecision] | None:
    """Retain the accepted handoff identity even if a task is subsequently reassigned."""
    from forge.persistence.repositories.subscription_decisions import (
        PostgresSubscriptionDecisionRepository,
    )

    # Version-one AcceptDecision has run_id then task_id in its ordered fields.
    target = SubscriptionDecisionRecord.payload["record"]["fields"][1][1]["$uuid"].astext
    row = (
        await session.execute(
            select(SubscriptionDecisionRecord, SubscriptionAttemptResult)
            .join(
                SubscriptionAttempt, SubscriptionAttempt.id == SubscriptionDecisionRecord.attempt_id
            )
            .join(
                SubscriptionAttemptResult,
                SubscriptionAttemptResult.attempt_id == SubscriptionAttempt.id,
            )
            .where(
                SubscriptionDecisionRecord.run_id == run_id,
                SubscriptionAttempt.run_id == run_id,
                SubscriptionDecisionRecord.record_type == "AcceptDecision",
                target == str(task_id),
                SubscriptionAttemptResult.accepted.is_(True),
                SubscriptionAttemptResult.disposition == "task_accepted",
            )
            .order_by(SubscriptionAttempt.attempt_number.desc())
            .limit(1)
        )
    ).first()
    if row is None:
        return None
    record, result = row
    await PostgresSubscriptionDecisionRepository(session).prepare_acceptance(result.attempt_id)
    decision = decode_subscription_record(record.payload)
    assert isinstance(decision, AcceptDecision) and result.application_payload is not None
    return result.attempt_id, UUID(str(result.application_payload["handoff_attempt_id"])), decision
