"""Frozen candidate reads require real broker binding and retain snapshot exclusion."""

import pytest
from forge.application.services.subscription_broker import SubscriptionToolBroker
from forge.application.services.subscription_decisions import SubscriptionDecisionApplication
from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
from forge.application.services.subscription_requests import SubscriptionRequestBuilder
from forge.domain.tool import (
    SubscriptionToolAuthorizationContext,
    ToolCallStatus,
    ToolName,
    ToolRequest,
    ToolResult,
)
from forge.persistence.models.scheduling import (
    SubscriptionScheduledEffect,
    SubscriptionSchedulerRun,
)
from test_scheduler_acceptance import (
    _remove_disposable_subscription_rows,  # noqa: F401 - pytest fixture
)
from test_subscription_review_finalization import matching_case
from test_subscription_usage import _reservation


@pytest.mark.integration
@pytest.mark.parametrize("review_required", [False, True])
@pytest.mark.parametrize(
    "tool",
    [
        ToolName.GIT_STATUS,
        ToolName.GIT_DIFF,
        ToolName.VALIDATION_RESULTS_READ,
        ToolName.REVIEW_ARTIFACTS_READ,
    ],
)
async def test_closed_candidate_named_reads_use_broker_and_preserve_snapshot_exclusion(
    session_factory, tmp_path, review_required, tool
):
    factory, parent = await matching_case(
        session_factory, tmp_path, review_required=review_required
    )
    await SubscriptionDecisionApplication(factory).finalize_review_selection(
        parent.attempt.attempt_id
    )
    admission = await SubscriptionDecisionExecutor(factory).admit_next(
        "candidate-reader", _reservation()
    )
    assert admission is not None
    request = await SubscriptionRequestBuilder(factory).build(admission)
    assert tool in request.authorization.permitted_tools
    calls = []

    async def read(operation_id, name, arguments):
        async with factory() as work:
            row = await work.session.get(SubscriptionScheduledEffect, operation_id)
            scheduler = await work.session.get(SubscriptionSchedulerRun, admission.task.run_id)
            assert row.whole_worktree_exclusive is (tool is ToolName.GIT_DIFF)
            assert row.candidate_epoch == scheduler.candidate_epoch == admission.candidate_epoch
            assert scheduler.candidate_state == "closed"
            authority = request.authorization
            context = SubscriptionToolAuthorizationContext(
                run_id=authority.run_id,
                task_id=authority.task_id,
                attempt_id=authority.attempt_id,
                worktree_id=authority.worktree_id,
                purpose=authority.role,
                policy_version=authority.policy_version,
                permitted_tools=authority.permitted_tools,
                invocation_id=operation_id,
                operation_intent_id=operation_id,
            )
            assert (
                await work.subscription.authorize_tool(
                    context, ToolRequest(name=name, arguments=arguments)
                )
                is not None
            )
        calls.append(operation_id)
        return ToolResult(tool_name=name, status=ToolCallStatus.SUCCEEDED)

    broker = SubscriptionToolBroker(
        factory,
        lease=admission.lease,
        authority=request.authorization,
        effect=read,
        owned_paths=admission.task.owned_paths,
    )
    for _ in range(2):
        receipt = await broker.invoke(
            token=request.authorization.broker_token,
            provider_call_key="candidate-read",
            tool_name=tool,
            arguments={"scope": "snapshot"} if tool is ToolName.GIT_DIFF else {},
        )
        assert receipt.accepted
    assert len(calls) == 1


async def reader_case(
    session_factory, tmp_path, *, review_required=True, primary_budget=None, plan_scope=None
):
    factory, parent = await matching_case(
        session_factory,
        tmp_path,
        review_required=review_required,
        primary_budget=primary_budget,
        plan_scope=plan_scope,
    )
    await SubscriptionDecisionApplication(factory).finalize_review_selection(
        parent.attempt.attempt_id
    )
    admission = await SubscriptionDecisionExecutor(factory).admit_next(
        "candidate-reader", _reservation()
    )
    assert admission is not None
    return factory, admission


@pytest.mark.integration
@pytest.mark.parametrize("review_required", [False, True])
@pytest.mark.parametrize("change", ["missing", "write", "check", "commit", "generation"])
async def test_closed_effect_requires_exact_bound_read(
    session_factory, tmp_path, review_required, change
):
    from uuid import uuid4

    from forge.application.ports.scheduling import SchedulingConflict
    from forge.domain.subscription import ToolCallBinding
    from forge.persistence.models.subscription import SubscriptionAttempt

    factory, admission = await reader_case(
        session_factory, tmp_path, review_required=review_required
    )
    effect_id = uuid4()
    async with factory() as work:
        if change != "missing":
            tool = {
                "write": ToolName.REPOSITORY_WRITE_FILE,
                "check": ToolName.BUILD_RUN_NAMED_CHECK,
                "commit": ToolName.GIT_COMMIT,
            }.get(change, ToolName.GIT_STATUS)
            await work.subscription.bind_operation(
                ToolCallBinding(
                    attempt_id=admission.attempt.attempt_id,
                    provider_call_key="candidate-read",
                    durable_operation_id=effect_id,
                    tool_name=tool,
                    arguments_digest="a" * 64,
                ),
                run_id=admission.task.run_id,
                task_id=admission.task.task_id,
            )
        if change == "generation":
            (
                await work.session.get(SubscriptionAttempt, admission.attempt.attempt_id)
            ).lease_generation += 1
        await work.commit()
    async with factory() as work:
        with pytest.raises(SchedulingConflict, match="candidate barrier"):
            await work.scheduler.admit_effect(admission.lease, effect_id)
        assert await work.session.get(SubscriptionScheduledEffect, effect_id) is None


@pytest.mark.integration
async def test_legacy_primary_read_keeps_its_exclusive_replay_identity(session_factory, tmp_path):
    from uuid import uuid4

    from forge.domain.subscription import ToolCallBinding

    factory, admission = await reader_case(session_factory, tmp_path, review_required=False)
    effect_id = uuid4()
    async with factory() as work:
        await work.subscription.bind_operation(
            ToolCallBinding(
                attempt_id=admission.attempt.attempt_id,
                provider_call_key="legacy-read",
                durable_operation_id=effect_id,
                tool_name=ToolName.GIT_STATUS,
                arguments_digest="a" * 64,
            ),
            run_id=admission.task.run_id,
            task_id=admission.task.task_id,
        )
        effect = await work.scheduler.admit_effect(
            admission.lease, effect_id, whole_worktree_exclusive=True
        )
        await work.commit()
    async with factory() as work:
        replay = await work.scheduler.admit_effect(admission.lease, effect_id)
        assert replay == effect
        assert (
            await work.session.get(SubscriptionScheduledEffect, effect_id)
        ).whole_worktree_exclusive


@pytest.mark.integration
async def test_snapshot_exclusion_lasts_until_effect_settles(session_factory, tmp_path):
    from uuid import uuid4

    from forge.application.ports.scheduling import SchedulingConflict
    from forge.domain.subscription import ToolCallBinding

    factory, admission = await reader_case(session_factory, tmp_path)
    identities = (uuid4(), uuid4())
    async with factory() as work:
        for index, identity in enumerate(identities):
            await work.subscription.bind_operation(
                ToolCallBinding(
                    attempt_id=admission.attempt.attempt_id,
                    provider_call_key=f"snapshot-{index}",
                    durable_operation_id=identity,
                    tool_name=ToolName.GIT_DIFF,
                    arguments_digest="a" * 64,
                ),
                run_id=admission.task.run_id,
                task_id=admission.task.task_id,
            )
        first = await work.scheduler.admit_effect(
            admission.lease, identities[0], whole_worktree_exclusive=True
        )
        await work.commit()
    async with factory() as work:
        with pytest.raises(SchedulingConflict, match="exclusive effect barrier"):
            await work.scheduler.admit_effect(
                admission.lease, identities[1], whole_worktree_exclusive=True
            )
        assert await work.scheduler.settle_effect(first, accepted=True)
        second = await work.scheduler.admit_effect(
            admission.lease, identities[1], whole_worktree_exclusive=True
        )
        assert second.candidate_epoch == first.candidate_epoch
