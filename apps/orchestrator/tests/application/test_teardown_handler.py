"""Execution boundary for exact, durable resource teardown."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from forge.application.ports.runs import RunQuiescence
from forge.application.services.projects import _digest as policy_digest
from forge.domain.command import CommandEnvelope, CommandStatus
from forge.domain.policy import ProjectPolicy
from forge.domain.resource import ResourceState
from forge.domain.run import RunSnapshot, RunState
from forge.domain.teardown import teardown_confirmation


def fixture(tmp_path, *, state=RunState.CANCELLED):
    run = RunSnapshot(
        id=uuid4(),
        project_id=uuid4(),
        task_id=uuid4(),
        state=state,
        policy_version=1,
        worktree_path=str(tmp_path / "worktree"),
        branch_name="forge/test",
        base_ref="main",
        base_sha="a" * 40,
        database_state=ResourceState.DISABLED,
    )
    policy = ProjectPolicy(
        id=run.project_id,
        version=1,
        repository_path=str(tmp_path),
        github_repository="owner/repo",
        default_branch="main",
    )
    command = CommandEnvelope(
        id=uuid4(),
        run_id=run.id,
        command_type="teardown_run_resources",
        idempotency_key="teardown",
        expected_run_version=run.version,
        status=CommandStatus.LEASED,
        payload_schema_version=1,
        attempt=1,
        available_at=datetime.now(UTC),
        lease_owner="worker",
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
        actor_id=uuid4(),
        payload={"delete_branch": False, "confirm_resource_identity": teardown_confirmation(run)},
    )
    events = []

    async def append(event):
        events.append(event)
        return event

    work = SimpleNamespace(
        runs=SimpleNamespace(
            get_for_update=AsyncMock(return_value=run),
            prove_quiescent=AsyncMock(return_value=RunQuiescence(0, 0, 0, 0, 0)),
        ),
        commands=SimpleNamespace(assert_current_lease=AsyncMock(return_value=command)),
        projects=SimpleNamespace(
            get_policy=AsyncMock(
                return_value=SimpleNamespace(
                    project_id=run.project_id,
                    version=1,
                    document_schema_version=1,
                    document=policy.model_dump(mode="json"),
                    policy_digest=policy_digest(policy.model_dump(mode="json")),
                )
            )
        ),
        events=SimpleNamespace(
            list_after=AsyncMock(side_effect=lambda *_: list(events)),
            append=AsyncMock(side_effect=append),
        ),
        commit=AsyncMock(),
    )
    removed = replace(run, version=run.version + 1, worktree_path=None)

    async def remove(run_id, supplied_policy):
        assert run_id == run.id and supplied_policy == policy
        assert events[0].event_type == "resource.teardown_admitted"
        assert work.commit.await_count >= 1
        work.runs.get_for_update.return_value = removed
        return removed

    teardown = AsyncMock(side_effect=remove)
    return run, command, work, teardown, events


@pytest.mark.asyncio
async def test_teardown_persists_admission_before_effect_and_completion_afterward(tmp_path):
    from forge.application.handlers.teardown import TeardownRunResourcesHandler

    run, command, work, teardown, events = fixture(tmp_path)
    await TeardownRunResourcesHandler(teardown)(command, work)
    assert [event.event_type for event in events] == [
        "resource.teardown_admitted",
        "resource.teardown_completed",
    ]
    assert events[0].payload["confirmation"] == teardown_confirmation(run)
    assert events[1].payload["source_command_id"] == str(command.id)
    teardown.assert_awaited_once()


@pytest.mark.asyncio
async def test_completion_replay_does_not_repeat_resource_effect(tmp_path):
    from forge.application.handlers.teardown import TeardownRunResourcesHandler

    _, command, work, teardown, events = fixture(tmp_path)
    handler = TeardownRunResourcesHandler(teardown)
    await handler(command, work)
    await handler(command, work)
    teardown.assert_awaited_once()
    assert len(events) == 2


@pytest.mark.asyncio
async def test_fresh_branch_removal_after_recorded_removal_has_no_effect(tmp_path):
    from forge.application.handlers.teardown import (
        TeardownCommandRejected,
        TeardownRunResourcesHandler,
    )
    from forge.domain.event import RunEvent

    run, command, work, teardown, events = fixture(tmp_path)
    command = replace(
        command,
        payload={
            **command.payload,
            "delete_branch": True,
            "confirm_branch_name": run.branch_name,
        },
    )
    work.commands.assert_current_lease.return_value = command
    events.append(
        RunEvent(
            run_id=run.id,
            run_version=run.version,
            event_type="resource.branch_removed",
            actor_class="worker",
            payload={"source_command_id": str(uuid4())},
        )
    )
    branches = SimpleNamespace(observe_head=AsyncMock(return_value="b" * 40), remove=AsyncMock())
    with pytest.raises(TeardownCommandRejected, match="already recorded"):
        await TeardownRunResourcesHandler(teardown, branches=branches)(command, work)
    teardown.assert_not_awaited()
    branches.observe_head.assert_not_awaited()
    branches.remove.assert_not_awaited()
    assert len(events) == 1


@pytest.mark.asyncio
async def test_crash_after_resource_checkpoint_replays_original_admission(tmp_path):
    from forge.application.handlers.teardown import TeardownRunResourcesHandler

    _, command, work, teardown, events = fixture(tmp_path)
    remove = teardown.side_effect

    async def crash(run_id, policy):
        await remove(run_id, policy)
        raise RuntimeError("simulated process loss")

    teardown.side_effect = crash
    handler = TeardownRunResourcesHandler(teardown)
    with pytest.raises(RuntimeError, match="requires recovery"):
        await handler(command, work)
    assert len(events) == 1
    teardown.side_effect = remove
    await handler(command, work)
    assert len(events) == 2
    assert teardown.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", [{"branch_name": "forge/other"}, {"worktree_path": "other"}, {"base_sha": "b" * 40}]
)
async def test_admitted_retry_rejects_rebound_resources(tmp_path, change):
    from forge.application.handlers.teardown import TeardownRunResourcesHandler
    from forge.application.ports.commands import CommandRecoveryRequired

    run, command, work, teardown, events = fixture(tmp_path)
    teardown.side_effect = RuntimeError("crash")
    handler = TeardownRunResourcesHandler(teardown)
    with pytest.raises(RuntimeError, match="requires recovery"):
        await handler(command, work)
    work.runs.get_for_update.return_value = replace(run, **change)
    with pytest.raises(CommandRecoveryRequired):
        await handler(command, work)
    assert len(events) == 1
    teardown.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "blocker",
    [
        "pending_or_leased_commands",
        "running_steps",
        "running_executions",
        "running_tools",
        "unresolved_operations",
    ],
)
@pytest.mark.parametrize("state", [RunState.CANCELLED, RunState.AWAITING_HUMAN_INTERVENTION])
async def test_execution_rechecks_quiescence_before_any_effect(tmp_path, blocker, state):
    from forge.application.handlers.teardown import TeardownRunResourcesHandler
    from forge.application.ports.commands import CommandRecoveryRequired

    _, command, work, teardown, events = fixture(tmp_path, state=state)
    work.runs.prove_quiescent.return_value = replace(RunQuiescence(0, 0, 0, 0, 0), **{blocker: 1})
    with pytest.raises(CommandRecoveryRequired, match="unsettled work"):
        await TeardownRunResourcesHandler(teardown)(command, work)
    teardown.assert_not_awaited()
    assert not events


@pytest.mark.asyncio
async def test_stale_confirmation_and_lease_substitution_cannot_remove_resources(tmp_path):
    from forge.application.handlers.teardown import (
        TeardownCommandRejected,
        TeardownRunResourcesHandler,
    )
    from forge.application.ports.commands import CommandRecoveryRequired

    run, command, work, teardown, events = fixture(tmp_path)
    handler = TeardownRunResourcesHandler(teardown)
    work.runs.get_for_update.return_value = replace(run, version=run.version + 1)
    with pytest.raises(TeardownCommandRejected, match="stale"):
        await handler(command, work)
    work.runs.get_for_update.return_value = run
    work.commands.assert_current_lease.return_value = replace(command, actor_id=uuid4())
    with pytest.raises(CommandRecoveryRequired, match="lease"):
        await handler(command, work)
    teardown.assert_not_awaited()
    assert not events


@pytest.mark.asyncio
async def test_provisioner_return_without_persisted_removal_is_not_completion(tmp_path):
    from forge.application.handlers.teardown import TeardownRunResourcesHandler
    from forge.application.ports.commands import CommandRecoveryRequired

    run, command, work, teardown, events = fixture(tmp_path)
    teardown.side_effect = None
    teardown.return_value = replace(run, worktree_path=None)
    with pytest.raises(CommandRecoveryRequired, match="not completed"):
        await TeardownRunResourcesHandler(teardown)(command, work)
    assert len(events) == 1


@pytest.mark.asyncio
async def test_corrupt_frozen_policy_cannot_remove_resources(tmp_path):
    from forge.application.handlers.teardown import (
        TeardownCommandRejected,
        TeardownRunResourcesHandler,
    )

    _, command, work, teardown, events = fixture(tmp_path)
    work.projects.get_policy.return_value.policy_digest = "f" * 64
    with pytest.raises(TeardownCommandRejected, match="policy identity"):
        await TeardownRunResourcesHandler(teardown)(command, work)
    teardown.assert_not_awaited()
    assert not events


@pytest.mark.asyncio
async def test_forged_completion_actor_does_not_authorize_replay(tmp_path):
    from forge.application.handlers.teardown import TeardownRunResourcesHandler
    from forge.application.ports.commands import CommandRecoveryRequired

    _, command, work, teardown, events = fixture(tmp_path)
    handler = TeardownRunResourcesHandler(teardown)
    await handler(command, work)
    events[-1] = replace(events[-1], actor_class="operator", actor_id=command.actor_id)
    with pytest.raises(CommandRecoveryRequired, match="completion"):
        await handler(command, work)
    teardown.assert_awaited_once()


@pytest.mark.asyncio
async def test_teardown_accepts_registered_policy_with_unicode_path(tmp_path):
    from forge.application.handlers.teardown import TeardownRunResourcesHandler

    _, command, work, teardown, events = fixture(tmp_path / "café")
    await TeardownRunResourcesHandler(teardown)(command, work)
    assert events[-1].event_type == "resource.teardown_completed"


@pytest.mark.asyncio
async def test_quiescent_intervention_run_can_teardown_its_recorded_resources(tmp_path):
    from forge.application.handlers.teardown import TeardownRunResourcesHandler

    _, command, work, teardown, events = fixture(
        tmp_path, state=RunState.AWAITING_HUMAN_INTERVENTION
    )
    await TeardownRunResourcesHandler(teardown)(command, work)
    teardown.assert_awaited_once()
    assert events[-1].event_type == "resource.teardown_completed"


@pytest.mark.asyncio
async def test_branch_confirmation_freezes_head_before_resource_effect_and_reuses_it(tmp_path):
    from forge.application.handlers.teardown import TeardownRunResourcesHandler

    run, command, work, teardown, events = fixture(tmp_path)
    command = replace(
        command,
        payload={**command.payload, "delete_branch": True, "confirm_branch_name": run.branch_name},
    )
    work.commands.assert_current_lease.return_value = command
    observed = []

    async def head(source, policy, supplied_work):
        assert supplied_work is work
        assert source == run and events == []
        observed.append("head")
        return "b" * 40

    async def remove(source, policy, expected_head):
        assert source == command and expected_head == "b" * 40
        assert work.runs.get_for_update.return_value.worktree_path is None
        assert events[0].payload["branch_expected_head"] == "b" * 40
        observed.append("delete")
        raise RuntimeError("interrupted branch operation")

    branches = SimpleNamespace(
        observe_head=AsyncMock(side_effect=head),
        remove=AsyncMock(side_effect=remove),
        validate_completed=AsyncMock(),
    )
    handler = TeardownRunResourcesHandler(teardown, branches=branches)
    with pytest.raises(RuntimeError, match="requires recovery"):
        await handler(command, work)
    assert observed == ["head", "delete"]
    branches.remove.side_effect = None
    branches.remove.return_value = work.runs.get_for_update.return_value
    await handler(command, work)
    await handler(command, work)
    branches.observe_head.assert_awaited_once()
    assert branches.remove.await_count == 2
    branches.validate_completed.assert_awaited_once()
