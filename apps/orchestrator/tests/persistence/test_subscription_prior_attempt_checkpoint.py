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
    by_number = {item["attempt_number"]: item for item in checkpoint["attempts"]}
    assert by_number[1]["contract"] == "pre_plan"
    entry = by_number[parent.attempt.attempt_number]
    assert entry["contract"] == "current"
    assert entry["disposition"]
    assert entry["files_read"] == ("src/a.py",)
    assert entry["searches"] == ({"literal": "needle", "path": "src"},)
    assert entry["files_written"] == ("src/a.py",)
    assert entry["named_checks"] == ({"name": "unit", "passed": False, "status": "failed"},)
    assert "sibling" not in repr(checkpoint)
    assert "src/current.py" not in repr(checkpoint)
    assert "SECRET-CONTENT-MUST-NOT-APPEAR" not in repr(request.untrusted_context)
    assert request.prompt_version == "forge-subscription-v24"
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
    # The fixture carries a genuine approved-plan source link for attempt 1, so
    # that pre-plan attempt may cross; an arbitrary digest change to a later
    # attempt still cannot.
    factory, executor, _, parent, _, _ = await waiting_case(session_factory, tmp_path)
    await _record(factory, parent, ToolName.REPOSITORY_READ_FILE, {"path": "src/a.py"})
    async with factory() as work:
        attempt = await work.session.get(SubscriptionAttempt, parent.attempt.attempt_id)
        attempt.task_digest = "f" * 64
        await work.commit()
    resumed = await executor.admit_next("resumed-primary", _reservation())
    assert resumed is not None
    request = await SubscriptionRequestBuilder(factory).build(resumed)
    checkpoint = request.untrusted_context["prior_attempt_checkpoint"]
    assert [item["attempt_number"] for item in checkpoint["attempts"]] == [1]
    assert checkpoint["attempts"][0]["contract"] == "pre_plan"
    assert "src/a.py" not in repr(checkpoint)


@pytest.mark.integration
async def test_checkpoint_crosses_approved_plan_boundary(session_factory, tmp_path):
    from test_subscription_preparation import preparation_case

    factory, evidence, command, service, _ = await preparation_case(session_factory, tmp_path)
    async with factory() as work:
        work.session.add(
            ToolCall(
                id=uuid4(),
                run_id=evidence.producer.run_id,
                subscription_task_id=evidence.producer.task_id,
                subscription_attempt_id=evidence.producer.attempt_id,
                subscription_purpose="primary",
                tool_name=ToolName.REPOSITORY_READ_FILE.value,
                arguments_schema_version=1,
                normalized_arguments={"path": "src/planning.py"},
                authorized=True,
                status="SUCCEEDED",
                started_at=NOW,
                completed_at=NOW,
            )
        )
        await work.commit()
    async with factory() as work:
        await service.execute(command, work)
    admission = await SubscriptionDecisionExecutor(factory).admit_next(
        "implementation-primary", _reservation()
    )
    assert admission is not None and admission.attempt.attempt_number == 2
    request = await SubscriptionRequestBuilder(factory).build(admission)
    checkpoint = request.untrusted_context["prior_attempt_checkpoint"]
    (entry,) = checkpoint["attempts"]
    assert entry["attempt_number"] == 1
    assert entry["contract"] == "pre_plan"
    assert entry["files_read"] == ("src/planning.py",)
    assert request.prompt_version == "forge-subscription-v24"
    assert "pre_plan" in request.trusted_system_prompt
    assert "must be verified against the approved plan" in " ".join(request.trusted_system_prompt.split())


@pytest.mark.integration
async def test_prior_attempts_crossing_requires_valid_source_link(session_factory, tmp_path):
    from forge.persistence.repositories.subscription_recovery import (
        PostgresSubscriptionRecoveryRepository,
    )

    factory, executor, _, parent, _, child = await waiting_case(session_factory, tmp_path)
    await _record(factory, parent, ToolName.REPOSITORY_READ_FILE, {"path": "src/a.py"})
    async with factory() as work:
        attempt = await work.session.get(SubscriptionAttempt, parent.attempt.attempt_id)
        attempt.task_digest = "f" * 64
        await work.commit()
    resumed = await executor.admit_next("resumed-primary", _reservation())
    assert resumed is not None

    async def prior(source_attempt_id):
        async with factory() as work:
            current = await work.session.get(
                SubscriptionAttempt, resumed.attempt.attempt_id
            )
            rows = await PostgresSubscriptionRecoveryRepository(
                work.session
            ).prior_attempts(
                resumed.attempt.run_id,
                resumed.task.task_id,
                before_attempt_number=resumed.attempt.attempt_number,
                task_digest=current.task_digest,
                source_attempt_id=source_attempt_id,
            )
            await work.rollback()
            return rows

    assert await prior(None) == ()
    assert await prior(uuid4()) == ()
    # Source attempt of another task in the same run.
    assert await prior(child.attempt.attempt_id) == ()
    # Source attempt that is not earlier than the current attempt.
    assert await prior(resumed.attempt.attempt_id) == ()
    crossed = await prior(parent.attempt.attempt_id)
    # All attempts up to the recorded source may cross (e.g. an earlier planning
    # attempt plus the source attempt); none may be from another task or later.
    assert crossed
    assert all(
        row.attempt_number <= parent.attempt.attempt_number for row in crossed
    )
    by_number = {row.attempt_number: row for row in crossed}
    assert by_number[parent.attempt.attempt_number].contract == "pre_plan"
