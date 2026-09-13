"""Composed shared-worktree writers use real controlled scope and receipt authority."""

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest
from forge.agents.subscription_protocol import ProviderToolCall
from forge.application.services.subscription_broker import BrokerDenied
from forge.artifacts.filesystem import FilesystemArtifactStore
from forge.domain.plan import ScopedPlanOutput
from forge.domain.run import RunState
from forge.domain.scheduling import SchedulerCapacityPolicy
from forge.domain.subscription import (
    DelegateDecision,
    HandoffStatus,
    LogicalTaskContract,
    SpecialistPurpose,
    TaskBudget,
    TaskHandoff,
)
from forge.domain.tool import ToolName
from forge.evaluations.subscription_fixtures import build_split_catalog_fixture
from forge.persistence.unit_of_work import PostgresUnitOfWork
from forge.worker.composition import compose_worker_handlers
from subscription_counter_manifest import retain_counter_manifest
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_counter_acceptance import (
    PRIMARY,
    WRITER,
    CounterScript,
    prepared_counter_case,
)


class ParallelCatalogScript(CounterScript):
    def __init__(self):
        super().__init__()
        self.children = {name: uuid4() for name in ("alpha", "beta")}
        self.entered = {name: asyncio.Event() for name in self.children}
        self.written = {name: asyncio.Event() for name in self.children}
        self.release = asyncio.Event()
        self.any_entered = asyncio.Event()
        self.alpha_snapshot = asyncio.Event()
        self.denial = None

    async def execute(self, request, broker):
        if request.run_state is RunState.PLANNING:
            return ScopedPlanOutput(
                summary="Update both independent catalog values",
                assumptions=(),
                affected_components=("alpha/value.txt", "beta/value.txt"),
                steps=("Delegate disjoint owned edits and collect their snapshots",),
                required_checks=("unit",),
                risks=("Catalog values must remain independent",),
                security_considerations=(),
                dependency_changes=(),
                owned_paths=("alpha", "beta"),
            )
        if request.task.purpose is SpecialistPurpose.PRIMARY:
            return DelegateDecision(
                run_id=request.task.run_id,
                parent_task_id=request.task.task_id,
                child_tasks=tuple(
                    LogicalTaskContract(
                        run_id=request.task.run_id,
                        task_id=task_id,
                        parent_task_id=request.task.task_id,
                        purpose=SpecialistPurpose.ROUTINE_IMPLEMENTATION,
                        route=request.envelope.route_for(SpecialistPurpose.ROUTINE_IMPLEMENTATION),
                        owned_paths=(f"{name}/value.txt",),
                        named_checks=(),
                        budget=TaskBudget(max_provider_attempts=1),
                    )
                    for name, task_id in self.children.items()
                ),
                rationale="Two disjoint edits precede a whole-catalog validation",
            )
        name = next(
            name for name, task_id in self.children.items() if request.task.task_id == task_id
        )
        self.entered[name].set()
        self.any_entered.set()
        await asyncio.wait_for(self.release.wait(), timeout=20)

        async def call(key, tool, arguments):
            result = await broker(
                ProviderToolCall(
                    call_key=key,
                    thread_id="a2",
                    turn_id=str(request.attempt.attempt_id),
                    name=tool.value,
                    arguments=arguments,
                )
            )
            self.receipts.append(result)
            return result

        if name == "beta":
            with pytest.raises(BrokerDenied):
                await call(
                    "foreign-write",
                    ToolName.REPOSITORY_WRITE_FILE,
                    {"path": "alpha/value.txt", "content": "foreign\n"},
                )
            self.denial = True  # Harness observation, not an invented tool receipt.
        write = await call(
            "owned-write",
            ToolName.REPOSITORY_WRITE_FILE,
            {"path": f"{name}/value.txt", "content": f"{name}-v2\n"},
        )
        assert write["status"] == "succeeded"
        self.written[name].set()
        await asyncio.wait_for(
            asyncio.gather(*(event.wait() for event in self.written.values())), timeout=20
        )
        if name == "beta":
            await asyncio.wait_for(self.alpha_snapshot.wait(), timeout=20)
        snapshot = await call("snapshot", ToolName.GIT_DIFF, {"scope": "snapshot"})
        assert snapshot["status"] == "succeeded"
        if name == "alpha":
            self.alpha_snapshot.set()
        return TaskHandoff(
            run_id=request.task.run_id,
            task_id=request.task.task_id,
            attempt_id=request.attempt.attempt_id,
            status=HandoffStatus.COMPLETED,
            candidate_tree_digest=snapshot["metadata"]["candidate_tree_digest"],
            changed_paths=(f"{name}/value.txt",),
            check_results=(),
            evidence_receipt_ids=(snapshot["operation_id"],),
            summary=f"Updated only {name}",
        )


@pytest.mark.integration
async def test_two_composed_writers_preserve_disjoint_files_and_deny_foreign_write(
    session_factory, tmp_path
):
    script = ParallelCatalogScript()
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
        task_body="Set alpha/value.txt to alpha-v2 and beta/value.txt to beta-v2; keep the edits independent.",
    )
    handlers = case.handlers
    tasks = []
    try:
        workers = [handlers.subscription_invocations(f"a2-{name}") for name in script.children]
        tasks = [asyncio.create_task(workers[0].run_once())]
        # Production pollers retry a skipped brief DB lock. Start the second
        # claim after the first client is admitted, then hold both providers.
        await asyncio.wait_for(script.any_entered.wait(), timeout=10)
        tasks.append(asyncio.create_task(workers[1].run_once()))
        await asyncio.wait_for(
            asyncio.gather(*(event.wait() for event in script.entered.values())), timeout=10
        )
        async with case.factory() as work:
            active = await work.subscription_budget.usage(case.run.id)
            assert active.outstanding.provider_attempts == 2
        script.release.set()
        completed = await asyncio.wait_for(asyncio.gather(*tasks), timeout=35)
        assert not script.errors, script.errors
        assert all(value.attempt.result.failure is None for value in completed)
        assert all(
            value.attempt.settlement.disposition == "decision_pending" for value in completed
        )
        assert sorted(value.attempt.result.telemetry.tool_call_count for value in completed) == [
            2,
            2,
        ]
        await handlers.aclose()
        handlers = compose_worker_handlers(
            case.settings,
            session_factory,
            subscription_adapters=(script.adapter(PRIMARY), script.adapter(WRITER)),
        )
        recovery = await handlers.subscription_decision_recovery.reconcile_all()
        assert recovery.applied == 2 and recovery.deferred == 0
        assert (await handlers.subscription_decision_recovery.reconcile_all()).applied == 0
        async with case.factory() as work:
            run = await work.runs.get(case.run.id)
            calls = await work.tool_calls.list_for_run(run.id)
            usage = await work.subscription_budget.usage(run.id)
            for value in completed:
                handoff = await work.subscription_decisions.handoff_replay(
                    value.admission.attempt.attempt_id
                )
                assert handoff.accepted and handoff.disposition == "handoff_completed"
        assert usage.consumed.provider_attempts == 4 and usage.consumed.repairs == 0
        assert usage.outstanding.provider_attempts == 0
        assert len(calls) == 4
        assert script.denial is True
        for name in script.children:
            assert (Path(run.worktree_path) / name / "value.txt").read_text() == f"{name}-v2\n"
        await retain_counter_manifest(
            case.factory,
            FilesystemArtifactStore(case.settings.artifact_root),
            case.fixture,
            script,
            run_id=case.run.id,
            tmp_path=tmp_path,
            scenario="A2-writers",
            grade={"scope_and_handoff": "passed", "whole_A2": "incomplete"},
            worker_check_repair_sequences=0,
            operator_view={
                "foreign_write_denied_before_effect": script.denial,
                "concurrent_attempt_reservations": active.outstanding.provider_attempts,
                "accepted_handoffs_after_restart": recovery.applied,
                "not_exercised": ["named-check freeze", "checkpoint", "external drift"],
            },
        )
    finally:
        script.release.set()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await handlers.aclose()
