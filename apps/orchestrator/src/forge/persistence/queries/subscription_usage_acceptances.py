"""Bounded cross-source proof for primary acknowledgements of worker handoffs."""

from collections.abc import AsyncIterator
from uuid import UUID

from sqlalchemy import String, and_, cast, select
from sqlalchemy.ext.asyncio import AsyncSession

from forge.domain.subscription import AcceptDecision, SpecialistPurpose, TaskHandoff
from forge.persistence.models.run import Run
from forge.persistence.models.subscription import SubscriptionEnvelope
from forge.persistence.queries.subscription_usage_measurements import GroupKey, source_key
from forge.persistence.queries.subscription_usage_proofs import UsageEvidence
from forge.persistence.queries.subscription_usage_sources import UsageSources


async def task_acceptance_sources(
    session: AsyncSession,
    run_id: UUID | None,
) -> AsyncIterator[tuple[GroupKey, GroupKey | None, UUID | None]]:
    """Yield locally proved acknowledgements and their proved (or unknown) target.

    Sorting by child identity allows one previous accepted task per page group;
    repeated primary acknowledgements never require an unbounded identity set.
    """
    primary, child = UsageSources.named("accepting"), UsageSources.named("accepted")
    statement = primary.join(
        select(
            *primary.columns(), *child.columns(), Run.project_id, SubscriptionEnvelope
        ).select_from(primary.attempt)
    )
    statement = child.join(
        statement.outerjoin(
            child.attempt,
            and_(
                cast(child.attempt.id, String)
                == primary.result.application_payload["handoff_attempt_id"].astext,
                child.attempt.run_id == primary.attempt.run_id,
            ),
        ),
        optional=True,
    )
    statement = (
        statement.join(Run, Run.id == primary.attempt.run_id)
        .outerjoin(SubscriptionEnvelope, SubscriptionEnvelope.run_id == primary.attempt.run_id)
        .where(
            primary.result.accepted.is_(True),
            primary.result.disposition == "task_accepted",
            primary.attempt.lease_owner.is_not(None),
        )
        .order_by(
            child.attempt.task_row_id, child.attempt.attempt_number, primary.attempt.attempt_number
        )
        .execution_options(yield_per=100)
    )
    if run_id is not None:
        statement = statement.where(primary.attempt.run_id == run_id)
    rows = await session.stream(statement)
    try:
        async for row in rows:
            parent = UsageEvidence(*row[:7])
            project_id, envelope = row[14:]
            parent_source = parent.source(envelope)
            if not parent.applied(parent_source):
                # The main assessment already classified these as unverified.
                continue
            parent_key, _ = source_key(parent.attempt, parent.task, parent.consumption, project_id)
            child_key = None
            child_task_id = None
            if row[7] is not None:
                target = UsageEvidence(*row[7:14])
                target_source = target.source(envelope)
                assert parent_source is not None and parent.result is not None
                decision = parent_source.decision
                handoff = target_source.decision if target_source else None
                if (
                    isinstance(decision, AcceptDecision)
                    and isinstance(handoff, TaskHandoff)
                    and parent_source.contract.purpose is SpecialistPurpose.PRIMARY
                    and parent_source.contract.parent_task_id is None
                    and target_source is not None
                    and target.applied(target_source)
                    and target.record is not None
                    and target.record.created_at <= parent.attempt.created_at
                    and target_source.contract.parent_task_id == parent.task.id
                    and decision.task_id == target.task.id
                    and decision.candidate_commit == handoff.candidate_commit
                    and decision.candidate_tree_digest == handoff.candidate_tree_digest
                    and len(set(decision.evidence_receipt_ids))
                    == len(decision.evidence_receipt_ids)
                    and set(decision.evidence_receipt_ids) == set(handoff.evidence_receipt_ids)
                    and target.result is not None
                    and parent.result.application_payload
                    == {
                        "schema_version": 1,
                        "kind": "task_acceptance",
                        "result_digest": parent.result.result_digest,
                        "primary_task_id": str(parent.task.id),
                        "target_task_id": str(target.task.id),
                        "handoff_attempt_id": str(target.attempt.id),
                        "handoff_result_digest": target.result.result_digest,
                        "handoff_application_digest": target.result.application_digest,
                        "handoff_candidate_epoch": target.attempt.candidate_epoch,
                    }
                ):
                    child_key, _ = source_key(
                        target.attempt, target.task, target.consumption, project_id
                    )
                    child_task_id = target.task.id
            yield parent_key, child_key, child_task_id
    finally:
        await rows.close()
