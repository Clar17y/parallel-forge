"""A resumed attempt receives a checkpoint from its own earlier attempts only."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
from forge.application.services.subscription_requests import SubscriptionRequestBuilder
from forge.domain.tool import ToolName
from forge.persistence.models import ToolCall
from forge.persistence.models.subscription import SubscriptionAttempt
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_planning_start import planning_start_case
from test_subscription_usage import _reservation
from test_subscription_wait_application import waiting_case

NOW = datetime(2026, 10, 21, tzinfo=UTC)


async def _record(factory, admission, tool, arguments, *, status="SUCCEEDED"):
    async with factory() as work:
        work.session.add(
            ToolCall(
                id=uuid4(),
                run_id=admission.attempt.run_id,
                subscription_task_id=admission.task.task_id,
                subscription_attempt_id=admission.attempt.attempt_id,
                subscription_purpose=admission.task.purpose.value,
                tool_name=tool.value,
                arguments_schema_version=1,
                normalized_arguments=arguments,
                authorized=True,
                status=status,
                started_at=NOW,
                completed_at=NOW,
            )
        )
        await work.commit()


@pytest.mark.integration
async def test_resumed_primary_receives_checkpoint_for_its_own_prior_attempt(
    session_factory, tmp_path
):
    factory, executor, _, parent, _, child = await waiting_case(session_factory, tmp_path)
    await _record(factory, parent, ToolName.REPOSITORY_READ_FILE, {"path": "src/a.py"})
    await _record(factory, parent, ToolName.REPOSITORY_SEARCH, {"literal": "needle", "path": "src"})
    await _record(
        factory,
        parent,
        ToolName.REPOSITORY_WRITE_FILE,
        {"path": "src/a.py", "content": "SECRET-CONTENT-MUST-NOT-APPEAR"},
    )
    await _record(
        factory,
        parent,
        ToolName.BUILD_RUN_NAMED_CHECK,
        {"command_name": "unit"},
        status="FAILED",
    )
    await _record(factory, child, ToolName.REPOSITORY_READ_FILE, {"path": "src/sibling.py"})

    resumed = await executor.admit_next("resumed-primary", _reservation())
    assert resumed is not None and resumed.task.task_id == parent.task.task_id
    assert resumed.attempt.attempt_number > 1
    await _record(factory, resumed, ToolName.REPOSITORY_READ_FILE, {"path": "src/current.py"})

    request = await SubscriptionRequestBuilder(factory).build(resumed)
    checkpoint = request.untrusted_context["prior_attempt_checkpoint"]
    assert checkpoint["historical"] is True
    (entry,) = checkpoint["attempts"]
    assert entry["attempt_number"] == parent.attempt.attempt_number
    assert entry["disposition"]
    assert entry["files_read"] == ("src/a.py",)
    assert entry["searches"] == ({"literal": "needle", "path": "src"},)
    assert entry["files_written"] == ("src/a.py",)
    assert entry["named_checks"] == ({"name": "unit", "passed": False, "status": "failed"},)
    assert "sibling" not in repr(checkpoint)
    assert "current" not in repr(checkpoint)
    assert "SECRET-CONTENT-MUST-NOT-APPEAR" not in repr(request.untrusted_context)
    assert request.prompt_version == "forge-subscription-v21"
    assert "prior_attempt_checkpoint, when present, is a historical summary" in (
        request.trusted_system_prompt
    )
    assert "not evidence that any check currently passes" in request.trusted_system_prompt


@pytest.mark.integration
async def test_first_attempt_context_has_no_checkpoint(session_factory, tmp_path):
    factory, _original, command = await planning_start_case(session_factory, tmp_path)
    from forge.application.services.subscription_planning import SubscriptionPlanningService
    from forge.domain.subscription import TaskBudget

    async with factory() as work:
        await SubscriptionPlanningService(TaskBudget(max_provider_attempts=64)).execute(
            command, work
        )
    admission = await SubscriptionDecisionExecutor(factory).admit_next("primary", _reservation())
    assert admission is not None and admission.attempt.attempt_number == 1
    request = await SubscriptionRequestBuilder(factory).build(admission)
    assert "prior_attempt_checkpoint" not in request.untrusted_context


@pytest.mark.integration
async def test_checkpoint_gated_by_frozen_task_contract_digest(session_factory, tmp_path):
    factory, executor, _, parent, _, _ = await waiting_case(session_factory, tmp_path)
    await _record(factory, parent, ToolName.REPOSITORY_READ_FILE, {"path": "src/a.py"})
    async with factory() as work:
        attempt = await work.session.get(SubscriptionAttempt, parent.attempt.attempt_id)
        attempt.task_digest = "f" * 64
        await work.commit()
    resumed = await executor.admit_next("resumed-primary", _reservation())
    assert resumed is not None
    request = await SubscriptionRequestBuilder(factory).build(resumed)
    assert "prior_attempt_checkpoint" not in request.untrusted_context
