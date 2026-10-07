"""Durable, owner-enabled dispatch through the ordinary launch admission path."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from forge.application.services.epic_budget import EpicBudgetEdit, EpicBudgetService
from forge.application.services.epic_dispatch import (
    EpicDispatchConflict,
    EpicDispatchRequest,
    EpicDispatchService,
)
from forge.application.services.epic_dispatch_worker import EpicDispatchWorker
from forge.application.services.epic_eligibility import EpicItemEligibility
from forge.domain.epic_run_bridge import EpicExecutionBindingConflict, EpicLaunchConflict
from forge.domain.subscription import TaskBudget
from forge.persistence.models import (
    AgentExecution,
    ModelUsage,
    OperatorAuditEvent,
    Run,
    RunCommand,
    Task,
)
from forge.persistence.models.epic_brief import Epic
from forge.persistence.models.epic_dispatch import EpicDispatchSetting
from forge.persistence.models.epic_run_bridge import EpicChildBudgetHold, EpicExecutionControl
from forge.persistence.repositories.mutations import MutationConflict, PostgresMutationRepository
from forge.persistence.repositories.subscription import SubscriptionProfileNotFound
from sqlalchemy import func, select

from apps.orchestrator.tests.epic_run_bridge.test_launch import BridgeWork, setup


@pytest.fixture
def bridge_factory(session_factory):
    return lambda: BridgeWork(session_factory)


@pytest.mark.asyncio
async def test_dispatch_is_disabled_until_owner_configures_epoch(session_factory, bridge_factory):
    bridge, _, actor, epic_id, _, _, _ = await setup(session_factory, bridge_factory)
    execution = await bridge.start(
        actor=actor, epic_id=epic_id, idempotency_key="execution", expected_epic_version=5
    )
    dispatch = EpicDispatchService(bridge_factory)
    state = await dispatch.get(epic_id, execution.execution_id)
    assert state.version == 0
    assert state.enabled is False


class ReadyEligibility:
    def __init__(self, item_id):
        self.item_id = item_id

    async def readiness(self, *, epic_id, execution_id):
        return (EpicItemEligibility(
            item_id=self.item_id, disposition="required", status="ready",
            blocker_code=None, dependency_evidence=(), completion_evidence=None,
        ),)


class VerifiedEligibility(ReadyEligibility):
    async def readiness(self, *, epic_id, execution_id):
        return (EpicItemEligibility(
            item_id=self.item_id, disposition="required", status="verified",
            blocker_code=None, dependency_evidence=(), completion_evidence=None,
        ),)


class UnavailableEligibility(ReadyEligibility):
    async def readiness(self, *, epic_id, execution_id):
        raise RuntimeError("probe unavailable")


class TwoVerifiedEligibility(ReadyEligibility):
    def __init__(self, first_id, second_id):
        self.item_ids = (first_id, second_id)

    async def readiness(self, *, epic_id, execution_id):
        return tuple(EpicItemEligibility(
            item_id=item_id, disposition="required", status="verified",
            blocker_code=None, dependency_evidence=(), completion_evidence=None,
        ) for item_id in self.item_ids)


class TwoReadyEligibility(TwoVerifiedEligibility):
    first_active = False

    async def readiness(self, *, epic_id, execution_id):
        items = await super().readiness(epic_id=epic_id, execution_id=execution_id)
        return tuple(item.model_copy(update={"status": "active" if self.first_active and index == 0 else "ready"})
                     for index, item in enumerate(items))


class FailedAndReadyEligibility(TwoReadyEligibility):
    async def readiness(self, *, epic_id, execution_id):
        first, second = await super().readiness(epic_id=epic_id, execution_id=execution_id)
        return first.model_copy(update={"status": "blocked", "blocker_code": "predecessor_failed"}), second


class FailedNoReadyEligibility(FailedAndReadyEligibility):
    async def readiness(self, *, epic_id, execution_id):
        first, second = await super().readiness(epic_id=epic_id, execution_id=execution_id)
        return first, second.model_copy(update={"status": "blocked", "blocker_code": "predecessor_unverified"})


@pytest.mark.asyncio
@pytest.mark.parametrize("configured", [False, True])
@pytest.mark.parametrize(
    ("verified", "effects_settled"), [(True, True), (True, False), (False, True)]
)
async def test_manual_or_disabled_completion_requires_verified_settled_child(
    session_factory, bridge_factory, configured, verified, effects_settled
):
    bridge, _, actor, epic_id, first_id, _, request = await setup(session_factory, bridge_factory)
    child = await bridge.launch(actor=actor, epic_id=epic_id, idempotency_key="manual", request=request())
    dispatch = EpicDispatchService(bridge_factory)
    if configured:
        await dispatch.configure(
            actor=actor, epic_id=epic_id, execution_id=child.execution_id,
            idempotency_key="disabled", request=EpicDispatchRequest(expected_dispatch_version=0, enabled=False),
        )
    async with session_factory() as session, session.begin():
        run = await session.get(Run, child.run_id)
        run.state = "COMPLETED"
        command = (await session.scalars(select(RunCommand).where(RunCommand.run_id == child.run_id))).one()
        command.status = "COMPLETED"
        command.completed_at = datetime.now(UTC)
        hold = await session.get(EpicChildBudgetHold, child.attempt_id)
        hold.effects_settled = effects_settled
        control = await session.get(EpicExecutionControl, child.execution_id)
        control.updated_at = datetime.now(UTC) - timedelta(seconds=2)
    class ConcurrentReadiness(ReadyEligibility):
        arrivals = 0
        ready = asyncio.Event()

        async def readiness(self, *, epic_id, execution_id):
            self.arrivals += 1
            if self.arrivals == 2:
                self.ready.set()
            await self.ready.wait()
            result = await super().readiness(epic_id=epic_id, execution_id=execution_id)
            if verified:
                return tuple(item.model_copy(update={"status": "verified"}) for item in result)
            return result

    worker = EpicDispatchWorker(bridge_factory, bridge=bridge, eligibility=ConcurrentReadiness(first_id))
    assert await asyncio.gather(worker.run_once(), worker.run_once()) == [
        child.execution_id, child.execution_id
    ]
    async with bridge_factory() as work:
        control = await work.session.get(EpicExecutionControl, child.execution_id)
        assert (control.state, control.version, control.blocker_code) == (
            ("SUCCEEDED", 2, None) if verified and effects_settled else ("ACTIVE", 1, None)
        )
        assert len(await work.epic_run_bridge.list_attempts(epic_id, execution_id=child.execution_id)) == 1
        assert await work.session.scalar(select(func.count()).select_from(Task)) == 1
        assert await work.session.scalar(select(func.count()).select_from(Run)) == 1
        assert await work.session.scalar(select(func.count()).select_from(RunCommand)) == 1
        setting = await work.session.get(EpicDispatchSetting, child.execution_id)
        assert (setting is not None) == configured
        if setting is not None:
            assert setting.version == 1 and not setting.enabled
        await work.commit()
    assert not (await dispatch.get(epic_id, child.execution_id)).enabled
    assert await asyncio.gather(worker.run_once(), worker.run_once()) == [None, None]


@pytest.mark.asyncio
@pytest.mark.parametrize("run_state", ["FAILED", "CANCELLED"])
@pytest.mark.parametrize("configured", [False, True])
async def test_manual_or_disabled_failed_required_child_blocks_ready_sibling(
    session_factory, bridge_factory, configured, run_state
):
    bridge, _, actor, epic_id, first_id, second_id, request = await setup(
        session_factory, bridge_factory, independent=True
    )
    child = await bridge.launch(actor=actor, epic_id=epic_id, idempotency_key="manual", request=request())
    if configured:
        await EpicDispatchService(bridge_factory).configure(
            actor=actor, epic_id=epic_id, execution_id=child.execution_id,
            idempotency_key="disabled", request=EpicDispatchRequest(expected_dispatch_version=0, enabled=False),
        )
    async with session_factory() as session, session.begin():
        (await session.get(Run, child.run_id)).state = run_state
        control = await session.get(EpicExecutionControl, child.execution_id)
        control.updated_at = datetime.now(UTC) - timedelta(seconds=2)
    worker = EpicDispatchWorker(
        bridge_factory, bridge=bridge, eligibility=FailedAndReadyEligibility(first_id, second_id)
    )
    assert await worker.run_once() == child.execution_id
    async with bridge_factory() as work:
        control = await work.session.get(EpicExecutionControl, child.execution_id)
        assert (control.state, control.version, control.blocker_code) == (
            "BLOCKED", 2, "predecessor_failed"
        )
        assert len(await work.epic_run_bridge.list_attempts(epic_id, execution_id=child.execution_id)) == 1
        for model in (Task, Run, RunCommand):
            assert await work.session.scalar(select(func.count()).select_from(model)) == 1
        setting = await work.session.get(EpicDispatchSetting, child.execution_id)
        assert (setting is not None) == configured
        if setting is not None:
            assert setting.version == 1 and not setting.enabled
        await work.commit()
    assert await asyncio.gather(worker.run_once(), worker.run_once()) == [None, None]


@pytest.mark.asyncio
async def test_manual_active_child_remains_active_without_auto_admission(session_factory, bridge_factory):
    bridge, _, actor, epic_id, first_id, _, request = await setup(session_factory, bridge_factory)
    child = await bridge.launch(actor=actor, epic_id=epic_id, idempotency_key="manual", request=request())
    async with session_factory() as session, session.begin():
        control = await session.get(EpicExecutionControl, child.execution_id)
        control.updated_at = datetime.now(UTC) - timedelta(seconds=2)
    worker = EpicDispatchWorker(bridge_factory, bridge=bridge, eligibility=ReadyEligibility(first_id))
    assert await worker.run_once() == child.execution_id
    assert await worker.run_once() is None
    async with bridge_factory() as work:
        control = await work.session.get(EpicExecutionControl, child.execution_id)
        assert (control.state, control.version) == ("ACTIVE", 1)
        assert len(await work.epic_run_bridge.list_attempts(epic_id, execution_id=child.execution_id)) == 1
        await work.commit()


@pytest.mark.asyncio
async def test_unavailable_manual_proof_yields_poll_to_enabled_epoch(session_factory, bridge_factory):
    bridge, _, actor, manual_epic, _, _, _ = await setup(session_factory, bridge_factory)
    manual = await bridge.start(
        actor=actor, epic_id=manual_epic, idempotency_key="manual", expected_epic_version=5
    )
    _, _, enabled_actor, enabled_epic, enabled_item, _, _ = await setup(
        session_factory, bridge_factory
    )
    enabled = await bridge.start(
        actor=enabled_actor, epic_id=enabled_epic,
        idempotency_key="enabled", expected_epic_version=5,
    )
    await EpicDispatchService(bridge_factory).configure(
        actor=enabled_actor, epic_id=enabled_epic, execution_id=enabled.execution_id,
        idempotency_key="enable", request=EpicDispatchRequest(expected_dispatch_version=0, enabled=True),
    )
    async with session_factory() as session, session.begin():
        (await session.get(EpicExecutionControl, manual.execution_id)).updated_at = (
            datetime.now(UTC) - timedelta(seconds=10)
        )
        (await session.get(EpicDispatchSetting, enabled.execution_id)).checked_at = (
            datetime.now(UTC) - timedelta(seconds=5)
        )

    class PerEpochEligibility:
        async def readiness(self, *, epic_id, execution_id):
            if execution_id == manual.execution_id:
                raise RuntimeError("manual proof unavailable")
            return await ReadyEligibility(enabled_item).readiness(
                epic_id=epic_id, execution_id=execution_id
            )

    worker = EpicDispatchWorker(bridge_factory, bridge=bridge, eligibility=PerEpochEligibility())
    assert await worker.run_once() == manual.execution_id
    assert await worker.run_once() == enabled.execution_id
    async with bridge_factory() as work:
        assert (await work.session.get(EpicExecutionControl, manual.execution_id)).state == "ACTIVE"
        assert await work.epic_run_bridge.list_attempts(manual_epic, execution_id=manual.execution_id) == []
        assert len(await work.epic_run_bridge.list_attempts(enabled_epic, execution_id=enabled.execution_id)) == 1
        await work.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("authority", ["manual", "disabled", "enabled"])
@pytest.mark.parametrize("run_state", ["FAILED", "CANCELLED"])
async def test_unavailable_proof_still_blocks_known_required_failure(
    session_factory, bridge_factory, authority, run_state
):
    bridge, _, actor, epic_id, first_id, _, request = await setup(session_factory, bridge_factory)
    child = await bridge.launch(actor=actor, epic_id=epic_id, idempotency_key="child", request=request())
    if authority != "manual":
        await EpicDispatchService(bridge_factory).configure(
            actor=actor, epic_id=epic_id, execution_id=child.execution_id,
            idempotency_key="configured",
            request=EpicDispatchRequest(
                expected_dispatch_version=0, enabled=authority == "enabled"
            ),
        )
    async with session_factory() as session, session.begin():
        (await session.get(Run, child.run_id)).state = run_state
        (await session.get(EpicExecutionControl, child.execution_id)).updated_at = (
            datetime.now(UTC) - timedelta(seconds=2)
        )
    worker = EpicDispatchWorker(
        bridge_factory, bridge=bridge, eligibility=UnavailableEligibility(first_id)
    )
    assert await worker.run_once() == child.execution_id
    async with bridge_factory() as work:
        control = await work.session.get(EpicExecutionControl, child.execution_id)
        assert (control.state, control.version, control.blocker_code) == (
            "BLOCKED", 2, "predecessor_failed"
        )
        setting = await work.session.get(EpicDispatchSetting, child.execution_id)
        assert (setting is None) == (authority == "manual")
        if setting is not None:
            assert (setting.version, setting.enabled, setting.blocker_code) == (
                1, False, "predecessor_failed"
            )
        assert len(await work.epic_run_bridge.list_attempts(epic_id, execution_id=child.execution_id)) == 1
        for model in (Task, Run, RunCommand):
            assert await work.session.scalar(select(func.count()).select_from(model)) == 1
        await work.commit()
    assert await asyncio.gather(worker.run_once(), worker.run_once()) == [None, None]


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_unavailable_proof_preserves_newer_owner_retry(
    session_factory, bridge_factory, enabled
):
    bridge, _, actor, epic_id, first_id, _, request = await setup(session_factory, bridge_factory)
    first = await bridge.launch(actor=actor, epic_id=epic_id, idempotency_key="first", request=request())
    if enabled:
        await EpicDispatchService(bridge_factory).configure(
            actor=actor, epic_id=epic_id, execution_id=first.execution_id,
            idempotency_key="enabled",
            request=EpicDispatchRequest(expected_dispatch_version=0, enabled=True),
        )
    async with session_factory() as session, session.begin():
        (await session.get(Run, first.run_id)).state = "FAILED"
        (await session.get(EpicExecutionControl, first.execution_id)).updated_at = (
            datetime.now(UTC) - timedelta(seconds=2)
        )

    class RetryThenUnavailable:
        async def readiness(self, *, epic_id, execution_id):
            await bridge.launch(
                actor=actor, epic_id=epic_id, idempotency_key="owner-retry",
                request=request(first_id, execution_id=execution_id, owner_override=True),
            )
            raise RuntimeError("proof unavailable after owner retry")

    worker = EpicDispatchWorker(bridge_factory, bridge=bridge, eligibility=RetryThenUnavailable())
    assert await worker.run_once() == first.execution_id
    async with bridge_factory() as work:
        control = await work.session.get(EpicExecutionControl, first.execution_id)
        assert (control.state, control.version, control.blocker_code) == ("ACTIVE", 1, None)
        attempts = await work.epic_run_bridge.list_attempts(epic_id, execution_id=first.execution_id)
        assert len(attempts) == 2 and attempts[1].attempt_number == 2
        setting = await work.session.get(EpicDispatchSetting, first.execution_id)
        assert (setting is not None) == enabled
        if setting is not None:
            assert setting.enabled and setting.blocker_code == "readiness_unavailable"
        await work.commit()


@pytest.mark.asyncio
async def test_disable_during_readiness_still_reconciles_terminal_child(session_factory, bridge_factory):
    bridge, _, actor, epic_id, first_id, _, request = await setup(session_factory, bridge_factory)
    child = await bridge.launch(actor=actor, epic_id=epic_id, idempotency_key="manual", request=request())
    dispatch = EpicDispatchService(bridge_factory)
    await dispatch.configure(
        actor=actor, epic_id=epic_id, execution_id=child.execution_id,
        idempotency_key="enabled", request=EpicDispatchRequest(expected_dispatch_version=0, enabled=True),
    )
    async with session_factory() as session, session.begin():
        (await session.get(Run, child.run_id)).state = "COMPLETED"
        command = (await session.scalars(select(RunCommand).where(RunCommand.run_id == child.run_id))).one()
        command.status = "COMPLETED"
        command.completed_at = datetime.now(UTC)
        (await session.get(EpicChildBudgetHold, child.attempt_id)).effects_settled = True

    class DisableDuringReadiness(VerifiedEligibility):
        async def readiness(self, *, epic_id, execution_id):
            await dispatch.configure(
                actor=actor, epic_id=epic_id, execution_id=execution_id,
                idempotency_key="disabled",
                request=EpicDispatchRequest(expected_dispatch_version=1, enabled=False),
            )
            return await super().readiness(epic_id=epic_id, execution_id=execution_id)

    worker = EpicDispatchWorker(
        bridge_factory, bridge=bridge, eligibility=DisableDuringReadiness(first_id)
    )
    assert await worker.run_once() == child.execution_id
    async with bridge_factory() as work:
        control = await work.session.get(EpicExecutionControl, child.execution_id)
        assert (control.state, control.version) == ("SUCCEEDED", 2)
        assert len(await work.epic_run_bridge.list_attempts(epic_id, execution_id=child.execution_id)) == 1
        await work.commit()
    assert not (await dispatch.get(epic_id, child.execution_id)).enabled


@pytest.mark.asyncio
@pytest.mark.parametrize("run_state", ["FAILED", "CANCELLED"])
@pytest.mark.parametrize("effects_settled", [False, True])
@pytest.mark.parametrize("sibling_ready", [False, True])
async def test_failed_required_child_blocks_independent_ready_sibling(
    session_factory, bridge_factory, run_state, effects_settled, sibling_ready
):
    bridge, _, actor, epic_id, first_id, second_id, request = await setup(
        session_factory, bridge_factory, independent=True
    )
    first = await bridge.launch(actor=actor, epic_id=epic_id, idempotency_key="first", request=request())
    async with session_factory() as session, session.begin():
        run = await session.get(Run, first.run_id)
        run.state = run_state
        command = (await session.scalars(select(RunCommand).where(RunCommand.run_id == first.run_id))).one()
        command.status = "COMPLETED"
        command.completed_at = datetime.now(UTC)
        hold = await session.get(EpicChildBudgetHold, first.attempt_id)
        hold.effects_settled = effects_settled
    dispatch = EpicDispatchService(bridge_factory)
    await dispatch.configure(
        actor=actor, epic_id=epic_id, execution_id=first.execution_id,
        idempotency_key="enable", request=EpicDispatchRequest(expected_dispatch_version=0, enabled=True),
    )
    worker = EpicDispatchWorker(
        bridge_factory, bridge=bridge,
        eligibility=(FailedAndReadyEligibility if sibling_ready else FailedNoReadyEligibility)(
            first_id, second_id
        ),
    )
    assert await worker.run_once() == first.execution_id
    state = await dispatch.get(epic_id, first.execution_id)
    assert state.enabled is False and state.blocker_code == "predecessor_failed"
    async with bridge_factory() as work:
        control = await work.session.get(EpicExecutionControl, first.execution_id)
        assert control.state == "BLOCKED" and control.blocker_code == "predecessor_failed"
        assert control.version == 2
        attempts = await work.epic_run_bridge.list_attempts(epic_id, execution_id=first.execution_id)
        assert len(attempts) == 1 and attempts[0].attempt_id == first.attempt_id
        assert (await work.session.get(EpicChildBudgetHold, first.attempt_id)).effects_settled == effects_settled
        for model in (Task, Run, RunCommand):
            assert await work.session.scalar(select(func.count()).select_from(model)) == 1
        await work.commit()
    assert await asyncio.gather(worker.run_once(), worker.run_once()) == [None, None]
    async with bridge_factory() as work:
        assert (await work.session.get(EpicExecutionControl, first.execution_id)).version == 2
        assert len(await work.epic_run_bridge.list_attempts(epic_id, execution_id=first.execution_id)) == 1
        await work.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_sibling", [False, True])
async def test_failure_after_claim_refuses_bridge_and_owner_can_retry_without_note(
    session_factory, bridge_factory, owner_sibling
):
    bridge, _, actor, epic_id, first_id, second_id, request = await setup(
        session_factory, bridge_factory, independent=True
    )
    first = await bridge.launch(actor=actor, epic_id=epic_id, idempotency_key="first", request=request())
    dispatch = EpicDispatchService(bridge_factory)
    await dispatch.configure(
        actor=actor, epic_id=epic_id, execution_id=first.execution_id,
        idempotency_key="enable", request=EpicDispatchRequest(expected_dispatch_version=0, enabled=True),
    )
    token = uuid4()
    async with session_factory() as session, session.begin():
        setting = await session.get(EpicDispatchSetting, first.execution_id)
        setting.claim_item_id = second_id
        setting.claim_token = token
        setting.claim_expires_at = datetime.now(UTC) + timedelta(minutes=1)
        run = await session.get(Run, first.run_id)
        run.state = "FAILED"
    with pytest.raises(EpicLaunchConflict) as error:
        await bridge.launch(
            actor=actor, epic_id=epic_id, idempotency_key="claimed-sibling",
            request=request(second_id, execution_id=first.execution_id),
            dispatch_claim_token=token,
        )
    assert "predecessor_failed" in error.value.blocker_codes
    worker = EpicDispatchWorker(
        bridge_factory, bridge=bridge, eligibility=FailedAndReadyEligibility(first_id, second_id)
    )
    await worker.run_once()
    async with bridge_factory() as work:
        setting = await work.session.get(EpicDispatchSetting, first.execution_id)
        assert not setting.enabled and setting.claim_token is None
        assert (await work.session.get(EpicExecutionControl, first.execution_id)).state == "BLOCKED"
        await work.commit()
    await dispatch.configure(
        actor=actor, epic_id=epic_id, execution_id=first.execution_id,
        idempotency_key="re-enable", request=EpicDispatchRequest(expected_dispatch_version=1, enabled=True),
    )
    await asyncio.sleep(1.1)
    await worker.run_once()
    assert (await dispatch.get(epic_id, first.execution_id)).blocker_code == "execution_not_active"
    target = second_id if owner_sibling else first_id
    retry = await bridge.launch(
        actor=actor, epic_id=epic_id, idempotency_key="owner-retry",
        request=request(target, execution_id=first.execution_id, owner_override=True),
    )
    replay = await bridge.launch(
        actor=actor, epic_id=epic_id, idempotency_key="owner-retry",
        request=request(target, execution_id=first.execution_id, owner_override=True),
    )
    assert retry.attempt_id == replay.attempt_id
    assert retry.attempt_number == (1 if owner_sibling else 2)
    assert retry.override_note is None and "predecessor_failed" in retry.blocker_codes
    assert "execution_not_active" in retry.blocker_codes
    async with bridge_factory() as work:
        attempts = await work.epic_run_bridge.list_attempts(epic_id, execution_id=first.execution_id)
        assert [attempt.attempt_id for attempt in attempts] == [first.attempt_id, retry.attempt_id]
        assert (await work.runs.get(first.run_id)).state == "FAILED"
        for model in (Task, Run, RunCommand):
            assert await work.session.scalar(select(func.count()).select_from(model)) == 2
        await work.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_after_refusal", [False, True])
async def test_failure_committed_after_claim_stops_bridge_and_settles_control(
    session_factory, bridge_factory, retry_after_refusal
):
    bridge, _, actor, epic_id, first_id, second_id, request = await setup(
        session_factory, bridge_factory, independent=True
    )
    first = await bridge.launch(actor=actor, epic_id=epic_id, idempotency_key="first", request=request())
    async with session_factory() as session, session.begin():
        run = await session.get(Run, first.run_id)
        run.state = "COMPLETED"
        command = (await session.scalars(select(RunCommand).where(RunCommand.run_id == first.run_id))).one()
        command.status = "COMPLETED"
        command.completed_at = datetime.now(UTC)
        hold = await session.get(EpicChildBudgetHold, first.attempt_id)
        hold.effects_settled = True
    dispatch = EpicDispatchService(bridge_factory)
    await dispatch.configure(
        actor=actor, epic_id=epic_id, execution_id=first.execution_id,
        idempotency_key="enable", request=EpicDispatchRequest(expected_dispatch_version=0, enabled=True),
    )
    claimed = asyncio.Event()
    release = asyncio.Event()

    class WaitingBridge:
        retry = None

        async def launch(self, **kwargs):
            claimed.set()
            await release.wait()
            try:
                return await bridge.launch(**kwargs)
            except EpicLaunchConflict:
                if retry_after_refusal:
                    self.retry = await bridge.launch(
                        actor=actor, epic_id=epic_id, idempotency_key="owner-retry-after-refusal",
                        request=request(first_id, execution_id=first.execution_id, owner_override=True),
                    )
                raise

    waiting_bridge = WaitingBridge()
    worker = EpicDispatchWorker(
        bridge_factory, bridge=waiting_bridge,
        eligibility=FailedAndReadyEligibility(first_id, second_id)
    )
    pending = asyncio.create_task(worker.run_once())
    await asyncio.wait_for(claimed.wait(), timeout=10)
    try:
        async with session_factory() as session, session.begin():
            setting = await session.get(EpicDispatchSetting, first.execution_id)
            assert setting.claim_item_id == second_id and setting.claim_token is not None
            run = await session.get(Run, first.run_id)
            run.state = "FAILED"
    finally:
        release.set()
    assert await asyncio.wait_for(pending, timeout=10) == first.execution_id
    state = await dispatch.get(epic_id, first.execution_id)
    assert state.enabled is retry_after_refusal
    assert state.blocker_code == ("active_child" if retry_after_refusal else "predecessor_failed")
    async with bridge_factory() as work:
        control = await work.session.get(EpicExecutionControl, first.execution_id)
        assert (control.state, control.version, control.blocker_code) == (
            ("ACTIVE" if retry_after_refusal else "BLOCKED"),
            (1 if retry_after_refusal else 2),
            (None if retry_after_refusal else "predecessor_failed"),
        )
        assert len(await work.epic_run_bridge.list_attempts(epic_id, execution_id=first.execution_id)) == (
            2 if retry_after_refusal else 1
        )
        for model in (Task, Run, RunCommand):
            assert await work.session.scalar(select(func.count()).select_from(model)) == (
                2 if retry_after_refusal else 1
            )
        await work.commit()


@pytest.mark.asyncio
async def test_exhausted_budget_blocks_control_but_owner_can_admit_directly(
    session_factory, bridge_factory
):
    bridge, _, actor, epic_id, first_id, _, request = await setup(session_factory, bridge_factory)
    execution = await bridge.start(
        actor=actor, epic_id=epic_id, idempotency_key="execution", expected_epic_version=5
    )
    await EpicBudgetService(bridge_factory).edit(
        actor=actor, epic_id=epic_id, idempotency_key="zero-cap",
        request=EpicBudgetEdit(expected_version=0, ceiling=TaskBudget(max_provider_attempts=0)),
    )
    dispatch = EpicDispatchService(bridge_factory)
    await dispatch.configure(
        actor=actor, epic_id=epic_id, execution_id=execution.execution_id,
        idempotency_key="enable", request=EpicDispatchRequest(expected_dispatch_version=0, enabled=True),
    )
    worker = EpicDispatchWorker(bridge_factory, bridge=bridge, eligibility=ReadyEligibility(first_id))
    assert await worker.run_once() == execution.execution_id
    state = await dispatch.get(epic_id, execution.execution_id)
    assert state.enabled is False and state.blocker_code == "epic_budget_provider_attempts_exhausted"
    async with bridge_factory() as work:
        control = await work.session.get(EpicExecutionControl, execution.execution_id)
        assert (control.state, control.version, control.blocker_code) == (
            "BLOCKED", 2, "epic_budget_provider_attempts_exhausted"
        )
        assert await work.epic_run_bridge.list_attempts(epic_id, execution_id=execution.execution_id) == []
        for model in (Task, Run, RunCommand):
            assert await work.session.scalar(select(func.count()).select_from(model)) == 0
        await work.commit()
    assert await worker.run_once() is None
    owner = await bridge.launch(
        actor=actor, epic_id=epic_id, idempotency_key="owner-admit",
        request=request(first_id, execution_id=execution.execution_id, owner_override=True),
    )
    assert owner.override_note is None
    assert "epic_budget_provider_attempts_exhausted" in owner.blocker_codes
    assert "execution_not_active" in owner.blocker_codes


@pytest.mark.asyncio
@pytest.mark.parametrize("budget_change", ["raise", "unset", "unchanged"])
async def test_owner_reenable_recovers_only_actual_budget_block_and_rechecks_cap(
    session_factory, bridge_factory, budget_change
):
    bridge, _, actor, epic_id, first_id, _, _ = await setup(session_factory, bridge_factory)
    execution = await bridge.start(
        actor=actor, epic_id=epic_id, idempotency_key="execution", expected_epic_version=5
    )
    budget = EpicBudgetService(bridge_factory)
    await budget.edit(
        actor=actor, epic_id=epic_id, idempotency_key="zero-cap",
        request=EpicBudgetEdit(expected_version=0, ceiling=TaskBudget(max_provider_attempts=0)),
    )
    dispatch = EpicDispatchService(bridge_factory)
    await dispatch.configure(
        actor=actor, epic_id=epic_id, execution_id=execution.execution_id,
        idempotency_key="enable", request=EpicDispatchRequest(expected_dispatch_version=0, enabled=True),
    )
    worker = EpicDispatchWorker(bridge_factory, bridge=bridge, eligibility=ReadyEligibility(first_id))
    assert await worker.run_once() == execution.execution_id
    blocker = "epic_budget_provider_attempts_exhausted"
    async with bridge_factory() as work:
        control = await work.session.get(EpicExecutionControl, execution.execution_id)
        assert (control.state, control.version, control.blocker_code) == ("BLOCKED", 2, blocker)
        await work.commit()

    if budget_change != "unchanged":
        await budget.edit(
            actor=actor, epic_id=epic_id, idempotency_key="change-cap",
            request=EpicBudgetEdit(
                expected_version=1,
                ceiling=TaskBudget(max_provider_attempts=2 if budget_change == "raise" else 0),
                disabled_dimensions=("provider_attempts",) if budget_change == "unset" else (),
            ),
        )
    retry = EpicDispatchRequest(expected_dispatch_version=1, enabled=True)
    reopened = await dispatch.configure(
        actor=actor, epic_id=epic_id, execution_id=execution.execution_id,
        idempotency_key="retry", request=retry,
    )
    assert (reopened.version, reopened.enabled, reopened.blocker_code) == (2, True, blocker)
    assert await dispatch.get(epic_id, execution.execution_id) == reopened
    assert await dispatch.configure(
        actor=actor, epic_id=epic_id, execution_id=execution.execution_id,
        idempotency_key="retry", request=retry,
    ) == reopened
    async with bridge_factory() as work:
        control = await work.session.get(EpicExecutionControl, execution.execution_id)
        assert (control.state, control.version, control.blocker_code) == ("ACTIVE", 3, None)
        events = (await work.session.scalars(select(OperatorAuditEvent).where(
            OperatorAuditEvent.subject_id == epic_id,
            OperatorAuditEvent.event_type == "epic.dispatch_configured",
        ).order_by(OperatorAuditEvent.created_at))).all()
        assert len(events) == 2
        assert events[-1].actor_id == actor.actor_id
        assert events[-1].payload["execution_id"] == str(execution.execution_id)
        assert events[-1].payload["warnings"] == [blocker]
        assert events[-1].payload["execution_control_version"] == 3
        assert events[-1].payload["recovered_budget_blocker"] == blocker
        await work.commit()

    assert await worker.run_once() == execution.execution_id
    async with bridge_factory() as work:
        control = await work.session.get(EpicExecutionControl, execution.execution_id)
        if budget_change == "unchanged":
            assert (control.state, control.version, control.blocker_code) == ("BLOCKED", 4, blocker)
        else:
            assert (control.state, control.version, control.blocker_code) == ("ACTIVE", 3, None)
        expected_children = 0 if budget_change == "unchanged" else 1
        assert len(await work.epic_run_bridge.list_attempts(epic_id, execution_id=execution.execution_id)) == expected_children
        for model in (Task, Run, RunCommand):
            assert await work.session.scalar(select(func.count()).select_from(model)) == expected_children
        await work.commit()
    assert await dispatch.configure(
        actor=actor, epic_id=epic_id, execution_id=execution.execution_id,
        idempotency_key="retry", request=retry,
    ) == reopened


@pytest.mark.asyncio
async def test_budget_reenable_rejects_stale_invalid_and_conflicting_requests(
    session_factory, bridge_factory
):
    bridge, _, actor, epic_id, first_id, _, _ = await setup(session_factory, bridge_factory)
    execution = await bridge.start(
        actor=actor, epic_id=epic_id, idempotency_key="execution", expected_epic_version=5
    )
    await EpicBudgetService(bridge_factory).edit(
        actor=actor, epic_id=epic_id, idempotency_key="zero-cap",
        request=EpicBudgetEdit(expected_version=0, ceiling=TaskBudget(max_provider_attempts=0)),
    )
    dispatch = EpicDispatchService(bridge_factory)
    await dispatch.configure(
        actor=actor, epic_id=epic_id, execution_id=execution.execution_id,
        idempotency_key="enable", request=EpicDispatchRequest(expected_dispatch_version=0, enabled=True),
    )
    await EpicDispatchWorker(
        bridge_factory, bridge=bridge, eligibility=ReadyEligibility(first_id)
    ).run_once()
    with pytest.raises(EpicDispatchConflict, match="version is stale"):
        await dispatch.configure(
            actor=actor, epic_id=epic_id, execution_id=execution.execution_id,
            idempotency_key="stale",
            request=EpicDispatchRequest(expected_dispatch_version=0, enabled=True),
        )
    with pytest.raises(SubscriptionProfileNotFound, match="profile version not found"):
        await dispatch.configure(
            actor=actor, epic_id=epic_id, execution_id=execution.execution_id,
            idempotency_key="invalid-profile",
            request=EpicDispatchRequest(
                expected_dispatch_version=1, enabled=True,
                profile_id=uuid4(), profile_version=1,
            ),
        )
    with pytest.raises(MutationConflict):
        await dispatch.configure(
            actor=actor, epic_id=epic_id, execution_id=execution.execution_id,
            idempotency_key="enable",
            request=EpicDispatchRequest(expected_dispatch_version=1, enabled=True),
        )
    async with bridge_factory() as work:
        control = await work.session.get(EpicExecutionControl, execution.execution_id)
        assert (control.state, control.version, control.blocker_code) == (
            "BLOCKED", 2, "epic_budget_provider_attempts_exhausted"
        )
        setting = await work.session.get(EpicDispatchSetting, execution.execution_id)
        assert (setting.version, setting.enabled) == (1, False)
        events = (await work.session.scalars(select(OperatorAuditEvent).where(
            OperatorAuditEvent.subject_id == epic_id,
            OperatorAuditEvent.event_type == "epic.dispatch_configured",
        ))).all()
        assert len(events) == 1
        await work.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("control_state", "blocker", "reenable"),
    [
        ("BLOCKED", "epic_budget_provider_attempts_exhausted", False),
        ("BLOCKED", "predecessor_failed", True),
        ("BLOCKED", "uncontrolled_child", True),
        ("PAUSE_REQUESTED", "epic_budget_provider_attempts_exhausted", True),
        ("RESUME_REQUESTED", "epic_budget_provider_attempts_exhausted", True),
        ("CANCEL_REQUESTED", "epic_budget_provider_attempts_exhausted", True),
        ("PAUSED", "epic_budget_provider_attempts_exhausted", True),
        ("SUCCEEDED", "epic_budget_provider_attempts_exhausted", True),
        ("CANCELLED", "epic_budget_provider_attempts_exhausted", True),
    ],
)
async def test_reenable_does_not_recover_other_control_states_or_disable(
    session_factory, bridge_factory, control_state, blocker, reenable
):
    bridge, _, actor, epic_id, _, _, _ = await setup(session_factory, bridge_factory)
    execution = await bridge.start(
        actor=actor, epic_id=epic_id, idempotency_key="execution", expected_epic_version=5
    )
    dispatch = EpicDispatchService(bridge_factory)
    await dispatch.configure(
        actor=actor, epic_id=epic_id, execution_id=execution.execution_id,
        idempotency_key="enable", request=EpicDispatchRequest(expected_dispatch_version=0, enabled=True),
    )
    async with session_factory() as session, session.begin():
        control = await session.get(EpicExecutionControl, execution.execution_id)
        control.state = control_state
        control.version = 2
        control.blocker_code = blocker
    configured = await dispatch.configure(
        actor=actor, epic_id=epic_id, execution_id=execution.execution_id,
        idempotency_key="again",
        request=EpicDispatchRequest(expected_dispatch_version=1, enabled=reenable),
    )
    assert (configured.version, configured.enabled, configured.blocker_code) == (
        2, reenable, "execution_not_active"
    )
    async with bridge_factory() as work:
        control = await work.session.get(EpicExecutionControl, execution.execution_id)
        assert (control.state, control.version, control.blocker_code) == (control_state, 2, blocker)
        await work.commit()


@pytest.mark.asyncio
async def test_latest_active_retry_supersedes_old_failure_before_dispatch_poll(
    session_factory, bridge_factory
):
    bridge, _, actor, epic_id, first_id, second_id, request = await setup(
        session_factory, bridge_factory, independent=True
    )
    first = await bridge.launch(actor=actor, epic_id=epic_id, idempotency_key="first", request=request())
    async with session_factory() as session, session.begin():
        run = await session.get(Run, first.run_id)
        run.state = "FAILED"
    dispatch = EpicDispatchService(bridge_factory)
    await dispatch.configure(
        actor=actor, epic_id=epic_id, execution_id=first.execution_id,
        idempotency_key="enable", request=EpicDispatchRequest(expected_dispatch_version=0, enabled=True),
    )
    class OwnerRetryDuringReadiness(FailedAndReadyEligibility):
        retry = None

        async def readiness(self, *, epic_id, execution_id):
            stale = await super().readiness(epic_id=epic_id, execution_id=execution_id)
            self.retry = await bridge.launch(
                actor=actor, epic_id=epic_id, idempotency_key="retry",
                request=request(first_id, execution_id=first.execution_id, owner_override=True),
            )
            return stale

    eligibility = OwnerRetryDuringReadiness(first_id, second_id)
    worker = EpicDispatchWorker(bridge_factory, bridge=bridge, eligibility=eligibility)
    await worker.run_once()
    assert eligibility.retry.attempt_number == 2
    assert "predecessor_failed" in eligibility.retry.blocker_codes
    state = await dispatch.get(epic_id, first.execution_id)
    assert state.enabled and state.blocker_code == "child_effects_unsettled"
    async with bridge_factory() as work:
        control = await work.session.get(EpicExecutionControl, first.execution_id)
        assert control.state == "ACTIVE" and control.version == 1
        assert len(await work.epic_run_bridge.list_attempts(epic_id, execution_id=first.execution_id)) == 2
        await work.commit()


@pytest.mark.asyncio
async def test_deferred_failure_does_not_block_required_dispatch_control(session_factory, bridge_factory):
    bridge, _, actor, epic_id, first_id, second_id, request = await setup(
        session_factory, bridge_factory, deferred=True
    )
    deferred = await bridge.launch(
        actor=actor, epic_id=epic_id, idempotency_key="deferred-owner",
        request=request(second_id, owner_override=True),
    )
    async with session_factory() as session, session.begin():
        run = await session.get(Run, deferred.run_id)
        run.state = "FAILED"
    dispatch = EpicDispatchService(bridge_factory)
    await dispatch.configure(
        actor=actor, epic_id=epic_id, execution_id=deferred.execution_id,
        idempotency_key="enable", request=EpicDispatchRequest(expected_dispatch_version=0, enabled=True),
    )
    worker = EpicDispatchWorker(bridge_factory, bridge=bridge, eligibility=ReadyEligibility(first_id))
    await worker.run_once()
    async with bridge_factory() as work:
        control = await work.session.get(EpicExecutionControl, deferred.execution_id)
        assert control.state == "ACTIVE" and control.version == 1
        assert len(await work.epic_run_bridge.list_attempts(epic_id, execution_id=deferred.execution_id)) == 1
        await work.commit()
    assert (await dispatch.get(epic_id, deferred.execution_id)).blocker_code != "predecessor_failed"


@pytest.mark.asyncio
async def test_independent_ready_sibling_waits_for_active_epic_child(
    session_factory, bridge_factory
):
    bridge, _, actor, epic_id, first_id, second_id, _ = await setup(
        session_factory, bridge_factory, independent=True
    )
    execution = await bridge.start(
        actor=actor, epic_id=epic_id, idempotency_key="execution", expected_epic_version=5
    )
    dispatch = EpicDispatchService(bridge_factory)
    await dispatch.configure(
        actor=actor, epic_id=epic_id, execution_id=execution.execution_id,
        idempotency_key="enable", request=EpicDispatchRequest(expected_dispatch_version=0, enabled=True),
    )
    eligibility = TwoReadyEligibility(first_id, second_id)
    worker = EpicDispatchWorker(bridge_factory, bridge=bridge, eligibility=eligibility)
    await worker.run_once()
    eligibility.first_active = True
    await asyncio.sleep(1.1)
    await worker.run_once()
    state = await dispatch.get(epic_id, execution.execution_id)
    assert state.enabled and state.blocker_code == "active_child"
    async with bridge_factory() as work:
        attempts = await work.epic_run_bridge.list_attempts(epic_id, execution_id=execution.execution_id)
        assert len(attempts) == 1 and attempts[0].item_id == first_id
        assert await work.session.scalar(select(func.count()).select_from(Task)) == 1
        assert await work.session.scalar(select(func.count()).select_from(RunCommand)) == 1
        await work.commit()
    async with session_factory() as session, session.begin():
        run = await session.get(Run, attempts[0].run_id)
        run.state = "COMPLETED"
        command = (await session.scalars(
            select(RunCommand).where(RunCommand.run_id == run.id)
        )).one()
        command.status = "COMPLETED"
        command.completed_at = datetime.now(UTC)
        hold = await session.get(EpicChildBudgetHold, attempts[0].attempt_id)
        hold.effects_settled = True
        execution_id = uuid4()
        session.add(AgentExecution(
            id=execution_id, run_id=run.id, role="planner", instruction_version="v1",
            provider="fixture", model="model", status="SUCCEEDED",
        ))
        session.add(ModelUsage(
            run_id=run.id, agent_execution_id=execution_id,
            provider="fixture", model="model", prompt_version="v1",
            input_tokens=1, output_tokens=1, duration_ms=1, tool_call_count=0,
            pricing_version="v1", estimated_cost_minor=1, currency="USD",
        ))
    await asyncio.sleep(1.1)
    await worker.run_once()
    async with bridge_factory() as work:
        attempts = await work.epic_run_bridge.list_attempts(epic_id, execution_id=execution.execution_id)
        assert len(attempts) == 2 and attempts[1].item_id == second_id
        await work.commit()


@pytest.mark.asyncio
async def test_readiness_failure_is_visible_and_does_not_hot_loop(session_factory, bridge_factory):
    bridge, _, actor, epic_id, first_id, _, _ = await setup(session_factory, bridge_factory)
    execution = await bridge.start(
        actor=actor, epic_id=epic_id, idempotency_key="execution", expected_epic_version=5
    )
    dispatch = EpicDispatchService(bridge_factory)
    await dispatch.configure(
        actor=actor, epic_id=epic_id, execution_id=execution.execution_id,
        idempotency_key="enable", request=EpicDispatchRequest(expected_dispatch_version=0, enabled=True),
    )
    worker = EpicDispatchWorker(bridge_factory, bridge=bridge, eligibility=UnavailableEligibility(first_id))
    assert await worker.run_once() == execution.execution_id
    assert (await dispatch.get(epic_id, execution.execution_id)).blocker_code == "readiness_unavailable"
    assert await worker.run_once() is None


@pytest.mark.asyncio
async def test_owner_enable_dispatches_one_child_and_pause_blocks_progress(session_factory, bridge_factory):
    bridge, _, actor, epic_id, first_id, _, _ = await setup(session_factory, bridge_factory)
    execution = await bridge.start(
        actor=actor, epic_id=epic_id, idempotency_key="execution", expected_epic_version=5
    )
    dispatch = EpicDispatchService(bridge_factory)
    eligibility = ReadyEligibility(first_id)
    worker = EpicDispatchWorker(bridge_factory, bridge=bridge, eligibility=eligibility)
    assert await worker.run_once() is None
    configured = await dispatch.configure(
        actor=actor, epic_id=epic_id, execution_id=execution.execution_id,
        idempotency_key="enable", request=EpicDispatchRequest(expected_dispatch_version=0, enabled=True),
    )
    assert configured.version == 1 and configured.enabled
    assert (await dispatch.configure(
        actor=actor, epic_id=epic_id, execution_id=execution.execution_id,
        idempotency_key="enable", request=EpicDispatchRequest(expected_dispatch_version=0, enabled=True),
    )).version == 1
    await asyncio.gather(worker.run_once(), worker.run_once())
    async with bridge_factory() as work:
        attempts = await work.epic_run_bridge.list_attempts(epic_id, execution_id=execution.execution_id)
        assert len(attempts) == 1
        assert attempts[0].item_id == first_id
        control = await work.session.get(EpicExecutionControl, execution.execution_id)
        control.state = "PAUSED"
        await work.commit()
    await asyncio.sleep(1.1)
    await worker.run_once()
    assert (await dispatch.get(epic_id, execution.execution_id)).blocker_code == "execution_not_active"


@pytest.mark.asyncio
async def test_disable_before_poll_and_restart_keep_epoch_idle(session_factory, bridge_factory):
    bridge, _, actor, epic_id, first_id, _, _ = await setup(session_factory, bridge_factory)
    execution = await bridge.start(
        actor=actor, epic_id=epic_id, idempotency_key="execution", expected_epic_version=5
    )
    dispatch = EpicDispatchService(bridge_factory)
    await dispatch.configure(
        actor=actor, epic_id=epic_id, execution_id=execution.execution_id,
        idempotency_key="enable", request=EpicDispatchRequest(expected_dispatch_version=0, enabled=True),
    )
    await dispatch.configure(
        actor=actor, epic_id=epic_id, execution_id=execution.execution_id,
        idempotency_key="disable", request=EpicDispatchRequest(expected_dispatch_version=1, enabled=False),
    )
    assert await EpicDispatchWorker(
        bridge_factory, bridge=bridge, eligibility=ReadyEligibility(first_id)
    ).run_once() is None
    async with bridge_factory() as work:
        assert await work.epic_run_bridge.list_attempts(epic_id, execution_id=execution.execution_id) == []
        await work.commit()


@pytest.mark.asyncio
async def test_frozen_epoch_and_expired_claim_never_duplicate_child(session_factory, bridge_factory):
    bridge, _, actor, epic_id, first_id, _, _ = await setup(session_factory, bridge_factory)
    execution = await bridge.start(
        actor=actor, epic_id=epic_id, idempotency_key="execution", expected_epic_version=5
    )
    dispatch = EpicDispatchService(bridge_factory)
    await dispatch.configure(
        actor=actor, epic_id=epic_id, execution_id=execution.execution_id,
        idempotency_key="enable", request=EpicDispatchRequest(expected_dispatch_version=0, enabled=True),
    )
    async with bridge_factory() as work:
        epic = await work.session.get(Epic, epic_id)
        epic.version += 1  # A newer edit cannot retarget the frozen source.
        await work.commit()
    worker = EpicDispatchWorker(bridge_factory, bridge=bridge, eligibility=ReadyEligibility(first_id))
    await worker.run_once()
    async with bridge_factory() as work:
        attempts = await work.epic_run_bridge.list_attempts(epic_id, execution_id=execution.execution_id)
        assert len(attempts) == 1
        assert attempts[0].expected_epic_version == 6
        row = await work.session.get(EpicDispatchSetting, execution.execution_id)
        row.claim_item_id = first_id
        row.claim_token = uuid4()
        row.claim_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        row.checked_at = None
        await work.commit()
    restarted = EpicDispatchWorker(bridge_factory, bridge=bridge, eligibility=ReadyEligibility(first_id))
    await restarted.run_once()
    async with bridge_factory() as work:
        assert len(await work.epic_run_bridge.list_attempts(epic_id, execution_id=execution.execution_id)) == 1
        row = await work.session.get(EpicDispatchSetting, execution.execution_id)
        assert row.claim_item_id is None
        await work.commit()


@pytest.mark.asyncio
async def test_disable_revokes_claim_before_bridge_admission(session_factory, bridge_factory):
    bridge, _, actor, epic_id, first_id, _, request = await setup(session_factory, bridge_factory)
    execution = await bridge.start(
        actor=actor, epic_id=epic_id, idempotency_key="execution", expected_epic_version=5
    )
    dispatch = EpicDispatchService(bridge_factory)
    await dispatch.configure(
        actor=actor, epic_id=epic_id, execution_id=execution.execution_id,
        idempotency_key="enable", request=EpicDispatchRequest(expected_dispatch_version=0, enabled=True),
    )
    token = uuid4()
    async with bridge_factory() as work:
        row = await work.session.get(EpicDispatchSetting, execution.execution_id)
        row.claim_item_id = first_id
        row.claim_token = token
        row.claim_expires_at = datetime.now(UTC) + timedelta(minutes=1)
        await work.commit()
    await dispatch.configure(
        actor=actor, epic_id=epic_id, execution_id=execution.execution_id,
        idempotency_key="disable", request=EpicDispatchRequest(expected_dispatch_version=1, enabled=False),
    )
    with pytest.raises(EpicExecutionBindingConflict, match="dispatch claim is stale"):
        await bridge.launch(
            actor=actor, epic_id=epic_id, idempotency_key="stale-claim",
            request=request(first_id, execution_id=execution.execution_id),
            dispatch_claim_token=token,
        )
    async with bridge_factory() as work:
        assert await work.epic_run_bridge.list_attempts(epic_id, execution_id=execution.execution_id) == []
        await work.commit()


@pytest.mark.asyncio
async def test_manual_launch_racing_committed_dispatch_claim_has_one_child(
    session_factory, bridge_factory
):
    bridge, _, actor, epic_id, first_id, _, request = await setup(session_factory, bridge_factory)
    execution = await bridge.start(
        actor=actor, epic_id=epic_id, idempotency_key="execution", expected_epic_version=5
    )
    dispatch = EpicDispatchService(bridge_factory)
    await dispatch.configure(
        actor=actor, epic_id=epic_id, execution_id=execution.execution_id,
        idempotency_key="enable", request=EpicDispatchRequest(expected_dispatch_version=0, enabled=True),
    )
    claimed = asyncio.Event()
    release = asyncio.Event()

    class WaitingDispatchBridge:
        async def launch(self, **kwargs):
            claimed.set()
            await release.wait()
            return await bridge.launch(**kwargs)

    worker = EpicDispatchWorker(
        bridge_factory, bridge=WaitingDispatchBridge(), eligibility=ReadyEligibility(first_id)
    )
    automatic = asyncio.create_task(worker.run_once())
    await asyncio.wait_for(claimed.wait(), 10)
    try:
        async with bridge_factory() as work:
            setting = await work.session.get(EpicDispatchSetting, execution.execution_id)
            assert setting.claim_item_id == first_id and setting.claim_token is not None
            await work.commit()
        manual = await bridge.launch(
            actor=actor, epic_id=epic_id, idempotency_key="manual-race",
            request=request(first_id, execution_id=execution.execution_id),
        )
    finally:
        release.set()
    assert await asyncio.wait_for(automatic, 10) == execution.execution_id
    state = await dispatch.get(epic_id, execution.execution_id)
    assert state.enabled and state.blocker_code == "active_child"
    async with bridge_factory() as work:
        attempts = await work.epic_run_bridge.list_attempts(epic_id, execution_id=execution.execution_id)
        assert len(attempts) == 1 and attempts[0].attempt_id == manual.attempt_id
        for model in (Task, Run, RunCommand):
            assert await work.session.scalar(select(func.count()).select_from(model)) == 1
        await work.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("control_state", ["PAUSE_REQUESTED", "CANCEL_REQUESTED", "CANCELLED"])
async def test_control_after_claim_fences_dispatch_admission(
    session_factory, bridge_factory, control_state
):
    bridge, _, actor, epic_id, first_id, _, request = await setup(session_factory, bridge_factory)
    execution = await bridge.start(
        actor=actor, epic_id=epic_id, idempotency_key="execution", expected_epic_version=5
    )
    dispatch = EpicDispatchService(bridge_factory)
    await dispatch.configure(
        actor=actor, epic_id=epic_id, execution_id=execution.execution_id,
        idempotency_key="enable", request=EpicDispatchRequest(expected_dispatch_version=0, enabled=True),
    )
    token = uuid4()
    async with bridge_factory() as work:
        row = await work.session.get(EpicDispatchSetting, execution.execution_id)
        row.claim_item_id = first_id
        row.claim_token = token
        row.claim_expires_at = datetime.now(UTC) + timedelta(minutes=1)
        control = await work.session.get(EpicExecutionControl, execution.execution_id)
        control.state = control_state
        await work.commit()
    with pytest.raises(EpicLaunchConflict, match="execution_not_active"):
        await bridge.launch(
            actor=actor, epic_id=epic_id, idempotency_key=f"fenced-{control_state}",
            request=request(first_id, execution_id=execution.execution_id),
            dispatch_claim_token=token,
        )
    async with bridge_factory() as work:
        assert await work.epic_run_bridge.list_attempts(epic_id, execution_id=execution.execution_id) == []
        await work.commit()


@pytest.mark.asyncio
async def test_expired_dispatch_token_cannot_admit_late_result(session_factory, bridge_factory):
    bridge, _, actor, epic_id, first_id, _, request = await setup(session_factory, bridge_factory)
    execution = await bridge.start(
        actor=actor, epic_id=epic_id, idempotency_key="execution", expected_epic_version=5
    )
    dispatch = EpicDispatchService(bridge_factory)
    await dispatch.configure(
        actor=actor, epic_id=epic_id, execution_id=execution.execution_id,
        idempotency_key="enable", request=EpicDispatchRequest(expected_dispatch_version=0, enabled=True),
    )
    old_token, new_token = uuid4(), uuid4()
    async with bridge_factory() as work:
        row = await work.session.get(EpicDispatchSetting, execution.execution_id)
        row.claim_item_id = first_id
        row.claim_token = new_token
        row.claim_expires_at = datetime.now(UTC) + timedelta(minutes=1)
        await work.commit()
    with pytest.raises(EpicExecutionBindingConflict, match="dispatch claim is stale"):
        await bridge.launch(
            actor=actor, epic_id=epic_id, idempotency_key="late-old-token",
            request=request(first_id, execution_id=execution.execution_id),
            dispatch_claim_token=old_token,
        )
    async with bridge_factory() as work:
        row = await work.session.get(EpicDispatchSetting, execution.execution_id)
        row.claim_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await work.commit()
    with pytest.raises(EpicExecutionBindingConflict, match="dispatch claim is stale"):
        await bridge.launch(
            actor=actor, epic_id=epic_id, idempotency_key="expired-new-token",
            request=request(first_id, execution_id=execution.execution_id),
            dispatch_claim_token=new_token,
        )
    async with bridge_factory() as work:
        assert await work.epic_run_bridge.list_attempts(epic_id, execution_id=execution.execution_id) == []
        await work.commit()


@pytest.mark.asyncio
async def test_admission_failure_stops_auto_retry_until_owner_reenables(session_factory, bridge_factory):
    bridge, _, actor, epic_id, first_id, _, _ = await setup(session_factory, bridge_factory)
    execution = await bridge.start(
        actor=actor, epic_id=epic_id, idempotency_key="execution", expected_epic_version=5
    )
    dispatch = EpicDispatchService(bridge_factory)
    await dispatch.configure(
        actor=actor, epic_id=epic_id, execution_id=execution.execution_id,
        idempotency_key="enable", request=EpicDispatchRequest(expected_dispatch_version=0, enabled=True),
    )

    class RefusingBridge:
        calls = 0

        async def launch(self, **kwargs):
            self.calls += 1
            raise EpicLaunchConflict(["epic_usage_unknown"], actual_epic_version=5)

    refusing = RefusingBridge()
    worker = EpicDispatchWorker(bridge_factory, bridge=refusing, eligibility=ReadyEligibility(first_id))
    await worker.run_once()
    state = await dispatch.get(epic_id, execution.execution_id)
    assert not state.enabled and state.blocker_code == "epic_usage_unknown"
    await worker.run_once()
    assert refusing.calls == 1
    async with bridge_factory() as work:
        assert await work.epic_run_bridge.list_attempts(epic_id, execution_id=execution.execution_id) == []
        await work.commit()
    resumed = await dispatch.configure(
        actor=actor, epic_id=epic_id, execution_id=execution.execution_id,
        idempotency_key="owner-retry", request=EpicDispatchRequest(expected_dispatch_version=1, enabled=True),
    )
    assert resumed.enabled and resumed.version == 2
    await EpicDispatchWorker(bridge_factory, bridge=bridge, eligibility=ReadyEligibility(first_id)).run_once()
    async with bridge_factory() as work:
        assert len(await work.epic_run_bridge.list_attempts(epic_id, execution_id=execution.execution_id)) == 1
        await work.commit()


@pytest.mark.asyncio
async def test_dispatch_bridge_transaction_rollback_then_owner_recovery(
    session_factory, bridge_factory, monkeypatch
):
    bridge, _, actor, epic_id, first_id, _, _ = await setup(session_factory, bridge_factory)
    execution = await bridge.start(
        actor=actor, epic_id=epic_id, idempotency_key="execution", expected_epic_version=5
    )
    dispatch = EpicDispatchService(bridge_factory)
    await dispatch.configure(
        actor=actor, epic_id=epic_id, execution_id=execution.execution_id,
        idempotency_key="enable", request=EpicDispatchRequest(expected_dispatch_version=0, enabled=True),
    )
    worker = EpicDispatchWorker(bridge_factory, bridge=bridge, eligibility=ReadyEligibility(first_id))

    async def fail(*args, **kwargs):
        raise RuntimeError("injected receipt failure")

    with monkeypatch.context() as patch:
        patch.setattr(PostgresMutationRepository, "complete", fail)
        await worker.run_once()
    assert (await dispatch.get(epic_id, execution.execution_id)).blocker_code == "dispatch_error"
    async with session_factory() as session:
        for model in (Task, Run, RunCommand):
            assert await session.scalar(select(func.count()).select_from(model)) == 0
    assert await worker.run_once() is None  # Bounded default: no second attempt.
    await dispatch.configure(
        actor=actor, epic_id=epic_id, execution_id=execution.execution_id,
        idempotency_key="owner-retry", request=EpicDispatchRequest(expected_dispatch_version=1, enabled=True),
    )
    await worker.run_once()
    async with bridge_factory() as work:
        assert len(await work.epic_run_bridge.list_attempts(epic_id, execution_id=execution.execution_id)) == 1
        for model in (Task, Run, RunCommand):
            assert await work.session.scalar(select(func.count()).select_from(model)) == 1
        await work.commit()


@pytest.mark.asyncio
async def test_verified_completion_waits_for_child_effect_and_hold_settlement(
    session_factory, bridge_factory
):
    bridge, _, actor, epic_id, first_id, _, request = await setup(session_factory, bridge_factory)
    child = await bridge.launch(actor=actor, epic_id=epic_id, idempotency_key="first", request=request())
    dispatch = EpicDispatchService(bridge_factory)
    await dispatch.configure(
        actor=actor, epic_id=epic_id, execution_id=child.execution_id,
        idempotency_key="enable", request=EpicDispatchRequest(expected_dispatch_version=0, enabled=True),
    )
    async with session_factory() as session, session.begin():
        run = await session.get(Run, child.run_id)
        run.state = "COMPLETED"
        command = (await session.scalars(select(RunCommand).where(RunCommand.run_id == child.run_id))).one()
        command.status = "COMPLETED"
        command.completed_at = datetime.now(UTC)
    worker = EpicDispatchWorker(bridge_factory, bridge=bridge, eligibility=VerifiedEligibility(first_id))
    await worker.run_once()
    async with bridge_factory() as work:
        assert (await work.session.get(EpicExecutionControl, child.execution_id)).state == "ACTIVE"
        hold = await work.session.get(EpicChildBudgetHold, child.attempt_id)
        hold.effects_settled = True
        await work.commit()
    await asyncio.sleep(1.1)
    # A separate lifecycle worker can still hold the terminal run row while
    # dispatch observes its already-settled effects. The terminal transition
    # must not wait on a needless run lock when there are no paused tasks.
    async with session_factory() as concurrent, concurrent.begin():
        await concurrent.execute(select(Run).where(Run.id == child.run_id).with_for_update())
        await asyncio.wait_for(worker.run_once(), timeout=0.75)
    async with bridge_factory() as work:
        assert (await work.session.get(EpicExecutionControl, child.execution_id)).state == "SUCCEEDED"
        await work.commit()
    late = await bridge.launch(
        actor=actor, epic_id=epic_id, idempotency_key="late-owner",
        request=request(first_id, execution_id=child.execution_id, owner_override=True),
    )
    assert "execution_not_active" in late.blocker_codes
    async with bridge_factory() as work:
        control = await work.session.get(EpicExecutionControl, child.execution_id)
        assert control.state == "BLOCKED" and control.blocker_code == "uncontrolled_child"
        await work.commit()


@pytest.mark.asyncio
async def test_three_bound_children_quiescent_terminal_projection(session_factory, bridge_factory):
    bridge, _, actor, epic_id, first_id, second_id, request = await setup(
        session_factory, bridge_factory, dependencies=True
    )

    async def settle(child):
        async with session_factory() as session, session.begin():
            run = await session.get(Run, child.run_id)
            run.state = "COMPLETED"
            command = (await session.scalars(
                select(RunCommand).where(RunCommand.run_id == child.run_id)
            )).one()
            command.status = "COMPLETED"
            command.completed_at = datetime.now(UTC)
            hold = await session.get(EpicChildBudgetHold, child.attempt_id)
            hold.effects_settled = True

    first = await bridge.launch(actor=actor, epic_id=epic_id, idempotency_key="first", request=request())
    await settle(first)
    second = await bridge.launch(
        actor=actor, epic_id=epic_id, idempotency_key="second",
        request=request(second_id, execution_id=first.execution_id, owner_override=True),
    )
    await settle(second)
    third = await bridge.launch(
        actor=actor, epic_id=epic_id, idempotency_key="third",
        request=request(first_id, execution_id=first.execution_id, owner_override=True),
    )
    await settle(third)
    dispatch = EpicDispatchService(bridge_factory)
    await dispatch.configure(
        actor=actor, epic_id=epic_id, execution_id=first.execution_id,
        idempotency_key="enable", request=EpicDispatchRequest(expected_dispatch_version=0, enabled=True),
    )
    worker = EpicDispatchWorker(
        bridge_factory, bridge=bridge, eligibility=TwoVerifiedEligibility(first_id, second_id)
    )
    assert await worker.run_once() == first.execution_id
    async with bridge_factory() as work:
        control = await work.session.get(EpicExecutionControl, first.execution_id)
        assert control.state == "SUCCEEDED"
        assert not (await dispatch.get(epic_id, first.execution_id)).enabled
        await work.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("priced_cost", "effects_settled", "record_usage", "other_currency"),
    [
        (3, True, True, False), (None, True, True, False),
        (3, False, True, False), (3, True, False, False),
        (3, True, True, True),
    ],
)
async def test_ordinary_priced_usage_counts_after_child_settlement(
    session_factory, bridge_factory, priced_cost, effects_settled, record_usage, other_currency
):
    bridge, _, actor, epic_id, _, _, request = await setup(session_factory, bridge_factory)
    child = await bridge.launch(actor=actor, epic_id=epic_id, idempotency_key="child", request=request())
    execution_id = uuid4()
    async with session_factory() as session, session.begin():
        run = await session.get(Run, child.run_id)
        run.state = "COMPLETED"
        hold = await session.get(EpicChildBudgetHold, child.attempt_id)
        hold.effects_settled = effects_settled
        session.add(AgentExecution(
            id=execution_id, run_id=child.run_id, role="planner", instruction_version="v1",
            provider="fixture", model="model", status="SUCCEEDED",
        ))
        if record_usage:
            session.add(ModelUsage(
                run_id=child.run_id, agent_execution_id=execution_id,
                provider="fixture", model="model", prompt_version="v1",
                input_tokens=10, output_tokens=5, duration_ms=100,
                tool_call_count=2, pricing_version="v1", estimated_cost_minor=priced_cost,
                currency="USD", unknown_price_reason="unpriced" if priced_cost is None else None,
            ))
        if other_currency:
            session.add(ModelUsage(
                run_id=child.run_id, agent_execution_id=execution_id,
                provider="fixture", model="model", prompt_version="v1",
                input_tokens=1, output_tokens=1, duration_ms=1,
                pricing_version="v1", estimated_cost_minor=2, currency="GBP",
            ))
    totals = await EpicBudgetService(bridge_factory).get(epic_id)
    assert totals.unknown is (priced_cost is None or not record_usage or other_currency)
    assert ("child_usage_unproved" in totals.warnings) is not record_usage
    assert totals.known["provider_attempts"] == 1
    assert totals.known["input_tokens"] == ((10 if record_usage else 0) + int(other_currency))
    expected_cost = (priced_cost or 0) if record_usage and not other_currency else 0
    assert totals.known["estimated_api_cost_minor"] == expected_cost
    assert totals.currency == ("USD" if priced_cost is not None and record_usage and not other_currency else None)
    if other_currency:
        assert "epic_cost_currency_conflict" in totals.warnings
        async with session_factory() as session:
            assert await session.scalar(select(func.count()).select_from(ModelUsage)) == 2
    if not effects_settled:
        assert totals.held["duration_ms"] > 0
