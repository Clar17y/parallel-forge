"""External observation/verification precede one durable application transaction."""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from forge.application.services.subscription_handoff_application import (
    SubscriptionHandoffApplication,
)
from test_subscription_handoff import case


@pytest.mark.parametrize("failure", [None, "snapshot", "verify", "apply"])
async def test_handoff_pipeline_releases_observation_on_every_outcome(failure):
    events = []
    attempt = uuid4()
    _, _, handoff, kwargs = case()
    proposal = SimpleNamespace(
        handoff=handoff,
        selected_candidate=None,
        task=kwargs["task"],
        policy=SimpleNamespace(version=1),
        worktree=SimpleNamespace(identity=SimpleNamespace(worktree_name="tree"), base_sha="a" * 40),
    )
    observation = SimpleNamespace(proposal=proposal)
    snapshot, proof, result = object(), object(), object()

    async def begin(identity, token):
        assert identity == attempt
        events.append("begin")
        return observation

    async def replay(identity):
        return None

    async def capture(value):
        assert value is proposal
        events.append("snapshot")
        if failure == "snapshot":
            raise ValueError("snapshot failed")
        return snapshot

    async def verify(handoff, **kwargs):
        assert handoff is proposal.handoff and kwargs["current_snapshot"] is snapshot
        assert kwargs["worktree_id"] == kwargs["resource_id"] == "tree"
        events.append("verify")
        return None if failure == "verify" else proof

    async def apply(value, supplied):
        assert value is observation and supplied is proof
        events.append("apply")
        if failure == "apply":
            raise ValueError("source changed")
        return result

    async def release(value):
        assert value is observation
        events.append("release")
        return True

    service = SubscriptionHandoffApplication(lambda: None, SimpleNamespace(assess=verify), capture)
    service._decisions = SimpleNamespace(
        handoff_replay=replay,
        begin_handoff_observation=begin,
        apply_handoff=apply,
        release_handoff_observation=release,
    )
    if failure:
        with pytest.raises(ValueError):
            await service.apply(attempt)
    else:
        assert await service.apply(attempt) is result
    assert events[0] == "begin" and events[-1] == "release"
    if failure in {"snapshot", "verify"}:
        assert "apply" not in events


async def test_repeated_cancellation_waits_for_owned_fence_release():
    entered, releasing, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
    _, _, handoff, kwargs = case()
    observation = SimpleNamespace(proposal=SimpleNamespace(handoff=handoff, task=kwargs["task"], selected_candidate=None))
    released = []

    async def begin(*args):
        return observation

    async def replay(*args):
        return None

    async def snapshot(*args):
        entered.set()
        await asyncio.Event().wait()

    async def release(value):
        releasing.set()
        await finish.wait()
        released.append(value)
        return True

    service = SubscriptionHandoffApplication(lambda: None, object(), snapshot)
    service._decisions = SimpleNamespace(
        handoff_replay=replay, begin_handoff_observation=begin, release_handoff_observation=release
    )
    task = asyncio.create_task(service.apply(uuid4()))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    await asyncio.wait_for(releasing.wait(), 1)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert released == [observation]


async def test_applied_handoff_replays_without_another_observation():
    result = object()

    async def replay(identity):
        return result

    service = SubscriptionHandoffApplication(lambda: None, object(), None)
    service._decisions = SimpleNamespace(handoff_replay=replay)
    assert await service.apply(uuid4()) is result
