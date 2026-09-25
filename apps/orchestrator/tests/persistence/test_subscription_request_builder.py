"""Real planning admission supplies model input and narrowly scoped broker authority."""

import pytest
from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
from forge.application.services.subscription_planning import SubscriptionPlanningService
from forge.application.services.subscription_requests import SubscriptionRequestBuilder
from forge.domain.run import RunState
from forge.domain.subscription import TaskBudget
from forge.domain.tool import ToolName, repository_resource_identity
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_planning_start import planning_start_case
from test_subscription_usage import _reservation


@pytest.mark.integration
async def test_real_planning_request_uses_frozen_context_and_read_only_authority(
    session_factory, tmp_path
):
    factory, original, command = await planning_start_case(session_factory, tmp_path)
    async with factory() as work:
        await SubscriptionPlanningService(TaskBudget(max_provider_attempts=64)).execute(
            command, work
        )
    admission = await SubscriptionDecisionExecutor(factory).admit_next("primary", _reservation())
    assert admission is not None
    request = await SubscriptionRequestBuilder(factory).build(admission)
    assert request.task == admission.task and request.envelope == admission.envelope
    assert request.attempt == admission.attempt
    assert request.budget == _reservation()
    assert request.budget.max_duration_seconds < request.task.budget.max_duration_seconds
    assert request.run_state is RunState.PLANNING
    assert request.authorization.worktree_id == repository_resource_identity(original.project_id)
    assert request.authorization.permitted_tools == frozenset(
        {
            ToolName.REPOSITORY_LIST_FILES,
            ToolName.REPOSITORY_READ_FILE,
            ToolName.REPOSITORY_SEARCH,
            ToolName.REPOSITORY_READ_INSTRUCTIONS,
        }
    )
    assert len(request.authorization.broker_token) >= 32
    assert request.known_tasks == ()
    async with factory() as work:
        human = await work.tasks.get(original.task_id)
    assert request.untrusted_context["task"]["text"] == human.normalized_text
    assert request.untrusted_context["task"]["digest"] == human.task_digest
    assert human.normalized_text not in request.trusted_system_prompt
    assert request.untrusted_context["base_sha"] == original.base_sha
    assert "environment" not in request.untrusted_context["policy"]
    repeated = await SubscriptionRequestBuilder(factory).build(admission)
    assert repeated.authorization.broker_token != request.authorization.broker_token


@pytest.mark.integration
@pytest.mark.parametrize("change", ["resource", "phase", "missing_worktree", "policy_changed"])
async def test_request_rejects_inconsistent_durable_context(session_factory, tmp_path, change):
    from forge.domain.operation import canonical_digest
    from forge.persistence.models import Run
    from forge.persistence.models.scheduling import SubscriptionScheduledTask

    factory, original, command = await planning_start_case(session_factory, tmp_path)
    async with factory() as work:
        await SubscriptionPlanningService(TaskBudget(max_provider_attempts=64)).execute(
            command, work
        )
    admission = await SubscriptionDecisionExecutor(factory).admit_next("primary", _reservation())
    assert admission is not None
    async with factory() as work:
        if change == "resource":
            row = await work.session.get(SubscriptionScheduledTask, admission.task.task_id)
            row.worktree_id = "foreign-tree"
        elif change in ("phase", "missing_worktree"):
            run = await work.session.get(Run, original.id)
            run.state = "PREPARING_WORKTREE" if change == "phase" else "IMPLEMENTING"
        else:
            record = await work.projects.get_policy(original.project_id, original.policy_version)
            document = {**record.document, "version": original.policy_version + 1}
            await work.projects.append_policy(
                project_id=original.project_id,
                expected_policy_version=original.policy_version,
                policy_document=document,
                policy_digest=canonical_digest(document),
            )
        await work.commit()
    with pytest.raises(ValueError):
        await SubscriptionRequestBuilder(factory).build(admission)


@pytest.mark.integration
async def test_resumed_primary_receives_recorded_child_outcome(session_factory, tmp_path):
    from forge.agents.subscription_protocol import json_value
    from forge.domain.subscription import HandoffStatus, decode_subscription_record
    from test_subscription_wait_application import waiting_case

    factory, executor, _, parent, _, child = await waiting_case(session_factory, tmp_path)
    admitted = await executor.admit_next("resumed-primary", _reservation())
    assert admitted.task.task_id == parent.task.task_id
    request = await SubscriptionRequestBuilder(factory).build(admitted)
    outcomes = {row["task_id"]: row for row in request.untrusted_context["task_outcomes"]}
    outcome = outcomes[str(child.task.task_id)]
    assert outcome["state"] == "terminal"
    handoff = decode_subscription_record(json_value(outcome["recorded_handoff"]))
    assert handoff.task_id == child.task.task_id and handoff.attempt_id == child.attempt.attempt_id
    assert handoff.status is HandoffStatus.REPAIRS_EXHAUSTED
    assert outcomes[str(parent.task.task_id)]["state"] == "running"
    assert "recorded_handoff" not in request.trusted_system_prompt

@pytest.mark.integration
async def test_closed_candidate_request_advertises_only_inspection_tools(session_factory, tmp_path):
    from test_subscription_wait_application import waiting_case

    factory, executor, _, parent, _, _ = await waiting_case(session_factory, tmp_path)
    async with factory() as work:
        epoch = await work.scheduler.begin_candidate(parent.task.run_id)
        await work.scheduler.close_candidate(parent.task.run_id, epoch)
        await work.commit()
    admission = await executor.admit_next("candidate-primary", _reservation())
    assert admission is not None and admission.task.task_id == parent.task.task_id
    request = await SubscriptionRequestBuilder(factory).build(admission)
    assert request.authorization.permitted_tools == frozenset({
        ToolName.REPOSITORY_LIST_FILES, ToolName.REPOSITORY_READ_FILE,
        ToolName.REPOSITORY_SEARCH, ToolName.REPOSITORY_READ_INSTRUCTIONS,
        ToolName.GIT_STATUS, ToolName.GIT_DIFF,
        ToolName.VALIDATION_RESULTS_READ, ToolName.REVIEW_ARTIFACTS_READ,
    })
    assert "candidate is CLOSED" in request.trusted_system_prompt
    # Acceptance dispatches controller validation. Requiring its result here
    # leaves a live primary waiting for work that only its proposal can start.
    assert "controller runs final validation after this proposal" in request.trusted_system_prompt
    assert request.untrusted_context["candidate_epoch"] == epoch + 1
