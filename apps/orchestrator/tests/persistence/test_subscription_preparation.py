"""Approved subscription preparation hands execution to the durable primary."""

from pathlib import Path

import pytest
from forge.application.ports.worktrees import ManagedWorktree
from forge.application.services.approved_plan import ApprovedPlanLoader
from forge.application.services.delivery_preparation import DeliveryPreparationService
from forge.application.services.subscription_plan_gate import SubscriptionPlanGateService
from forge.domain.resource import WorktreeIdentity
from forge.domain.run import RunState
from forge.persistence.models import Run, RunCommand
from forge.persistence.models.scheduling import SubscriptionScheduledTask
from forge.persistence.models.subscription import SubscriptionTask
from forge.persistence.repositories.commands import PostgresCommandRepository
from sqlalchemy import select
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_plan_gate import approve_proposal, proposal_case


async def preparation_case(session_factory, tmp_path, *, primary_budget=None, plan_scope=None, review_route=None):
    factory, store, evidence, _, validator = await proposal_case(
        session_factory, tmp_path, primary_budget=primary_budget, plan_scope=plan_scope, review_route=review_route
    )
    outcome = await SubscriptionPlanGateService(store, factory).request_settled(
        evidence.producer.attempt_id
    )
    await approve_proposal(factory, session_factory, evidence, validator, outcome)
    async with session_factory() as session:
        approval_command_id = await session.scalar(
            select(RunCommand.id).where(
                RunCommand.run_id == evidence.producer.run_id,
                RunCommand.command_type == "approve_plan",
            )
        )
    await PostgresCommandRepository(session_factory).complete(
        approval_command_id, worker_id="approval-worker"
    )
    command = await PostgresCommandRepository(session_factory).claim_next(
        worker_id="prepare-worker", lease_seconds=30
    )
    assert command is not None and command.command_type == "prepare_worktree"

    class Provisioner:
        calls = 0

        async def prepare(self, run_id, policy):
            self.calls += 1
            async with factory() as work:
                run = await work.runs.get(run_id)
                path = str(tmp_path / "prepared")
                row = await work.session.get(Run, run_id)
                row.worktree_path = path
                await work.commit()
                return ManagedWorktree(
                    identity=WorktreeIdentity.for_run(
                        run.project_id, run.id, run.branch_name, policy.database.enabled
                    ),
                    path=Path(path),
                    base_sha=run.base_sha,
                )

    provisioner = Provisioner()
    service = DeliveryPreparationService(ApprovedPlanLoader(store), provisioner)
    return factory, evidence, command, service, provisioner


@pytest.mark.integration
async def test_prepared_subscription_queues_primary_without_legacy_implementation(
    session_factory, tmp_path
):
    factory, evidence, command, service, provisioner = await preparation_case(
        session_factory, tmp_path
    )
    async with factory() as work:
        await service.execute(command, work)
    async with factory() as work:
        run = await work.runs.get(evidence.producer.run_id)
        task = await work.session.get(SubscriptionTask, evidence.producer.task_id)
        scheduled = await work.session.scalar(
            select(SubscriptionScheduledTask).where(SubscriptionScheduledTask.task_id == task.id)
        )
        assert run.state is RunState.IMPLEMENTING
        assert task.state == scheduled.state == "queued"
        assert (
            scheduled.worktree_id
            == WorktreeIdentity.for_run(
                run.project_id, run.id, run.branch_name, False
            ).worktree_name
        )
        assert await work.commands.get_by_idempotency_key(f"{run.id}:implement:1") is None
        await work.rollback()

    async with factory() as work:
        await service.execute(command, work)
    assert provisioner.calls == 1
    from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
    from test_subscription_usage import _reservation

    admission = await SubscriptionDecisionExecutor(factory).admit_next(
        "next-primary", _reservation()
    )
    assert admission is not None and admission.task.task_id == evidence.producer.task_id
    assert admission.attempt.attempt_number == 2
    assert admission.attempt.attempt_id != evidence.producer.attempt_id
    from datetime import timedelta

    async with factory() as work:
        renewed = await work.scheduler.renew(admission.lease, timedelta(seconds=60))
        assert (
            renewed.owner == admission.lease.owner
            and renewed.generation == admission.lease.generation
        )
        assert renewed.expires_at > admission.lease.expires_at
        await work.commit()


@pytest.mark.integration
async def test_subscription_preparation_rolls_back_queue_and_replays(
    session_factory, tmp_path, monkeypatch
):
    from forge.persistence.repositories.subscription_plan_gate import (
        PostgresSubscriptionPlanGateRepository,
    )

    factory, evidence, command, service, _ = await preparation_case(session_factory, tmp_path)
    original = PostgresSubscriptionPlanGateRepository.resume_prepared

    async def fail_after_queue(self, *args, **kwargs):
        await original(self, *args, **kwargs)
        raise RuntimeError("injected preparation publication failure")

    with monkeypatch.context() as patch:
        patch.setattr(PostgresSubscriptionPlanGateRepository, "resume_prepared", fail_after_queue)
        with pytest.raises(RuntimeError, match="publication failure"):
            async with factory() as work:
                await service.execute(command, work)
    async with factory() as work:
        assert (await work.runs.get(evidence.producer.run_id)).state is RunState.PREPARING_WORKTREE
        assert (
            await work.session.get(SubscriptionTask, evidence.producer.task_id)
        ).state == "blocked"
        assert not [
            event
            for event in await work.events.list_after(evidence.producer.run_id, 0)
            if event.event_type == "run.worktree_prepared"
        ]
        await work.rollback()
    async with factory() as work:
        await service.execute(command, work)
    async with factory() as work:
        assert (
            await work.session.get(SubscriptionTask, evidence.producer.task_id)
        ).state == "queued"
        await work.rollback()


@pytest.mark.integration
async def test_task_cancel_during_provision_prevents_primary_queue(
    session_factory, tmp_path, monkeypatch
):
    from forge.application.ports.subscription_plan_gate import SubscriptionPlanGateError

    factory, evidence, command, service, provisioner = await preparation_case(
        session_factory, tmp_path
    )
    original = provisioner.prepare

    async def stop_after_provision(*args, **kwargs):
        resource = await original(*args, **kwargs)
        async with factory() as work:
            task = await work.session.get(SubscriptionTask, evidence.producer.task_id)
            task.cancel_requested = True
            await work.commit()
        return resource

    monkeypatch.setattr(provisioner, "prepare", stop_after_provision)
    with pytest.raises(SubscriptionPlanGateError):
        async with factory() as work:
            await service.execute(command, work)
    async with factory() as work:
        assert (await work.runs.get(evidence.producer.run_id)).state is RunState.PREPARING_WORKTREE
        assert (
            await work.session.get(SubscriptionTask, evidence.producer.task_id)
        ).state == "blocked"
        await work.rollback()


@pytest.mark.integration
@pytest.mark.parametrize("mutation", ["resource", "approval"])
async def test_preparation_rechecks_resource_and_approval_before_queue(
    session_factory, tmp_path, monkeypatch, mutation
):
    from dataclasses import replace
    from datetime import UTC, datetime

    from forge.application.ports.commands import CommandRecoveryRequired
    from forge.application.services.approved_plan import ApprovedPlanError
    from forge.persistence.models import Approval

    factory, evidence, command, service, provisioner = await preparation_case(
        session_factory, tmp_path
    )
    original = provisioner.prepare

    async def change_after_provision(*args, **kwargs):
        resource = await original(*args, **kwargs)
        if mutation == "resource":
            return replace(resource, base_sha="b" * 40)
        async with factory() as work:
            approval = await work.session.scalar(
                select(Approval).where(
                    Approval.run_id == evidence.producer.run_id, Approval.gate == "plan"
                )
            )
            approval.invalidated_at = datetime.now(UTC)
            approval.invalidation_reason = "Test authority revocation"
            await work.commit()
        return resource

    monkeypatch.setattr(provisioner, "prepare", change_after_provision)
    expected_error = CommandRecoveryRequired if mutation == "resource" else ApprovedPlanError
    with pytest.raises(expected_error):
        async with factory() as work:
            await service.execute(command, work)
    async with factory() as work:
        assert (
            await work.session.get(SubscriptionTask, evidence.producer.task_id)
        ).state == "blocked"
        assert not [
            event
            for event in await work.events.list_after(evidence.producer.run_id, 0)
            if event.event_type == "run.worktree_prepared"
        ]
        await work.rollback()


@pytest.mark.integration
async def test_paused_prepared_run_cannot_admit_next_primary(session_factory, tmp_path):
    from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
    from test_subscription_usage import _reservation

    factory, evidence, command, service, _ = await preparation_case(session_factory, tmp_path)
    async with factory() as work:
        await service.execute(command, work)
    async with factory() as work:
        run = await work.runs.get(evidence.producer.run_id)
        await work.runs.pause(run.id, run.version, "run.paused", {}, actor_class="operator")
        await work.commit()
    assert (
        await SubscriptionDecisionExecutor(factory).admit_next("late-primary", _reservation())
        is None
    )


@pytest.mark.integration
async def test_paused_primary_does_not_starve_another_ready_run(session_factory, tmp_path):
    from datetime import UTC, datetime
    from uuid import uuid4

    from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
    from forge.domain.run import RunSnapshot
    from forge.persistence.models.scheduling import SubscriptionSchedulerRun
    from test_scheduler_acceptance import _admit_run, _enqueue, _route
    from test_subscription_usage import _reservation

    factory, evidence, command, service, _ = await preparation_case(session_factory, tmp_path)
    async with factory() as work:
        await service.execute(command, work)
    async with factory() as work:
        paused = await work.runs.get(evidence.producer.run_id)
        await work.runs.pause(paused.id, paused.version, "run.paused", {}, actor_class="operator")
        other = RunSnapshot(
            id=uuid4(), project_id=paused.project_id, task_id=paused.task_id, policy_version=1
        )
        await work.runs.create(other)
        parent = await _admit_run(work, other, (_route("p"), _route("p")))
        child = await _enqueue(
            work,
            other.id,
            provider="p",
            worktree="independent-ready-tree",
            parent_id=parent,
            paths=("apps",),
        )
        # Make the paused row the first fair-scheduling candidate. A later budget
        # rejection must not roll back the claim and select it forever.
        scheduler = await work.session.get(SubscriptionSchedulerRun, other.id)
        scheduler.last_claimed_at = datetime.now(UTC)
        await work.commit()
    admission = await SubscriptionDecisionExecutor(factory).admit_next(
        "available-worker", _reservation()
    )
    assert admission is not None and admission.task.task_id == child
    assert admission.attempt.run_id == other.id
    async with factory() as work:
        assert (await work.subscription_budget.usage(paused.id)).consumed.provider_attempts == 1
        assert (
            await work.session.get(SubscriptionTask, evidence.producer.task_id)
        ).state == "queued"
        await work.rollback()


@pytest.mark.integration
@pytest.mark.parametrize("control", ["run_pause", "task_cancel"])
async def test_primary_lease_renewal_observes_current_stop(session_factory, tmp_path, control):
    from datetime import timedelta

    from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
    from forge.persistence.repositories.scheduling import SchedulingConflict
    from test_subscription_usage import _reservation

    factory, evidence, command, service, _ = await preparation_case(session_factory, tmp_path)
    async with factory() as work:
        await service.execute(command, work)
    admission = await SubscriptionDecisionExecutor(factory).admit_next(
        "active-primary", _reservation()
    )
    assert admission is not None
    async with factory() as work:
        if control == "run_pause":
            run = await work.runs.get(evidence.producer.run_id)
            await work.runs.pause(run.id, run.version, "run.paused", {}, actor_class="operator")
        else:
            task = await work.session.get(SubscriptionTask, evidence.producer.task_id)
            task.cancel_requested = True
        await work.commit()
    async with factory() as work:
        with pytest.raises(SchedulingConflict):
            await work.scheduler.renew(admission.lease, timedelta(seconds=30))
        await work.rollback()
