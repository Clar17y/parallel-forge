"""Bounded outcome context is scoped and never silently substitutes old evidence."""

from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from forge.domain.subscription import HandoffStatus, TaskHandoff, encode_subscription_record
from forge.persistence.models.subscription import SubscriptionDecisionRecord
from forge.persistence.repositories.subscription import (
    PostgresSubscriptionRepository,
    SubscriptionConflict,
)
from sqlalchemy import select
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_wait_application import waiting_case


@pytest.mark.integration
async def test_latest_outcome_uses_attempt_order_and_current_task_flags(session_factory, tmp_path):
    factory, _, _, parent, _, child = await waiting_case(session_factory, tmp_path)
    later = replace(
        child.attempt, attempt_id=uuid4(), attempt_number=child.attempt.attempt_number + 1
    )
    handoff = TaskHandoff(
        run_id=later.run_id,
        task_id=later.task_id,
        attempt_id=later.attempt_id,
        status=HandoffStatus.FAILED,
        summary="Latest recorded observation",
    )
    async with factory() as work:
        await work.subscription.create_attempt(
            later, route_payload=child.task.route, idempotency_key="later"
        )
        await work.subscription.record_decision(handoff, idempotency_key="later-outcome")
        record = await work.session.scalar(
            select(SubscriptionDecisionRecord).where(
                SubscriptionDecisionRecord.attempt_id == later.attempt_id
            )
        )
        record.created_at = datetime(2000, 1, 1, tzinfo=UTC)
        from forge.persistence.models.subscription import SubscriptionTask

        task = await work.session.get(SubscriptionTask, child.task.task_id)
        task.pause_requested = True
        await work.commit()
    async with factory() as work:
        outcomes = await work.subscription.invocation_outcomes(
            parent.task.run_id, (parent.task.task_id, child.task.task_id)
        )
        outcome = next(item for item in outcomes if item.task_id == child.task.task_id)
        assert outcome.recorded_handoff == handoff and outcome.pause_requested
        assert outcome.state == "terminal" and not outcome.cancel_requested
        with pytest.raises(SubscriptionConflict, match="lineage"):
            await work.subscription.invocation_outcomes(uuid4(), (child.task.task_id,))
        with pytest.raises(SubscriptionConflict, match="lineage"):
            await work.subscription.invocation_outcomes(parent.task.run_id, (uuid4(),))


@pytest.mark.integration
@pytest.mark.parametrize("corruption", ["lineage", "oversized"])
async def test_invalid_latest_handoff_is_rejected(session_factory, tmp_path, corruption):
    factory, _, _, parent, _, child = await waiting_case(session_factory, tmp_path)
    async with factory() as work:
        record = await work.session.scalar(
            select(SubscriptionDecisionRecord).where(
                SubscriptionDecisionRecord.attempt_id == child.attempt.attempt_id,
                SubscriptionDecisionRecord.record_type == "TaskHandoff",
            )
        )
        if corruption == "lineage":
            record.payload = encode_subscription_record(
                TaskHandoff(
                    run_id=uuid4(),
                    task_id=child.task.task_id,
                    attempt_id=child.attempt.attempt_id,
                    status=HandoffStatus.FAILED,
                )
            )
        else:
            record.payload = {**record.payload, "padding": "x" * 65536}
        await work.commit()
    async with factory() as work:
        with pytest.raises(SubscriptionConflict, match="lineage|bound"):
            await work.subscription.invocation_outcomes(parent.task.run_id, (child.task.task_id,))


@pytest.mark.parametrize("identities", [(), (uuid4(),) * 2, tuple(uuid4() for _ in range(258))])
async def test_outcome_task_set_bound_precedes_database_access(identities):
    with pytest.raises(SubscriptionConflict, match="bound"):
        await PostgresSubscriptionRepository(None).invocation_outcomes(uuid4(), identities)
