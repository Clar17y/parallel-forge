"""Task inspection retains only safe worker-feedback receipt state."""

import hashlib
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from forge.api.schemas.subscription_tasks import SubscriptionTaskPage
from forge.application.services.subscription_feedback import SubscriptionTaskFeedbackService
from forge.application.services.subscription_profiles import LocalOperatorProfileActor
from forge.domain.subscription_feedback import (
    MAX_FEEDBACK_PER_TASK,
    SubscriptionTaskFeedbackRequest,
)
from forge.persistence.models import ApiMutation
from forge.persistence.models.subscription_feedback import SubscriptionTaskFeedback
from forge.persistence.queries.subscription_tasks import SubscriptionTaskQuery
from forge.persistence.repositories.subscription_feedback import _close_before_forwarding
from forge.persistence.unit_of_work import PostgresUnitOfWork
from test_scheduler_acceptance import (  # noqa: F401
    _enqueue,
    _remove_disposable_subscription_rows,
)
from test_subscription_task_controls import _seed


@pytest.mark.integration
async def test_task_projection_retains_safe_feedback_receipt_state(session_factory, persisted_run):
    primary_id, child_1 = await _seed(session_factory, persisted_run)
    async with PostgresUnitOfWork(session_factory) as work:
        child_2 = await _enqueue(
            work,
            persisted_run.id,
            provider="p",
            worktree="task-control-tree",
            parent_id=primary_id,
            paths=("apps",),
        )
        await work.commit()

    times = [
        datetime(2026, 9, 18, 12, 0, tzinfo=UTC),
        datetime(2026, 9, 18, 12, 5, tzinfo=UTC),
        datetime(2026, 9, 18, 12, 10, tzinfo=UTC),
        datetime(2026, 9, 18, 12, 15, tzinfo=UTC),
    ]
    current_time = [times[0]]
    service = SubscriptionTaskFeedbackService(
        lambda: PostgresUnitOfWork(session_factory),
        now=lambda: current_time[0],
    )
    actor = LocalOperatorProfileActor()

    # Submit first receipt for child_1 and close it with budget_exhausted
    receipt_1_1 = await service.submit(
        run_id=persisted_run.id,
        task_id=child_1,
        actor=actor,
        idempotency_key="child-1-feedback-1",
        request=SubscriptionTaskFeedbackRequest(
            expected_run_version=0,
            expected_task_version=0,
            feedback="Preserve the partial parser bytes and add the replay assertion.",
        ),
    )
    async with PostgresUnitOfWork(session_factory) as work:
        row = await work.session.get(SubscriptionTaskFeedback, receipt_1_1.receipt_id)
        assert row is not None
        _close_before_forwarding(row, "budget_exhausted")
        await work.commit()

    # Submit second receipt for child_1 and close it with cancelled
    current_time[0] = times[1]
    receipt_1_2 = await service.submit(
        run_id=persisted_run.id,
        task_id=child_1,
        actor=actor,
        idempotency_key="child-1-feedback-2",
        request=SubscriptionTaskFeedbackRequest(
            expected_run_version=0,
            expected_task_version=0,
            feedback="Second feedback for first child task.",
        ),
    )
    async with PostgresUnitOfWork(session_factory) as work:
        row = await work.session.get(SubscriptionTaskFeedback, receipt_1_2.receipt_id)
        assert row is not None
        _close_before_forwarding(row, "cancelled")
        await work.commit()

    # Submit first receipt for sibling child_2 and close it with cancelled
    current_time[0] = times[2]
    receipt_2_1 = await service.submit(
        run_id=persisted_run.id,
        task_id=child_2,
        actor=actor,
        idempotency_key="child-2-feedback-1",
        request=SubscriptionTaskFeedbackRequest(
            expected_run_version=0,
            expected_task_version=0,
            feedback="First feedback for second child task.",
        ),
    )
    async with PostgresUnitOfWork(session_factory) as work:
        row = await work.session.get(SubscriptionTaskFeedback, receipt_2_1.receipt_id)
        assert row is not None
        _close_before_forwarding(row, "cancelled")
        await work.commit()

    # Submit second receipt for sibling child_2; stays pending_primary
    current_time[0] = times[3]
    receipt_2_2 = await service.submit(
        run_id=persisted_run.id,
        task_id=child_2,
        actor=actor,
        idempotency_key="child-2-feedback-2",
        request=SubscriptionTaskFeedbackRequest(
            expected_run_version=0,
            expected_task_version=0,
            feedback="Second feedback for second child task.",
        ),
    )

    page = SubscriptionTaskPage.model_validate(
        await SubscriptionTaskQuery(session_factory).tasks(persisted_run.id)
    )
    task_1 = next(value for value in page.tasks if value.task_id == child_1)
    task_2 = next(value for value in page.tasks if value.task_id == child_2)

    # Discriminate per-task partitioning and oldest-first ordering
    assert [r.receipt_id for r in task_1.feedback_receipts] == [
        receipt_1_1.receipt_id,
        receipt_1_2.receipt_id,
    ]
    assert [r.receipt_id for r in task_2.feedback_receipts] == [
        receipt_2_1.receipt_id,
        receipt_2_2.receipt_id,
    ]

    # Discriminate closed_reason projection and status projection
    assert task_1.feedback_receipts[0].status == "closed"
    assert task_1.feedback_receipts[0].closed_reason == "budget_exhausted"
    assert task_1.feedback_receipts[0].primary_task_id == primary_id
    assert task_1.feedback_receipts[0].feedback_digest == receipt_1_1.feedback_digest
    assert task_1.feedback_receipts[0].feedback_bytes == receipt_1_1.feedback_bytes

    assert task_1.feedback_receipts[1].status == "closed"
    assert task_1.feedback_receipts[1].closed_reason == "cancelled"
    assert task_1.feedback_receipts[0].observed_at <= task_1.feedback_receipts[1].observed_at

    assert task_2.feedback_receipts[0].status == "closed"
    assert task_2.feedback_receipts[0].closed_reason == "cancelled"
    assert task_2.feedback_receipts[0].primary_task_id == primary_id

    assert task_2.feedback_receipts[1].status == "pending_primary"
    assert task_2.feedback_receipts[1].closed_reason is None
    assert task_2.feedback_receipts[0].observed_at <= task_2.feedback_receipts[1].observed_at

    # Discriminate safe projection against leakage of submitted feedback
    assert "partial parser bytes" not in page.model_dump_json()
    assert "second child task" not in page.model_dump_json()


@pytest.mark.integration
async def test_task_projection_rejects_defensive_overflow_bound(session_factory, persisted_run):
    primary_id, task_id = await _seed(session_factory, persisted_run)
    base_time = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
    actor_id = uuid4()
    async with PostgresUnitOfWork(session_factory) as work:
        for index in range(MAX_FEEDBACK_PER_TASK + 1):
            mutation_id = uuid4()
            work.session.add(
                ApiMutation(
                    id=mutation_id,
                    actor_id=actor_id,
                    action=f"subscription.feedback.submit-{index}",
                    scope=f"runs/{persisted_run.id}",
                    key_hash=hashlib.sha256(f"overflow-key-{index}".encode()).hexdigest(),
                    request_digest="0" * 64,
                    lifecycle_state="RESERVED",
                )
            )
            work.session.add(
                SubscriptionTaskFeedback(
                    id=mutation_id,
                    actor_id=actor_id,
                    run_id=persisted_run.id,
                    primary_task_id=primary_id,
                    task_id=task_id,
                    observed_run_version=0,
                    observed_task_version=0,
                    observed_primary_version=0,
                    observed_task_digest="0" * 64,
                    observed_primary_digest="0" * 64,
                    envelope_digest="0" * 64,
                    feedback="x",
                    feedback_bytes=1,
                    feedback_digest=hashlib.sha256(b"x").hexdigest(),
                    request_digest="0" * 64,
                    state="closed",
                    closed_reason="cancelled",
                    application_digest="0" * 64,
                    created_at=base_time + timedelta(seconds=index),
                )
            )
        await work.commit()

    with pytest.raises(ValueError, match="subscription feedback projection exceeds its bound"):
        await SubscriptionTaskQuery(session_factory).tasks(persisted_run.id)
