"""A3 uses actual same-origin worktrees and durable A barriers while B completes."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from forge.artifacts.filesystem import FilesystemArtifactStore
from forge.domain.scheduling import SchedulerCapacityPolicy
from forge.domain.subscription import RolePreference, SpecialistPurpose
from forge.domain.tool import ToolName
from forge.evaluations.check_evidence import read_check_evidence
from forge.evaluations.subscription_fixtures import (
    _run_git_command,
    build_split_catalog_fixture,
    release_slow_unit_barrier,
)
from forge.persistence.models.scheduling import (
    SubscriptionScheduledEffect,
    SubscriptionScheduledTask,
)
from forge.persistence.queries.subscription_tasks import SubscriptionTaskQuery
from forge.persistence.repositories.subscription import PostgresSubscriptionRepository
from forge.persistence.unit_of_work import PostgresUnitOfWork
from forge.tools.git import ControlledGit
from forge.worker.composition import compose_worker_handlers
from sqlalchemy import select
from subscription_counter_manifest import retain_counter_manifest
from subscription_worktree_case import ALPHA, WorktreeRouter, WorktreeScript, second_run
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_counter_acceptance import PRIMARY, WRITER, prepared_counter_case
from test_subscription_task_control_recovery import control


def child(run_id, script):
    return SimpleNamespace(task=SimpleNamespace(run_id=run_id, task_id=script.child_id))


async def pending_effects(factory, run_id):
    async with factory() as work:
        effects = (
            await work.session.scalars(
                select(SubscriptionScheduledEffect).where(
                    SubscriptionScheduledEffect.run_id == run_id,
                    SubscriptionScheduledEffect.state.in_(("admitted", "reconciling")),
                )
            )
        ).all()
        return [
            SimpleNamespace(
                id=effect.id,
                task_id=effect.task_id,
                whole_worktree_exclusive=effect.whole_worktree_exclusive,
            )
            for effect in effects
        ]


@pytest.mark.integration
@pytest.mark.parametrize("hold", ["check", "checkpoint", "paused", "reconciliation"])
async def test_other_worktree_finishes_while_origin_peer_is_held(
    session_factory, tmp_path, monkeypatch, hold
):
    first, second = WorktreeScript(hold), WorktreeScript("normal")
    router = WorktreeRouter(first)
    async with PostgresUnitOfWork(session_factory) as work:
        await work.scheduler.configure_capacity(
            SchedulerCapacityPolicy(version=2, global_limit=2, run_limit=3, provider_limit=2)
        )
        await work.commit()
    case = await prepared_counter_case(
        session_factory,
        tmp_path,
        script=router,
        fixture_builder=build_split_catalog_fixture,
        task_body="Set alpha/value.txt to alpha-v2; retain actual checks and a snapshot.",
        extra_preferences=(
            RolePreference(purpose=SpecialistPurpose.INTEGRATION, preferred_route=WRITER),
        ),
    )
    handlers, restarted, first_task, second_task = case.handlers, None, None, None
    try:
        # Use operator controls to set up two independently approved queued runs;
        # never teach the production poller a special test-only run filter.
        paused_a = await control(case.factory, child(case.run.id, first), "pause")
        other = await second_run(case, session_factory, router, second)
        paused_b = await control(case.factory, child(other.id, second), "pause")
        await control(
            case.factory, child(case.run.id, first), "resume", pause_id=paused_a.receipt_id
        )
        async with case.factory() as work:
            run_a, run_b = await work.runs.get(case.run.id), await work.runs.get(other.id)
            first.worktree, second.worktree = Path(run_a.worktree_path), Path(run_b.worktree_path)
            assert run_a.project_id == run_b.project_id and run_a.base_sha == run_b.base_sha
            a = await work.session.get(SubscriptionScheduledTask, first.child_id)
            b = await work.session.get(SubscriptionScheduledTask, second.child_id)
            assert a.worktree_id != b.worktree_id
        assert first.worktree != second.worktree
        origins = [
            _run_git_command(["git", "remote", "get-url", "origin"], path).stdout.strip()
            for path in (first.worktree, second.worktree)
        ]
        assert origins[0] == origins[1]
        assert (second.worktree / ALPHA).read_text() == "old\n"

        commit_prepared = ControlledGit.commit_prepared

        def hold_commit(git, tree, prepared):
            if hold == "checkpoint" and tree.path == first.worktree:
                first.commit_entered.set()
                assert first.commit_release.wait(30), "harness must release A's checkpoint"
            return commit_prepared(git, tree, prepared)

        monkeypatch.setattr(ControlledGit, "commit_prepared", hold_commit)
        record_receipt = PostgresSubscriptionRepository.record_operation_receipt

        async def failed_receipt(repository, binding, *, run_id, task_id, receipt):
            if hold == "reconciliation" and run_id == case.run.id:
                raise RuntimeError("A3 injected callback receipt persistence failure")
            return await record_receipt(
                repository, binding, run_id=run_id, task_id=task_id, receipt=receipt
            )

        monkeypatch.setattr(
            PostgresSubscriptionRepository, "record_operation_receipt", failed_receipt
        )
        first.start.set()
        first_task = asyncio.create_task(handlers.subscription_invocations("a3-a").run_once())
        if hold in {"check", "checkpoint"}:
            await asyncio.wait_for(first.held.wait(), 15)
            assert not first_task.done()
            effects = await pending_effects(case.factory, case.run.id)
            assert len(effects) == 1 and effects[0].whole_worktree_exclusive
            assert effects[0].task_id == first.child_id
        else:
            stopped_a = await asyncio.wait_for(first_task, 15)
            assert not first.errors, first.errors
            if hold == "paused":
                assert stopped_a.attempt.settlement.disposition == "decision_pending"
                paused_a = await control(case.factory, child(case.run.id, first), "pause")
                assert paused_a.status == "paused"
            else:
                assert stopped_a.attempt.result.failure is not None
                effects = await pending_effects(case.factory, case.run.id)
                assert len(effects) == 1
                assert (first.worktree / ALPHA).read_text() == "alpha-v2\n"
        view_before = await SubscriptionTaskQuery(session_factory).tasks(case.run.id)
        a_view = next(task for task in view_before["tasks"] if task["task_id"] == first.child_id)
        if hold == "paused":
            assert a_view["pause_requested"] and a_view["unsettled_effects"] == 0
        elif hold == "reconciliation":
            assert a_view["state"] == "reconciling" and a_view["unsettled_effects"] == 1
        async with case.factory() as work:
            usage_a = await work.subscription_budget.usage(case.run.id)

        await control(case.factory, child(other.id, second), "resume", pause_id=paused_b.receipt_id)
        second_task = asyncio.create_task(handlers.subscription_invocations("a3-b").run_once())
        await asyncio.wait_for(second.entered.wait(), 10)
        # The second provider has launched before any A release. Its admission
        # observes the explicit host ceiling, not a shared-origin mutex.
        async with case.factory() as work:
            active = await work.scheduler._active_count()
            assert active == (1 if hold == "paused" else 2)
        second.start.set()
        completed_b = await asyncio.wait_for(second_task, 15)
        assert not first.errors and not second.errors, (first.errors, second.errors)
        assert completed_b.attempt.result.failure is None
        assert completed_b.attempt.settlement.disposition == "decision_pending"
        assert completed_b.admission.task.run_id == other.id
        assert (second.worktree / ALPHA).read_text() == "alpha-v2\n"
        assert not first.commit_release.is_set()
        assert not (first.worktree / ".forge-acceptance/slow-unit.release").exists()
        if hold in {"check", "checkpoint"}:
            assert not first_task.done()
        elif hold == "reconciliation":
            assert len(await pending_effects(case.factory, case.run.id)) == 1
        async with case.factory() as work:
            assert await work.subscription_budget.usage(case.run.id) == usage_a

        # A separate composition can apply B once while A remains held, paused
        # or unresolved. Already-running A owns its original composition.
        restarted = compose_worker_handlers(
            case.settings,
            session_factory,
            subscription_adapters=(router.adapter(PRIMARY), router.adapter(WRITER)),
        )
        recovery = await restarted.subscription_decision_recovery.reconcile_all()
        assert recovery.applied == 1
        assert (await restarted.subscription_decision_recovery.reconcile_all()).applied == 0
        view_after = await SubscriptionTaskQuery(session_factory).tasks(case.run.id)
        assert (
            next(task for task in view_after["tasks"] if task["task_id"] == first.child_id)
            == a_view
        )
        async with case.factory() as work:
            replay = await work.subscription_decisions.handoff_replay(
                completed_b.admission.attempt.attempt_id
            )
            assert replay.accepted and replay.disposition == "handoff_completed"
            usage_b = await work.subscription_budget.usage(other.id)
            assert usage_b.consumed.provider_attempts == 3
            assert usage_b.consumed.named_checks == 1 and usage_b.consumed.repairs == 0
            assert usage_b.outstanding.provider_attempts == 0
        evidence = {
            "held_condition": hold,
            "peer_run_id": case.run.id,
            "same_origin": origins[0],
            "same_project": run_a.project_id,
            "host_limit": 2,
            "run_limit": 3,
            "active_at_b_admission": active,
            "a_before_b": view_before,
            "a_while_b_completed": view_after,
            "b_after_recovery": await SubscriptionTaskQuery(session_factory).tasks(other.id),
            "b_finished_before_a_release": True,
        }
        store = FilesystemArtifactStore(case.settings.artifact_root)
        unit = next(
            receipt
            for receipt in second.receipts
            if receipt["tool_name"] == ToolName.BUILD_RUN_NAMED_CHECK.value
        )
        grade = await read_check_evidence(
            case.fixture.case_contract,
            store,
            {**unit, "tool_call_id": unit["operation_id"]},
            command_name="unit",
        )
        assert grade is not None
        assert set(grade[0]) == set(case.fixture.case_contract.required_tests) and all(
            grade[0].values()
        )
        assert set(grade[1]) == set(case.fixture.case_contract.required_assertions) and all(
            grade[1].values()
        )
        await retain_counter_manifest(
            case.factory,
            store,
            case.fixture,
            second,
            run_id=other.id,
            tmp_path=tmp_path,
            grade=grade,
            scenario=f"A3-{hold}-B",
            worker_check_repair_sequences=0,
            operator_view=evidence,
        )
        first.commit_release.set()
        if hold == "check":
            release_slow_unit_barrier(first.worktree)
        settled_a = await asyncio.wait_for(first_task, 15)
        assert not first.errors, first.errors
        if hold in {"check", "checkpoint"}:
            assert settled_a.attempt.result.failure is None
            assert (await handlers.subscription_decision_recovery.reconcile_all()).applied == 1
        # Pause and uncertain receipt remain durable; finishing B must not clear
        # either one or mutate the other worktree's retained source.
        await retain_counter_manifest(
            case.factory,
            store,
            case.fixture,
            first,
            run_id=case.run.id,
            tmp_path=tmp_path,
            grade=None,
            scenario=f"A3-{hold}-A",
            worker_check_repair_sequences=0,
            operator_view=evidence,
            restarted_before_handoff=False,
        )
    finally:
        first.start.set()
        second.start.set()
        first.commit_release.set()
        if hold == "check" and first.worktree is not None:
            release_slow_unit_barrier(first.worktree)
        await asyncio.gather(
            *(task for task in (first_task, second_task) if task is not None),
            return_exceptions=True,
        )
        if restarted is not None:
            await restarted.aclose()
        await handlers.aclose()
