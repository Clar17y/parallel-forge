"""A2 lifecycle with real barriers, checkpoint, review, and external-drift handling."""

import asyncio
from pathlib import Path

import pytest
from forge.artifacts.filesystem import FilesystemArtifactStore
from forge.domain.approval import ApprovalGate
from forge.domain.run import RunState
from forge.domain.scheduling import SchedulerCapacityPolicy
from forge.domain.subscription import RolePreference, SpecialistPurpose
from forge.evaluations.subscription_fixtures import _run_git_command
from forge.persistence.models.scheduling import (
    SubscriptionScheduledEffect,
    SubscriptionScheduledTask,
    SubscriptionSchedulerRun,
)
from forge.persistence.models.subscription_results import SubscriptionAttemptResult
from forge.persistence.repositories.commands import PostgresCommandRepository
from forge.persistence.unit_of_work import PostgresUnitOfWork
from forge.tools.git import ControlledGit
from forge.worker.composition import compose_worker_handlers
from sqlalchemy import select
from subscription_catalog_script import CATALOG_PATHS, OPUS, CatalogLifecycleScript
from subscription_counter_manifest import retain_counter_manifest
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_counter_acceptance import PRIMARY, WRITER, prepared_counter_case


async def catalog_case(session_factory, tmp_path, monkeypatch):
    from forge.evaluations.subscription_fixtures import build_split_catalog_fixture

    script = CatalogLifecycleScript()
    async with PostgresUnitOfWork(session_factory) as work:
        await work.scheduler.configure_capacity(
            SchedulerCapacityPolicy(version=2, global_limit=2, run_limit=3, provider_limit=2)
        )
        await work.commit()
    case = await prepared_counter_case(
        session_factory,
        tmp_path,
        script=script,
        fixture_builder=build_split_catalog_fixture,
        task_body="Set alpha/value.txt to alpha-v2 and beta/value.txt to beta-v2; integrate and independently review both.",
        extra_preferences=(
            RolePreference(purpose=SpecialistPurpose.INTEGRATION, preferred_route=WRITER),
            RolePreference(purpose=SpecialistPurpose.INDEPENDENT_REVIEW, preferred_route=OPUS),
        ),
    )
    async with case.factory() as work:
        script.worktree = Path((await work.runs.get(case.run.id)).worktree_path)
    original = ControlledGit.commit_prepared

    def held_commit(git, tree, prepared):
        if tree.path == script.worktree:
            script.commit_entered.set()
            assert script.commit_release.wait(20), "harness must release the actual checkpoint"
        return original(git, tree, prepared)

    monkeypatch.setattr(ControlledGit, "commit_prepared", held_commit)

    async def observe_barrier(name, request):
        async with case.factory() as work:
            effects = (
                await work.session.scalars(
                    select(SubscriptionScheduledEffect).where(
                        SubscriptionScheduledEffect.run_id == request.task.run_id,
                        SubscriptionScheduledEffect.state == "admitted",
                    )
                )
            ).all()
            assert len(effects) == 1 and effects[0].whole_worktree_exclusive
            assert effects[0].task_id == request.task.task_id
            return {"kind": name, "effect_id": str(effects[0].id), "whole_worktree_exclusive": True}

    script.observe_barrier = observe_barrier
    return case


async def settled_parallel_writers(case):
    script, handlers = case.script, case.handlers
    completions = asyncio.Queue()

    async def invoke(owner):
        outcome = await handlers.subscription_invocations(owner).run_once()
        await completions.put(outcome)
        return outcome

    tasks = [asyncio.create_task(invoke("catalog-alpha"))]
    try:
        await asyncio.wait_for(script.any_entered.wait(), 10)
        tasks.append(asyncio.create_task(invoke("catalog-beta")))
        await asyncio.wait_for(
            asyncio.gather(*(event.wait() for event in script.entered.values())), 10
        )
        async with case.factory() as work:
            assert (
                await work.subscription_budget.usage(case.run.id)
            ).outstanding.provider_attempts == 2
        script.release.set()
        beta = await asyncio.wait_for(completions.get(), 20)
        assert not script.errors, script.errors
        assert beta.admission.task.task_id == script.children["beta"]
        assert (await handlers.subscription_decision_recovery.reconcile_all()).applied == 1
        delegated = await handlers.subscription_invocations("primary-conflicting-alpha").run_once()
        assert delegated.application.disposition == "delegated"
        async with case.factory() as work:
            before = await work.subscription_budget.usage(case.run.id)
            alpha = await work.session.get(SubscriptionScheduledTask, script.children["alpha"])
            repeat = await work.session.get(SubscriptionScheduledTask, script.repeat_id)
            assert alpha.state == "leased" and repeat.state == "queued"
            assert alpha.worktree_id == repeat.worktree_id
        # There is one free host slot. The already-owned path, not capacity,
        # suppresses this later conflicting task without an attempt debit.
        assert before.outstanding.provider_attempts == 1
        assert await handlers.subscription_invocations("conflicting-alpha").run_once() is None
        async with case.factory() as work:
            assert await work.subscription_budget.usage(case.run.id) == before
        return {
            "repeat_task_id": str(script.repeat_id),
            "suppressed_with_free_host_slot": True,
            "usage_at_conflict": before,
        }
    finally:
        script.release.set()
        script.alpha_release.set()
        await asyncio.gather(*tasks)
        assert not script.errors, script.errors
        assert (await handlers.subscription_decision_recovery.reconcile_all()).applied == 1


@pytest.mark.integration
@pytest.mark.parametrize("external_drift", [False, True])
async def test_catalog_checkpoint_review_and_drift(
    session_factory, tmp_path, monkeypatch, external_drift
):
    case = await catalog_case(session_factory, tmp_path, monkeypatch)
    script, handlers = case.script, case.handlers
    try:
        conflict = await settled_parallel_writers(case)
        # The woken primary may yield to the later alpha owner before delegating
        # integration. Each transition still uses the production invocation path.
        for index in range(5):
            if script.integration_id in script.handoffs:
                break
            result = await handlers.subscription_invocations(f"catalog-settles-{index}").run_once()
            assert not script.errors, script.errors
            assert result is not None and result.attempt.result.failure is None
            assert result.application is None or result.application.accepted, result.application
            recovered = await handlers.subscription_decision_recovery.reconcile_all()
            assert recovered.deferred == recovered.unsupported == 0
        assert script.integration_id in script.handoffs
        integrated = script.handoffs[script.integration_id]
        async with case.factory() as work:
            base_sha = (await work.runs.get(case.run.id)).base_sha
        committed_range = f"{base_sha}..{integrated.candidate_commit}"
        assert (
            _run_git_command(
                ["git", "rev-list", "--count", committed_range], script.worktree
            ).stdout.strip()
            == "1"
        )
        committed_paths = _run_git_command(
            ["git", "diff", "--name-only", committed_range], script.worktree
        ).stdout.splitlines()
        assert tuple(committed_paths) == CATALOG_PATHS
        checkpoint_diff = _run_git_command(
            ["git", "diff", "--no-ext-diff", "--no-textconv", committed_range], script.worktree
        ).stdout
        assert set(script.barrier_observations) == {"checkpoint", "slow-unit"}
        assert script.denial
        assert all(
            (script.worktree / path).read_text() == f"{path.split('/')[0]}-v2\n"
            for path in CATALOG_PATHS
        )
        # Recreate the worker before selecting and applying the fresh reviewer.
        await handlers.aclose()
        handlers = compose_worker_handlers(
            case.settings,
            session_factory,
            subscription_adapters=tuple(script.adapter(route) for route in (PRIMARY, WRITER, OPUS)),
        )
        selected = await handlers.subscription_invocations("catalog-review-selection").run_once()
        assert selected.application.disposition == "review_selected"
        reviewed = await handlers.subscription_invocations("catalog-reviewer").run_once()
        assert not script.errors, script.errors
        assert reviewed.admission.task.purpose is SpecialistPurpose.INDEPENDENT_REVIEW
        assert (
            reviewed.admission.task.owned_paths == () and reviewed.admission.task.named_checks == ()
        )
        assert reviewed.attempt.result.failure is None
        assert (await handlers.subscription_decision_recovery.reconcile_all()).applied == 1
        assert (await handlers.subscription_decision_recovery.reconcile_all()).applied == 0
        review = script.handoffs["review"]
        assert review.candidate_tree_digest == integrated.candidate_tree_digest
        assert review.candidate_commit == integrated.candidate_commit
        if external_drift:
            (script.worktree / CATALOG_PATHS[1]).write_text("human edit after review\n")
        accepted = await handlers.subscription_invocations("catalog-primary-accepts").run_once()
        assert not script.errors, script.errors
        assert accepted.admission.task.route.effective == PRIMARY
        if external_drift:
            assert not accepted.application.accepted
            assert accepted.application.disposition == "acceptance_repair_queued"
            assert (script.worktree / CATALOG_PATHS[1]).read_text() == "human edit after review\n"
        else:
            assert accepted.application.disposition == "acceptance_validation_queued"
            commands = PostgresCommandRepository(session_factory)
            command = await commands.claim_next(worker_id="catalog-validation", lease_seconds=60)
            assert command.command_type == "validate"
            async with case.factory() as work:
                await handlers["validate"](command, work)
            await commands.complete(command.id, worker_id="catalog-validation")
        async with case.factory() as work:
            run = await work.runs.get(case.run.id)
            scheduler = await work.session.get(SubscriptionSchedulerRun, run.id)
            source = await work.session.get(
                SubscriptionAttemptResult, accepted.admission.attempt.attempt_id
            )
            usage = await work.subscription_budget.usage(run.id)
            if external_drift:
                assert scheduler.candidate_state == "open"
                assert run.pending_gate is None
                assert usage.consumed.repairs == 1
                # The existing repair mechanism reserves its next attempt's
                # budget. It has no provider process or execution-capacity lease.
                assert usage.outstanding.provider_attempts == 1
                primary = await work.session.get(
                    SubscriptionScheduledTask, accepted.admission.task.task_id
                )
                assert (
                    primary.state == "queued"
                    and primary.lease_owner is None
                    and primary.lease_expires_at is None
                )
            else:
                assert scheduler.candidate_state == "closed"
                assert (
                    run.state is RunState.AWAITING_PR_APPROVAL
                    and run.pending_gate is ApprovalGate.PR
                )
                assert usage.consumed.repairs == 0
                assert usage.outstanding.provider_attempts == 0
            final_application = source.application_payload
        await retain_counter_manifest(
            case.factory,
            FilesystemArtifactStore(case.settings.artifact_root),
            case.fixture,
            script,
            run_id=case.run.id,
            tmp_path=tmp_path,
            scenario="A2-drift" if external_drift else "A2-integrated",
            grade={"deterministic_lifecycle": "passed", "live_provider_proof": "unproved"},
            worker_check_repair_sequences=0,
            restarted_before_handoff=False,
            operator_view={
                "conflict": conflict,
                "barriers": script.barrier_observations,
                "checkpoint_diff": checkpoint_diff,
                "final_application": final_application,
                "external_drift": external_drift,
                "restarted_before_review_selection": True,
            },
        )
    finally:
        script.commit_release.set()
        script.alpha_release.set()
        script.release.set()
        await handlers.aclose()
