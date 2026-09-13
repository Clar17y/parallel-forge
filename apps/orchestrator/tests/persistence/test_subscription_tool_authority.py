"""Durable subscription authority is checked against current PostgreSQL rows."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from forge.domain.operation import canonical_digest
from forge.domain.subscription import (
    AttemptIdentity,
    RouteBinding,
    SpecialistPurpose,
    ToolCallBinding,
    encode_subscription_record,
)
from forge.domain.tool import SubscriptionToolAuthorizationContext, ToolName, ToolRequest
from forge.persistence.models.scheduling import (
    SubscriptionScheduledEffect,
    SubscriptionScheduledTask,
)
from forge.persistence.models.subscription import SubscriptionAttempt, SubscriptionTask
from forge.persistence.unit_of_work import PostgresUnitOfWork
from sqlalchemy import update
from test_scheduler_acceptance import (
    _admit_run,
    _enqueue,
    _remove_disposable_subscription_rows,  # noqa: F401 - autouse cleanup
    _route,
)


@pytest.mark.integration
@pytest.mark.parametrize(
    "change",
    [
        "none",
        "newer_attempt",
        "wrong_route",
        "settled_without_tool",
        "expired",
        "paused",
        "wrong_digest",
        "wrong_worktree",
    ],
)
async def test_authority_requires_current_route_attempt_and_unsettled_effect(
    session_factory, persisted_run, change
):
    attempt_id, operation_id = uuid4(), uuid4()
    request = ToolRequest(name=ToolName.REPOSITORY_READ_FILE, arguments={"path": "apps/file.py"})
    route = RouteBinding(requested=_route("p"), effective=_route("p"))
    async with PostgresUnitOfWork(session_factory) as work:
        primary = await _admit_run(work, persisted_run, (_route("p"), _route("p")))
        task_id = await _enqueue(
            work,
            persisted_run.id,
            provider="p",
            worktree="tree",
            parent_id=primary,
            paths=("apps",),
        )
        await work.subscription.create_attempt(
            AttemptIdentity(run_id=persisted_run.id, task_id=task_id, attempt_id=attempt_id),
            route_payload=route,
            idempotency_key="attempt",
        )
        await work.session.execute(
            update(SubscriptionTask).where(SubscriptionTask.id == task_id).values(state="running")
        )
        await work.session.execute(
            update(SubscriptionAttempt)
            .where(SubscriptionAttempt.id == attempt_id)
            .values(status="running")
        )
        await work.subscription.bind_operation(
            ToolCallBinding(
                attempt_id=attempt_id,
                provider_call_key="read",
                durable_operation_id=operation_id,
                tool_name=request.name,
                arguments_digest=canonical_digest(request.arguments),
            ),
            run_id=persisted_run.id,
            task_id=task_id,
        )
        lease = await work.scheduler.claim_ready("owner", timedelta(seconds=30))
        assert lease is not None
        await work.scheduler.admit_effect(lease, operation_id)
        if change == "newer_attempt":
            await work.subscription.create_attempt(
                AttemptIdentity(
                    run_id=persisted_run.id, task_id=task_id, attempt_id=uuid4(), attempt_number=2
                ),
                route_payload=route,
                idempotency_key="new-attempt",
            )
        elif change == "wrong_route":
            await work.session.execute(
                update(SubscriptionAttempt)
                .where(SubscriptionAttempt.id == attempt_id)
                .values(
                    route_payload=encode_subscription_record(
                        RouteBinding(requested=_route("foreign"), effective=_route("foreign"))
                    )
                )
            )
        elif change == "settled_without_tool":
            await work.session.execute(
                update(SubscriptionScheduledEffect)
                .where(SubscriptionScheduledEffect.id == operation_id)
                .values(state="settled")
            )
        if change == "expired":
            await work.session.execute(
                update(SubscriptionScheduledTask)
                .where(SubscriptionScheduledTask.task_id == task_id)
                .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
        elif change == "paused":
            await work.scheduler.request_stop(persisted_run.id, task_id, cancel=False)
        await work.commit()
    if change == "wrong_digest":
        request = ToolRequest(name=request.name, arguments={"path": "apps/other.py"})
    context = SubscriptionToolAuthorizationContext(
        run_id=persisted_run.id,
        task_id=task_id,
        attempt_id=attempt_id,
        worktree_id="other" if change == "wrong_worktree" else "tree",
        purpose=SpecialistPurpose.ROUTINE_IMPLEMENTATION,
        policy_version=1,
        permitted_tools=frozenset({request.name}),
        invocation_id=operation_id,
        operation_intent_id=operation_id,
    )
    async with PostgresUnitOfWork(session_factory) as work:
        proof = await work.subscription.authorize_tool(context, request)
        assert (proof is not None) is (change == "none")
