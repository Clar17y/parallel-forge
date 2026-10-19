"""Completed failed validation must settle while slow observations renew its lease."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, uuid4, uuid5

import forge.application.services.validation as validation_module
import pytest
from forge.application.handlers.run_controls import CancelRunHandler, PauseRunHandler
from forge.application.ports.commands import (
    CommandLane,
    CommandLeaseLost,
    CommandRecoveryRequired,
    CommandSuspended,
)
from forge.application.ports.executions import ExecutionStatus
from forge.application.ports.subscription_decisions import SubscriptionDecisionError
from forge.application.ports.worktrees import GitSnapshotFile
from forge.application.services.control_settlement import controlled_stop
from forge.application.services.worker import Worker
from forge.domain.command import CommandStatus
from forge.domain.run import RunState
from forge.persistence.models import EvidenceSet, Run, RunCommand
from forge.persistence.models.scheduling import SubscriptionScheduledTask
from forge.persistence.models.subscription_results import SubscriptionRepairDebit
from forge.persistence.repositories.commands import PostgresCommandRepository
from sqlalchemy import func, select, text
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_publication import publication_case
from test_subscription_validation_repair import failed_validation_case


@pytest.mark.integration
async def test_completed_failed_validation_renews_through_slow_candidate_observations(
    session_factory, tmp_path, monkeypatch
):
    factory, proposal, _, command, controller, runner, _ = await failed_validation_case(
        session_factory, tmp_path
    )
    # Reproduce the durable state seen after all checks were recorded but before
    # a publication/repair decision could commit. Reclaim must use those receipts.
    async with factory() as work:
        evidence = await controller._validation.execute(command, work)
        row = await work.session.get(RunCommand, command.id)
        row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await work.commit()

    capture = controller._candidate

    async def slow_candidate(*args, **kwargs):
        await asyncio.sleep(2)
        return await capture(*args, **kwargs)

    monkeypatch.setattr(controller, "_candidate", slow_candidate)
    commands = PostgresCommandRepository(session_factory)
    worker = Worker(
        commands,
        session_factory,
        handlers={"validate": controller.validate},
        worker_id="failure-recovery",
        lease_seconds=3,
    )
    assert await worker.tick() is True
    assert await worker.tick() is None
    settled = await commands.get(command.id)
    assert settled.status is CommandStatus.COMPLETED
    assert settled.attempt == command.attempt + 1
    async with factory() as work:
        run = await work.runs.get(command.run_id)
        assert run.state is RunState.REMEDIATING
        assert run.version == command.expected_run_version + 1
        assert run.local_remediation_count == 1
        assert await work.session.get(SubscriptionRepairDebit, proposal.attempt_id) is not None
        rejections = [
            event for event in await work.events.list_after(command.run_id, 0)
            if event.event_type == "run.subscription_validation_rejected"
        ]
        assert len(rejections) == 1
        assert rejections[0].payload["validation_digest"] == evidence.manifest_digest
    assert runner.calls == runner.runner.calls == 1


@pytest.mark.integration
async def test_reclaimer_holding_command_lock_cannot_deadlock_failure_settlement(
    session_factory, tmp_path, monkeypatch
):
    factory, _, _, command, controller, _, _ = await failed_validation_case(
        session_factory, tmp_path
    )
    commands = PostgresCommandRepository(session_factory)
    capture = controller._candidate
    observations = 0
    reclaimer = None

    async def reclaim_during_observation(*args, **kwargs):
        nonlocal observations, reclaimer
        observed = await capture(*args, **kwargs)
        observations += 1
        if observations == 2:
            await _expire_delivery(factory, command)
            reclaimer = asyncio.create_task(
                commands.claim_next(worker_id="reclaimed-validation", lease_seconds=60)
            )
            # The real claim holds the command row and waits for this handler's
            # source/run lock. The final fence must yield without a lock cycle.
            async with asyncio.timeout(2):
                while True:
                    async with session_factory() as observer:
                        blocked = await observer.scalar(text(
                            "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                            "WHERE datname = current_database() AND wait_event_type = 'Lock')"
                        ))
                    if blocked:
                        break
                    await asyncio.sleep(0.01)
        return observed

    monkeypatch.setattr(controller, "_candidate", reclaim_during_observation)
    try:
        with pytest.raises(CommandRecoveryRequired, match="settlement fence is busy"):
            async with factory() as work:
                await controller.validate(command, work)
    finally:
        if reclaimer is not None:
            replacement = await asyncio.wait_for(reclaimer, timeout=5)
            assert replacement is not None and replacement.id == command.id
            assert replacement.lease_owner == "reclaimed-validation"
    async with factory() as work:
        assert (await work.runs.get(command.run_id)).state is RunState.VALIDATING


@pytest.mark.integration
@pytest.mark.parametrize("failed_check", [False, True])
async def test_candidate_drift_during_decision_does_not_commit_a_result(
    session_factory, tmp_path, monkeypatch, failed_check
):
    case = failed_validation_case if failed_check else publication_case
    factory, proposal, _, command, controller, _, git = await case(session_factory, tmp_path)
    capture, snapshot = controller._candidate, git.working_tree_snapshot

    def drift(*args, **kwargs):
        return replace(
            snapshot(*args, **kwargs),
            files=(GitSnapshotFile(
                path="changed.py", mode="100644", content_digest="e" * 64, byte_count=1,
            ),),
            changed_paths=("changed.py",),
        )

    async def observe_then_drift(*args, **kwargs):
        observed = await capture(*args, **kwargs)
        monkeypatch.setattr(git, "working_tree_snapshot", drift)
        return observed

    monkeypatch.setattr(controller, "_candidate", observe_then_drift)
    async with factory() as work:
        with pytest.raises(CommandRecoveryRequired, match="candidate differs"):
            await controller.validate(command, work)
    async with factory() as work:
        run = await work.runs.get(command.run_id)
        assert run.state is RunState.VALIDATING and run.pending_gate is None
        assert run.local_remediation_count == 0
        assert await work.session.get(SubscriptionRepairDebit, proposal.attempt_id) is None


@pytest.mark.integration
@pytest.mark.parametrize("stage", ["validation", "publication", "replay"])
@pytest.mark.parametrize("failed_check", [False, True])
async def test_busy_renewal_fence_rolls_back_and_replays_without_claiming_owner_loss(
    session_factory, tmp_path, monkeypatch, stage, failed_check
):
    case = failed_validation_case if failed_check else publication_case
    factory, proposal, _, command, controller, runner, _ = await case(session_factory, tmp_path)
    prior = None
    if stage == "replay":
        async with factory() as work:
            prior = await controller.validate(command, work)

    commands = PostgresCommandRepository(session_factory)
    locked = commands._locked
    held, release = asyncio.Event(), asyncio.Event()
    renewal = None

    async def hold_renewal(*args, **kwargs):
        row = await locked(*args, **kwargs)
        held.set()
        await release.wait()
        return row

    monkeypatch.setattr(commands, "_locked", hold_renewal)
    calls = 0

    def observe(operation, at):
        async def observed(*args, **kwargs):
            nonlocal calls, renewal
            result = await operation(*args, **kwargs)
            calls += 1
            if calls == at:
                renewal = asyncio.create_task(commands.renew(
                    command.id, worker_id=command.lease_owner, lease_seconds=60,
                ))
                await asyncio.wait_for(held.wait(), timeout=2)
            return result

        return observed

    if stage == "validation":
        monkeypatch.setattr(
            validation_module, "_candidate_matches",
            observe(validation_module._candidate_matches, 5),
        )
    elif stage == "publication":
        monkeypatch.setattr(controller, "_candidate", observe(controller._candidate, 2))
    else:
        monkeypatch.setattr(
            controller._publication, "verify_validation",
            observe(controller._publication.verify_validation, 1),
        )

    try:
        with pytest.raises(CommandRecoveryRequired, match="settlement fence is busy"):
            async with factory() as work:
                try:
                    await controller.validate(command, work)
                except CommandRecoveryRequired:
                    # PostgreSQL NOWAIT produced 55P03. The savepoint rolls back
                    # that failed statement; the outer decision still rolls back.
                    assert await work.session.scalar(text("SELECT 1")) == 1
                    raise
    finally:
        release.set()
        if renewal is not None:
            renewed = await asyncio.wait_for(renewal, timeout=2)
            assert renewed.lease_owner == command.lease_owner
            assert renewed.attempt == command.attempt
            assert renewed.status is CommandStatus.LEASED
    assert renewal is not None
    async with factory() as work:
        run = await work.runs.get(command.run_id)
        assert run.state is (RunState.VALIDATING if prior is None else prior.state)
        assert run.local_remediation_count == int(stage == "replay" and failed_check)
        debit = await work.session.get(SubscriptionRepairDebit, proposal.attempt_id)
        assert (debit is not None) is (stage == "replay" and failed_check)
        validation_sets = await work.session.scalar(
            select(func.count()).select_from(EvidenceSet).where(
                EvidenceSet.run_id == command.run_id, EvidenceSet.kind == "validation",
            )
        )
        assert validation_sets == int(stage != "validation")
        assert await work.session.scalar(
            select(func.count()).select_from(EvidenceSet).where(
                EvidenceSet.run_id == command.run_id, EvidenceSet.kind == "acceptance",
            )
        ) == int(stage == "replay" and not failed_check)
    async with factory() as work:
        recovered = await controller.validate(command, work)
    expected = RunState.REMEDIATING if failed_check else RunState.AWAITING_PR_APPROVAL
    assert recovered.state is expected
    if prior is not None:
        assert recovered == prior
    # The holder is only used once; ordinary acknowledgement retains its default
    # blocking semantics and confirms the still-current delivery after recovery.
    monkeypatch.setattr(commands, "_locked", locked)
    assert (await commands.complete(command.id, worker_id=command.lease_owner)).status is (
        CommandStatus.COMPLETED
    )
    async with factory() as work:
        decisions = [
            event for event in await work.events.list_after(command.run_id, 0)
            if event.event_type in {
                "run.subscription_validation_rejected", "run.subscription_acceptance_decided"
            }
        ]
        assert len(decisions) == 1
    assert runner.calls == runner.runner.calls == 1


async def _expire_delivery(factory, command):
    async with factory() as work:
        row = await work.session.get(RunCommand, command.id)
        row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await work.commit()


@pytest.mark.integration
@pytest.mark.parametrize("outcome", ["passed", "repair", "task_exhausted", "run_exhausted"])
async def test_validation_and_decision_replay_allow_renewal_during_all_observations(
    session_factory, tmp_path, monkeypatch, outcome
):
    case = publication_case if outcome == "passed" else failed_validation_case
    factory, proposal, dispatch, command, controller, runner, _ = await case(
        session_factory, tmp_path
    )
    if outcome.endswith("exhausted"):
        async with factory() as work:
            if outcome == "task_exhausted":
                scheduled = await work.session.get(
                    SubscriptionScheduledTask, proposal.decision.task_id
                )
                scheduled.repairs = scheduled.max_repairs
            else:
                run = await work.session.get(Run, command.run_id)
                run.local_remediation_count = proposal.policy.local_remediation_limit
            await work.commit()
    commands = PostgresCommandRepository(session_factory)
    await _expire_delivery(factory, command)
    observations = {"validation": 0, "publication": 0, "read": 0, "write": 0}

    def observe(stage, operation):
        async def observed(*args, **kwargs):
            # A separate PostgreSQL connection must be able to renew while the
            # handler is in Git/artifact IO. This catches blocking row locks
            # without relying on a large checkout or wall-clock lease expiry.
            await asyncio.wait_for(
                commands.renew(command.id, worker_id="observation-recovery", lease_seconds=30),
                timeout=2,
            )
            observations[stage] += 1
            return await operation(*args, **kwargs)

        return observed

    monkeypatch.setattr(
        validation_module,
        "_candidate_matches",
        observe("validation", validation_module._candidate_matches),
    )
    monkeypatch.setattr(controller, "_candidate", observe("publication", controller._candidate))
    monkeypatch.setattr(dispatch._store, "open_bytes", observe("read", dispatch._store.open_bytes))
    monkeypatch.setattr(dispatch._store, "put_bytes", observe("write", dispatch._store.put_bytes))
    complete = commands.complete
    settlement_calls = 0

    async def crash_before_first_ack(*args, **kwargs):
        nonlocal settlement_calls
        settlement_calls += 1
        if settlement_calls == 1:
            raise RuntimeError("worker stopped before command acknowledgement")
        return await complete(*args, **kwargs)

    monkeypatch.setattr(commands, "complete", crash_before_first_ack)
    worker = Worker(
        commands,
        session_factory,
        handlers={"validate": controller.validate},
        worker_id="observation-recovery",
        lease_seconds=30,
    )
    with pytest.raises(RuntimeError, match="before command acknowledgement"):
        await worker.tick()
    before_replay = dict(observations)
    assert all(count > 0 for count in before_replay.values())
    await _expire_delivery(factory, command)
    assert await worker.tick() is True
    assert await worker.tick() is None
    assert observations["validation"] == before_replay["validation"]
    assert observations["publication"] == before_replay["publication"]
    assert observations["read"] > before_replay["read"]
    assert (await commands.get(command.id)).status is CommandStatus.COMPLETED
    assert (await commands.get(command.id)).attempt == command.attempt + 2
    async with factory() as work:
        run = await work.runs.get(command.run_id)
        expected_state = {
            "passed": RunState.AWAITING_PR_APPROVAL,
            "repair": RunState.REMEDIATING,
            "task_exhausted": RunState.AWAITING_HUMAN_INTERVENTION,
            "run_exhausted": RunState.AWAITING_HUMAN_INTERVENTION,
        }[outcome]
        assert run.state is expected_state
        assert run.version == command.expected_run_version + 1
        expected_count = {
            "passed": 0,
            "repair": 1,
            "task_exhausted": 0,
            "run_exhausted": proposal.policy.local_remediation_limit,
        }[outcome]
        assert run.local_remediation_count == expected_count
        debit = await work.session.get(SubscriptionRepairDebit, proposal.attempt_id)
        assert (debit is not None) is (outcome == "repair")
        decisions = [
            event for event in await work.events.list_after(command.run_id, 0)
            if event.event_type in {
                "run.subscription_validation_rejected", "run.subscription_acceptance_decided"
            }
        ]
        assert len(decisions) == 1
    assert runner.calls == runner.runner.calls == 1


@pytest.mark.integration
@pytest.mark.parametrize("failed_check", [False, True])
@pytest.mark.parametrize("lease_change", ["expired", "reclaimed"])
async def test_final_observation_must_reacquire_exact_live_delivery(
    session_factory, tmp_path, monkeypatch, failed_check, lease_change
):
    case = failed_validation_case if failed_check else publication_case
    factory, proposal, _, command, controller, runner, _ = await case(session_factory, tmp_path)
    capture = controller._candidate
    observations = 0

    async def lose_lease(*args, **kwargs):
        nonlocal observations
        observed = await capture(*args, **kwargs)
        observations += 1
        if observations == 2:
            async with factory() as changed:
                row = await changed.session.get(RunCommand, command.id)
                if lease_change == "expired":
                    row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
                else:
                    row.lease_owner = "replacement-worker"
                    row.attempt_count += 1
                await asyncio.wait_for(changed.commit(), timeout=2)
        return observed

    monkeypatch.setattr(controller, "_candidate", lose_lease)
    async with factory() as work:
        with pytest.raises(CommandLeaseLost):
            await controller.validate(command, work)
    async with factory() as work:
        run = await work.runs.get(command.run_id)
        assert run.state is RunState.VALIDATING and run.pending_gate is None
        assert run.version == command.expected_run_version
        assert run.local_remediation_count == 0
        assert await work.session.get(SubscriptionRepairDebit, proposal.attempt_id) is None
        assert not [
            event for event in await work.events.list_after(command.run_id, 0)
            if event.event_type in {
                "run.subscription_validation_rejected", "run.subscription_acceptance_decided"
            }
        ]
    assert runner.calls == runner.runner.calls == 1


@pytest.mark.integration
@pytest.mark.parametrize("control", ["pause", "cancel"])
@pytest.mark.parametrize("applied", [False, True])
async def test_owner_control_at_validation_boundary_prevents_failure_decision(
    session_factory, tmp_path, monkeypatch, control, applied
):
    factory, proposal, _, command, controller, runner, _ = await failed_validation_case(
        session_factory, tmp_path
    )
    execute = controller._validation.execute
    commands = PostgresCommandRepository(session_factory)

    async def control_after_validation(*args, **kwargs):
        evidence = await execute(*args, **kwargs)
        # Validation's committed evidence is a real authority boundary where an
        # owner control can be admitted before the final decision takes run locks.
        async with factory() as changed:
            await changed.commands.enqueue(
                run_id=command.run_id,
                command_type=control,
                idempotency_key=f"{command.run_id}:{control}:during-validation",
                payload={},
                expected_run_version=command.expected_run_version,
                actor_id=uuid4(),
            )
            await changed.commit()
        if applied:
            stopping = await commands.claim_next(
                worker_id="owner-control", lease_seconds=60, lane=CommandLane.CONTROL
            )
            assert stopping is not None and stopping.command_type == control
            handler = PauseRunHandler() if control == "pause" else CancelRunHandler()
            async with factory() as changed:
                await handler(stopping, changed)
            await commands.complete(stopping.id, worker_id=stopping.lease_owner)
        return evidence

    monkeypatch.setattr(controller._validation, "execute", control_after_validation)
    async with factory() as work:
        with pytest.raises((CommandRecoveryRequired, SubscriptionDecisionError)):
            await controller.validate(command, work)
    async with factory() as work:
        run = await work.runs.get(command.run_id)
        expected = RunState.PAUSED if control == "pause" else RunState.CANCELLED
        assert run.state is (expected if applied else RunState.VALIDATING)
        assert run.local_remediation_count == 0 and run.pending_gate is None
        assert await work.session.get(SubscriptionRepairDebit, proposal.attempt_id) is None
        assert not [
            event for event in await work.events.list_after(command.run_id, 0)
            if event.event_type == "run.subscription_validation_rejected"
        ]
    assert runner.calls == runner.runner.calls == 1


@pytest.mark.integration
@pytest.mark.parametrize(
    ("stage", "control", "applied", "failed_check"),
    [
        pytest.param("before_check", None, False, False, id="before-check-passed"),
        pytest.param("before_check", None, False, True, id="before-check-failed"),
        pytest.param("after_check", "pause", True, False, id="applied-pause-passed"),
        pytest.param("after_check", "cancel", True, True, id="applied-cancel-failed"),
        pytest.param("after_check", "pause", False, True, id="pending-pause-failed"),
        pytest.param("after_check", "cancel", False, False, id="pending-cancel-passed"),
        pytest.param("recovery", None, False, False, id="receipt-recovery-passed"),
        pytest.param("recovery", None, False, True, id="receipt-recovery-failed"),
        pytest.param("publication", "pause", False, False, id="publication-pending-pause"),
        pytest.param("publication", "cancel", True, True, id="publication-applied-cancel"),
    ],
)
async def test_control_stop_yields_to_reclaimer_without_inventing_suspension(
    session_factory, tmp_path, monkeypatch, stage, control, applied, failed_check
):
    case = failed_validation_case if failed_check else publication_case
    factory, proposal, _, command, controller, runner, git = await case(
        session_factory, tmp_path
    )
    validation = controller._validation
    if stage == "recovery":
        async def stop_before_publication(*args, **kwargs):
            raise CommandRecoveryRequired("test crash before evidence publication")

        with monkeypatch.context() as preparation:
            preparation.setattr(validation, "publish", stop_before_publication)
            with pytest.raises(CommandRecoveryRequired, match="test crash"):
                async with factory() as work:
                    await validation.execute(command, work)
        assert runner.calls == runner.runner.calls == 1

    commands = PostgresCommandRepository(session_factory)
    async with factory() as work:
        admitted_run = await work.runs.get(command.run_id)
    load, snapshot = validation._approved_plans.load, git.working_tree_snapshot
    loads, reclaimer = 0, None
    target = {"before_check": 2, "after_check": 3, "recovery": 3, "publication": 4}[stage]

    def drift(*args, **kwargs):
        return replace(
            snapshot(*args, **kwargs),
            files=(GitSnapshotFile(
                path="changed.py", mode="100644", content_digest="e" * 64, byte_count=1,
            ),),
            changed_paths=("changed.py",),
        )

    async def observe_authority(*args, **kwargs):
        nonlocal loads, reclaimer
        current = await load(*args, **kwargs)
        loads += 1
        if loads == target:
            # Applied controls change the run. Pending controls invalidate the
            # acceptance source before Git; ordinary drift verifies stop proof.
            if not applied:
                monkeypatch.setattr(git, "working_tree_snapshot", drift)
            await _expire_delivery(factory, command)
            reclaimer = asyncio.create_task(commands.claim_next(
                worker_id="reclaimed-control-settlement", lease_seconds=60,
            ))
            async with asyncio.timeout(2):
                while True:
                    async with session_factory() as observer:
                        blocked = await observer.scalar(text(
                            "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                            "WHERE datname = current_database() AND wait_event_type = 'Lock')"
                        ))
                    if blocked:
                        break
                    await asyncio.sleep(0.01)
        return current

    monkeypatch.setattr(validation._approved_plans, "load", observe_authority)
    reason = (
        "acceptance authority differs"
        if control is not None and not applied
        else "settlement fence is busy"
    )
    try:
        with pytest.raises(CommandRecoveryRequired, match=reason):
            async with factory() as work:
                commit, commits = work.commit, 0

                async def control_at_commit_boundary():
                    nonlocal commits
                    await commit()
                    commits += 1
                    if control is not None and commits == target:
                        await _admit_test_control(factory, commands, command, control, applied)

                monkeypatch.setattr(work, "commit", control_at_commit_boundary)
                try:
                    await validation.execute(command, work)
                except CommandRecoveryRequired:
                    assert await work.session.scalar(text("SELECT 1")) == 1
                    raise
    finally:
        if reclaimer is not None:
            replacement = await asyncio.wait_for(reclaimer, timeout=5)
            if control is not None:
                assert replacement is None
            else:
                assert replacement is not None and replacement.id == command.id
                assert replacement.lease_owner == "reclaimed-control-settlement"
    assert loads == target and reclaimer is not None
    async with factory() as work:
        # An expired or replaced delivery cannot now authenticate a stop, even
        # when a genuine owner control was applied while its activity ran.
        assert not await controlled_stop(work, command, admitted_run)
        run = await work.runs.get(command.run_id)
        expected = RunState.PAUSED if control == "pause" else RunState.CANCELLED
        assert run.state is (expected if applied else RunState.VALIDATING)
        assert run.local_remediation_count == 0 and run.pending_gate is None
        assert await work.session.get(SubscriptionRepairDebit, proposal.attempt_id) is None
        step = await work.controller_steps.get(
            run.id, uuid5(NAMESPACE_URL, f"forge:validate:{command.id}")
        )
        assert step.status is ExecutionStatus.RUNNING
        assert await work.session.scalar(
            select(func.count()).select_from(EvidenceSet).where(
                EvidenceSet.run_id == run.id, EvidenceSet.kind.in_(("validation", "acceptance")),
            )
        ) == 0
        assert not [
            event for event in await work.events.list_after(command.run_id, 0)
            if event.event_type in {
                "delivery.suspended", "run.subscription_validation_rejected",
                "run.subscription_acceptance_decided",
            }
        ]
    assert runner.calls == runner.runner.calls == int(stage != "before_check")


async def _admit_test_control(factory, commands, command, control, applied):
    async with factory() as changed:
        await changed.commands.enqueue(
            run_id=command.run_id,
            command_type=control,
            idempotency_key=f"{command.run_id}:{control}:during-check",
            payload={},
            expected_run_version=command.expected_run_version,
            actor_id=uuid4(),
        )
        await changed.commit()
    if applied:
        stopping = await commands.claim_next(
            worker_id="owner-control", lease_seconds=60, lane=CommandLane.CONTROL,
        )
        assert stopping is not None and stopping.command_type == control
        handler = PauseRunHandler() if control == "pause" else CancelRunHandler()
        async with factory() as changed:
            await handler(stopping, changed)
        await commands.complete(stopping.id, worker_id=stopping.lease_owner)


@pytest.mark.integration
@pytest.mark.parametrize("control", ["pause", "cancel"])
@pytest.mark.parametrize("failed_check", [False, True])
async def test_live_delivery_settles_applied_control_after_current_check(
    session_factory, tmp_path, monkeypatch, control, failed_check
):
    case = failed_validation_case if failed_check else publication_case
    factory, proposal, _, command, controller, runner, _ = await case(session_factory, tmp_path)
    commands = PostgresCommandRepository(session_factory)
    with pytest.raises(CommandSuspended):
        async with factory() as work:
            commit, commits = work.commit, 0

            async def control_at_commit_boundary():
                nonlocal commits
                await commit()
                commits += 1
                if commits == 3:
                    await _admit_test_control(factory, commands, command, control, True)

            monkeypatch.setattr(work, "commit", control_at_commit_boundary)
            await controller._validation.execute(command, work)
    async with factory() as work:
        run = await work.runs.get(command.run_id)
        assert run.state is (RunState.PAUSED if control == "pause" else RunState.CANCELLED)
        assert run.local_remediation_count == 0
        assert await work.session.get(SubscriptionRepairDebit, proposal.attempt_id) is None
        step = await work.controller_steps.get(
            run.id, uuid5(NAMESPACE_URL, f"forge:validate:{command.id}")
        )
        assert step.status is ExecutionStatus.CANCELLED
        suspended = [
            event for event in await work.events.list_after(command.run_id, 0)
            if event.event_type == "delivery.suspended"
        ]
        assert len(suspended) == 1 and suspended[0].payload["command_id"] == str(command.id)
    assert runner.calls == runner.runner.calls == 1
