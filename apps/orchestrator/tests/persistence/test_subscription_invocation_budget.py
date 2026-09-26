"""A request can read only its exact outstanding attempt reservation."""

from uuid import uuid4

import pytest
from forge.application.ports.subscription_gateway import (
    SubscriptionFailure,
    SubscriptionInvocationResult,
)
from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
from forge.persistence.repositories.subscription_budget import SubscriptionBudgetConflict
from forge.persistence.unit_of_work import PostgresUnitOfWork
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_invocation_context import _admission
from test_subscription_usage import _reservation


@pytest.mark.integration
async def test_reservation_read_checks_lineage_and_outstanding_state(
    session_factory, persisted_run
):
    admission = await _admission(session_factory, persisted_run)
    ids = (persisted_run.id, admission.task.task_id, admission.attempt.attempt_id)
    async with PostgresUnitOfWork(session_factory) as work:
        await work.runs.get_for_update(persisted_run.id)
        assert await work.subscription_budget.reserved_budget(*ids) == _reservation()
        for position in range(3):
            foreign = list(ids)
            foreign[position] = uuid4()
            with pytest.raises(SubscriptionBudgetConflict, match="matching reservation"):
                await work.subscription_budget.reserved_budget(*foreign)
    executor = SubscriptionDecisionExecutor(lambda: PostgresUnitOfWork(session_factory))
    await executor.settle(
        admission,
        SubscriptionInvocationResult(
            attempt=admission.attempt, failure=SubscriptionFailure.UNAVAILABLE
        ),
    )
    async with PostgresUnitOfWork(session_factory) as work:
        await work.runs.get_for_update(persisted_run.id)
        with pytest.raises(SubscriptionBudgetConflict, match="already settled"):
            await work.subscription_budget.reserved_budget(*ids)
