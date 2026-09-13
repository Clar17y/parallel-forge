"""Remediation uses the same controlled command and commit boundaries."""

from uuid import uuid5

import pytest
from forge.domain.run import RunState
from forge.domain.tool import ToolName
from forge.persistence.models import Run
from forge.persistence.models.subscription import SubscriptionAttempt
from forge.persistence.unit_of_work import PostgresUnitOfWork
from test_scheduler_acceptance import (
    _remove_disposable_subscription_rows,  # noqa: F401 - pytest fixture
)
from test_subscription_git_commit import _case
from test_subscription_named_check import _subscription_named_case


async def seed_remediation_phase(session_factory, attempt_id):
    # The full failure-to-remediation transition is exercised separately. These
    # existing effect fixtures isolate the stage checks on actual controlled IO.
    async with PostgresUnitOfWork(session_factory) as work:
        attempt = await work.session.get(SubscriptionAttempt, attempt_id)
        (await work.session.get(Run, attempt.run_id)).state = RunState.REMEDIATING.value
        await work.commit()


@pytest.mark.integration
@pytest.mark.parametrize("command_name", ["unit", "not-in-policy"])
async def test_remediation_named_check_keeps_policy_and_one_effect_semantics(
    session_factory, tmp_path, command_name
):
    case, broker, _, attempt = await _subscription_named_case(session_factory, tmp_path)
    await seed_remediation_phase(session_factory, attempt)
    arguments = {
        "token": "named-token",
        "provider_call_key": "named",
        "tool_name": ToolName.BUILD_RUN_NAMED_CHECK,
        "arguments": {"command_name": command_name},
    }
    receipt = await broker.invoke(**arguments)
    assert receipt.accepted is (command_name == "unit")
    assert await broker.invoke(**arguments) == receipt
    assert case.factory.calls == int(command_name == "unit")


@pytest.mark.integration
async def test_remediation_commit_keeps_both_durable_effect_phases(session_factory, tmp_path):
    broker, git, attempt, _, worktree = await _case(session_factory, tmp_path)
    await seed_remediation_phase(session_factory, attempt)
    arguments = {
        "token": "commit-token",
        "provider_call_key": "commit",
        "tool_name": ToolName.GIT_COMMIT,
        "arguments": {"message": "fix: bounded remediation checkpoint"},
    }
    before = git.head_sha(worktree)
    receipt = await broker.invoke(**arguments)
    assert receipt.accepted and receipt.result["status"] == "succeeded"
    after = git.head_sha(worktree)
    assert after != before
    assert await broker.invoke(**arguments) == receipt
    assert git.head_sha(worktree) == after
    async with PostgresUnitOfWork(session_factory) as work:
        call = await work.tool_calls.get(uuid5(attempt, "forge-subscription-tool-v1:commit"))
        preparation = await work.operations.get(call.operation_intent_id)
        publication = await work.operations.get_by_idempotency_key(f"git.commit:{call.id}:publish")
        assert preparation.request_payload["subscription_attempt_id"] == str(attempt)
        assert publication.request_payload["subscription_attempt_id"] == str(attempt)
        assert publication.id != preparation.id
