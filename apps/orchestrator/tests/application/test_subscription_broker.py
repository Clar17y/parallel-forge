import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from forge.agents.subscription_protocol import ProviderToolCall, tool_result_frame
from forge.application.services.subscription_broker import BrokerDenied, SubscriptionToolBroker
from forge.domain.scheduling import TaskEffectLease, TaskLease
from forge.domain.subscription import BrokerAuthorizationBinding, SpecialistPurpose
from forge.domain.tool import ToolCallStatus, ToolName, ToolResult


class Subscription:
    def __init__(self):
        self.binding = None
        self.receipt = None

    async def bind_operation(self, binding, **_):
        if self.binding and (
            self.binding.tool_name != binding.tool_name
            or self.binding.arguments_digest != binding.arguments_digest
        ):
            raise ValueError("conflict")
        self.binding = self.binding or binding
        return self.binding

    async def record_operation_receipt(self, binding, *, receipt, **_):
        self.receipt = receipt
        return binding

    async def operation_receipt(self, _binding, **_):
        return self.receipt


class Scheduler:
    def __init__(self):
        self.admitted = []
        self.settled = []

    async def admit_effect(self, lease, effect_id, **_):
        if effect_id not in self.admitted:
            self.admitted.append(effect_id)
        return TaskEffectLease(effect_id=effect_id, task_lease=lease, candidate_epoch=0)

    async def settle_effect(self, effect, *, accepted):
        self.settled.append((effect.effect_id, accepted))
        return accepted

    async def reconcile_effect(self, effect):
        self.reconciled = effect.effect_id


class Work:
    def __init__(self, subscription, scheduler):
        self.subscription, self.scheduler = subscription, scheduler

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def commit(self):
        pass


def test_callback_replay_has_one_operation_and_conflicting_arguments_are_denied():
    run_id, task_id, attempt_id = uuid4(), uuid4(), uuid4()
    lease = TaskLease(
        run_id=run_id,
        task_id=task_id,
        owner="worker",
        generation=1,
        expires_at=datetime.now(UTC) + timedelta(seconds=10),
    )
    auth = BrokerAuthorizationBinding(
        run_id=run_id,
        task_id=task_id,
        attempt_id=attempt_id,
        worktree_id="forge-test",
        role=SpecialistPurpose.ROUTINE_IMPLEMENTATION,
        policy_version=1,
        permitted_tools=frozenset({ToolName.REPOSITORY_READ_FILE}),
        broker_token="opaque",
    )
    subscription, scheduler = Subscription(), Scheduler()

    async def effect(_operation_id, name, _arguments):
        return ToolResult(tool_name=name, status=ToolCallStatus.SUCCEEDED)

    broker = SubscriptionToolBroker(
        lambda: Work(subscription, scheduler), lease=lease, authority=auth, effect=effect
    )
    first = asyncio.run(
        broker.invoke(
            token="opaque",
            provider_call_key="call-1",
            tool_name=ToolName.REPOSITORY_READ_FILE,
            arguments={"path": "a"},
        )
    )
    replay = asyncio.run(
        broker.invoke(
            token="opaque",
            provider_call_key="call-1",
            tool_name=ToolName.REPOSITORY_READ_FILE,
            arguments={"path": "a"},
        )
    )
    assert first.operation_id == replay.operation_id
    assert len(scheduler.admitted) == 1
    with pytest.raises(BrokerDenied):
        asyncio.run(
            broker.invoke(
                token="opaque",
                provider_call_key="call-1",
                tool_name=ToolName.REPOSITORY_READ_FILE,
                arguments={"path": "b"},
            )
        )


@pytest.mark.parametrize("token", ["wrong", "\u2603", "\ud800", "", None, b"opaque"])
def test_wrong_token_is_denied_before_persistence(token):
    run_id, task_id, attempt_id = uuid4(), uuid4(), uuid4()
    lease = TaskLease(
        run_id=run_id, task_id=task_id, owner="worker", generation=1, expires_at=datetime.now(UTC)
    )
    auth = BrokerAuthorizationBinding(
        run_id=run_id,
        task_id=task_id,
        attempt_id=attempt_id,
        worktree_id="forge-test",
        role=SpecialistPurpose.ROUTINE_IMPLEMENTATION,
        policy_version=1,
        permitted_tools=frozenset({ToolName.REPOSITORY_READ_FILE}),
        broker_token="opaque",
    )

    async def effect(_operation_id, name, _arguments):
        return ToolResult(tool_name=name, status=ToolCallStatus.SUCCEEDED)

    broker = SubscriptionToolBroker(
        lambda: Work(Subscription(), Scheduler()), lease=lease, authority=auth, effect=effect
    )
    with pytest.raises(BrokerDenied):
        asyncio.run(
            broker.invoke(
                token=token,
                provider_call_key="call",
                tool_name=ToolName.REPOSITORY_READ_FILE,
                arguments={},
            )
        )


def test_revoke_denies_new_calls_without_settling_existing_effects():
    run_id, task_id, attempt_id = uuid4(), uuid4(), uuid4()
    lease = TaskLease(
        run_id=run_id,
        task_id=task_id,
        owner="worker",
        generation=1,
        expires_at=datetime.now(UTC) + timedelta(seconds=10),
    )
    auth = BrokerAuthorizationBinding(
        run_id=run_id,
        task_id=task_id,
        attempt_id=attempt_id,
        worktree_id="forge-test",
        role=SpecialistPurpose.ROUTINE_IMPLEMENTATION,
        policy_version=1,
        permitted_tools=frozenset({ToolName.REPOSITORY_READ_FILE}),
        broker_token="opaque",
    )

    async def effect(_operation_id, name, _arguments):
        return ToolResult(tool_name=name, status=ToolCallStatus.SUCCEEDED)

    broker = SubscriptionToolBroker(
        lambda: Work(Subscription(), Scheduler()), lease=lease, authority=auth, effect=effect
    )
    asyncio.run(broker.revoke())
    with pytest.raises(BrokerDenied):
        asyncio.run(
            broker.invoke(
                token="opaque",
                provider_call_key="call",
                tool_name=ToolName.REPOSITORY_READ_FILE,
                arguments={"path": "a"},
            )
        )


def test_failure_is_not_accepted_from_arbitrary_success_field():
    run_id, task_id, attempt_id = uuid4(), uuid4(), uuid4()
    lease = TaskLease(
        run_id=run_id,
        task_id=task_id,
        owner="worker",
        generation=1,
        expires_at=datetime.now(UTC) + timedelta(seconds=10),
    )
    auth = BrokerAuthorizationBinding(
        run_id=run_id,
        task_id=task_id,
        attempt_id=attempt_id,
        worktree_id="forge-test",
        role=SpecialistPurpose.ROUTINE_IMPLEMENTATION,
        policy_version=1,
        permitted_tools=frozenset({ToolName.REPOSITORY_READ_FILE}),
        broker_token="opaque",
    )

    async def effect(_operation_id, name, _arguments):
        from forge.domain.tool import ToolError, ToolErrorCode

        return ToolResult(
            tool_name=name,
            status=ToolCallStatus.FAILED,
            error=ToolError(code=ToolErrorCode.OPERATION_ERROR, message="failed"),
        )

    scheduler = Scheduler()
    result = asyncio.run(
        SubscriptionToolBroker(
            lambda: Work(Subscription(), scheduler), lease=lease, authority=auth, effect=effect
        ).invoke(
            token="opaque",
            provider_call_key="call",
            tool_name=ToolName.REPOSITORY_READ_FILE,
            arguments={"path": "a"},
        )
    )
    assert result.accepted is False
    assert scheduler.settled[-1][1] is False


def test_provider_adapter_returns_the_protocols_top_level_result_shape():
    run_id, task_id, attempt_id = uuid4(), uuid4(), uuid4()
    lease = TaskLease(
        run_id=run_id,
        task_id=task_id,
        owner="worker",
        generation=1,
        expires_at=datetime.now(UTC) + timedelta(seconds=10),
    )
    auth = BrokerAuthorizationBinding(
        run_id=run_id,
        task_id=task_id,
        attempt_id=attempt_id,
        worktree_id="forge-test",
        role=SpecialistPurpose.ROUTINE_IMPLEMENTATION,
        policy_version=1,
        permitted_tools=frozenset({ToolName.REPOSITORY_READ_FILE}),
        broker_token="opaque",
    )

    async def effect(_operation_id, name, _arguments):
        return ToolResult(tool_name=name, status=ToolCallStatus.SUCCEEDED)

    subscription, scheduler = Subscription(), Scheduler()
    result = asyncio.run(
        SubscriptionToolBroker(
            lambda: Work(subscription, scheduler), lease=lease, authority=auth, effect=effect
        )(
            ProviderToolCall(
                call_key="call",
                thread_id="thread",
                turn_id="turn",
                name=ToolName.REPOSITORY_READ_FILE.value,
                arguments={"path": "a"},
            )
        )
    )
    assert (
        tool_result_frame(
            ProviderToolCall(
                call_key="call",
                thread_id="thread",
                turn_id="turn",
                name=ToolName.REPOSITORY_READ_FILE.value,
                arguments={"path": "a"},
            ),
            result,
            "request",
        )["result"]["success"]
        is True
    )


def test_effect_executes_between_short_lived_unit_of_work_scopes():
    run_id, task_id, attempt_id = uuid4(), uuid4(), uuid4()
    lease = TaskLease(
        run_id=run_id,
        task_id=task_id,
        owner="worker",
        generation=1,
        expires_at=datetime.now(UTC) + timedelta(seconds=10),
    )
    auth = BrokerAuthorizationBinding(
        run_id=run_id,
        task_id=task_id,
        attempt_id=attempt_id,
        worktree_id="forge-test",
        role=SpecialistPurpose.ROUTINE_IMPLEMENTATION,
        policy_version=1,
        permitted_tools=frozenset({ToolName.REPOSITORY_READ_FILE}),
        broker_token="opaque",
    )
    subscription, scheduler, active = Subscription(), Scheduler(), set()

    class TrackedWork(Work):
        async def __aenter__(self):
            active.add(self)
            return self

        async def __aexit__(self, *_):
            active.remove(self)
            return False

    def factory():
        return TrackedWork(subscription, scheduler)

    async def effect(_operation_id, name, _arguments):
        assert not active  # No open transaction or UoW spans the effect.
        return ToolResult(tool_name=name, status=ToolCallStatus.SUCCEEDED)

    asyncio.run(
        SubscriptionToolBroker(factory, lease=lease, authority=auth, effect=effect).invoke(
            token="opaque",
            provider_call_key="call",
            tool_name=ToolName.REPOSITORY_READ_FILE,
            arguments={"path": "a"},
        )
    )
    assert not active


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,arguments,explicit", [
    (ToolName.GIT_COMMIT, {"message": "checkpoint"}, False),
    (ToolName.GIT_DIFF, {"scope": "snapshot"}, False),
    (ToolName.BUILD_RUN_NAMED_CHECK, {"command_name": "test"}, False),
    (ToolName.REPOSITORY_READ_FILE, {"path": "apps/file"}, True),
])
async def test_concurrent_terminal_receipt_replay_preserves_exclusive_fence(tool, arguments, explicit):
    class ExactScopeScheduler(Scheduler):
        async def admit_effect(self, lease, effect_id, *, whole_worktree_exclusive=False, **kwargs):
            assert whole_worktree_exclusive is True, "exclusive replay cannot downgrade scope"
            return await super().admit_effect(lease, effect_id, **kwargs)

    run_id, task_id, attempt_id = uuid4(), uuid4(), uuid4()
    lease = TaskLease(run_id=run_id, task_id=task_id, owner="worker", generation=1,
                      expires_at=datetime.now(UTC) + timedelta(seconds=10))
    authority = BrokerAuthorizationBinding(
        run_id=run_id, task_id=task_id, attempt_id=attempt_id, worktree_id="tree",
        role=SpecialistPurpose.INTEGRATION, policy_version=1,
        permitted_tools=frozenset({tool}), broker_token="opaque",
    )
    entered = 0
    both = asyncio.Event()
    async def effect(_operation_id, name, _arguments):
        nonlocal entered
        entered += 1
        if entered == 2:
            both.set()
        await both.wait()
        return ToolResult(tool_name=name, status=ToolCallStatus.SUCCEEDED)
    subscription, scheduler = Subscription(), ExactScopeScheduler()
    broker = SubscriptionToolBroker(
        lambda: Work(subscription, scheduler), lease=lease, authority=authority,
        effect=effect, whole_worktree_exclusive=explicit,
    )
    async with asyncio.timeout(2):
        results = await asyncio.gather(*[broker.invoke(
            token="opaque", provider_call_key="same", tool_name=tool, arguments=arguments,
        ) for _ in range(2)])
    assert results[0] == results[1]
    assert len(scheduler.settled) == 1
