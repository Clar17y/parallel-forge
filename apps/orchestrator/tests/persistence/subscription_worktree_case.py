"""Two approved runs share one origin; only their provider clients are scripted."""

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from forge.agents.subscription_protocol import ProviderToolCall
from forge.application.adapters.git import LocalGitRepositoryInspector
from forge.application.ports.subscription_gateway import (
    SubscriptionFailure,
    SubscriptionInvocationResult,
)
from forge.application.services.auth import AuthenticatedActor
from forge.application.services.plan_evidence import PlanEvidenceValidator
from forge.application.services.runs import RunService
from forge.application.services.subscription_broker import BrokerDenied
from forge.application.services.tasks import PlainTextTaskRequest, TaskService
from forge.artifacts.filesystem import FilesystemArtifactStore
from forge.domain.plan import ScopedPlanOutput
from forge.domain.run import RunState
from forge.domain.subscription import (
    AcceptanceCriterion,
    CheckResultEvidence,
    DelegateDecision,
    HandoffStatus,
    LogicalTaskContract,
    SpecialistPurpose,
    TaskBudget,
    TaskHandoff,
)
from forge.domain.tool import ToolName
from forge.evaluations.subscription_fixtures import (
    clean_slow_unit_markers,
    wait_slow_unit_entered,
)
from forge.persistence.models import RunCommand
from forge.persistence.repositories.commands import PostgresCommandRepository
from sqlalchemy import select
from test_subscription_counter_acceptance import CounterScript
from test_subscription_plan_gate import approve_proposal
from test_subscription_usage import _known

ALPHA = "alpha/value.txt"


class WorktreeScript(CounterScript):
    def __init__(self, mode):
        super().__init__()
        self.mode = mode
        self.worktree = None
        self.held = asyncio.Event()
        self.entered = asyncio.Event()
        self.start = asyncio.Event()
        self.commit_entered = threading.Event()
        self.commit_release = threading.Event()
        self.handoff = None

    @property
    def checks(self):
        return ("unit", "slow-unit") if self.mode == "check" else ("unit",)

    async def execute(self, request, broker):
        if request.run_state is RunState.PLANNING:
            return ScopedPlanOutput(
                summary="Update alpha in this run's managed worktree",
                assumptions=(),
                affected_components=(ALPHA,),
                steps=("Delegate alpha, check the change and retain its snapshot",),
                required_checks=self.checks,
                risks=("Concurrent changes in another managed worktree",),
                security_considerations=(),
                dependency_changes=(),
                owned_paths=(ALPHA,),
            )
        if request.task.purpose is SpecialistPurpose.PRIMARY:
            return DelegateDecision(
                run_id=request.task.run_id,
                parent_task_id=request.task.task_id,
                child_tasks=(
                    LogicalTaskContract(
                        run_id=request.task.run_id,
                        task_id=self.child_id,
                        parent_task_id=request.task.task_id,
                        purpose=SpecialistPurpose.INTEGRATION,
                        route=request.envelope.route_for(SpecialistPurpose.INTEGRATION),
                        owned_paths=(ALPHA,),
                        named_checks=self.checks,
                        budget=TaskBudget(max_provider_attempts=2),
                        max_repairs=0,
                        typed_acceptance=(
                            AcceptanceCriterion(
                                criterion_id="alpha",
                                description="Alpha is alpha-v2 and unit passes",
                                required_check_names=self.checks,
                            ),
                        ),
                    ),
                ),
                rationale="One owned writer and its actual controlled checks",
            )

        async def call(key, name, arguments):
            receipt = await broker(
                ProviderToolCall(
                    call_key=key,
                    thread_id="a3",
                    turn_id="worker",
                    name=name.value,
                    arguments=arguments,
                )
            )
            self.receipts.append(receipt)
            return receipt

        self.entered.set()
        await self.start.wait()
        try:
            write = await call(
                "write-alpha",
                ToolName.REPOSITORY_WRITE_FILE,
                {"path": ALPHA, "content": "alpha-v2\n"},
            )
        except BrokerDenied:
            assert self.mode == "reconciliation"
            # The controlled write happened, but its callback receipt could not
            # commit. Preserve the real unsettled effect and stop the fake client.
            self.held.set()
            return SubscriptionInvocationResult(
                attempt=request.attempt,
                failure=SubscriptionFailure.PROTOCOL,
                failure_detail="Injected callback receipt persistence failure",
                telemetry=_known(),
            )
        assert write["status"] == "succeeded"
        commit = None
        if self.mode == "checkpoint":
            pending = asyncio.create_task(
                call("checkpoint", ToolName.GIT_COMMIT, {"message": "Checkpoint alpha"})
            )
            assert await asyncio.to_thread(self.commit_entered.wait, 10)
            self.held.set()
            commit = await pending
            assert commit["status"] == "succeeded"
        checks = []
        for name in self.checks:
            if name == "slow-unit":
                assert isinstance(self.worktree, Path)
                clean_slow_unit_markers(self.worktree)
                pending = asyncio.create_task(
                    call(name, ToolName.BUILD_RUN_NAMED_CHECK, {"command_name": name})
                )
                assert await asyncio.to_thread(wait_slow_unit_entered, self.worktree)
                self.held.set()
                check = await pending
            else:
                check = await call(name, ToolName.BUILD_RUN_NAMED_CHECK, {"command_name": name})
            assert check["status"] == "succeeded"
            checks.append(check)
        snapshot = await call("snapshot", ToolName.GIT_DIFF, {"scope": "snapshot"})
        assert snapshot["status"] == "succeeded"
        self.handoff = TaskHandoff(
            run_id=request.task.run_id,
            task_id=request.task.task_id,
            attempt_id=request.attempt.attempt_id,
            status=HandoffStatus.COMPLETED,
            candidate_tree_digest=snapshot["metadata"]["candidate_tree_digest"],
            candidate_commit=None if commit is None else commit["metadata"]["new_sha"],
            changed_paths=(ALPHA,),
            check_results=tuple(
                CheckResultEvidence(
                    command_name=name,
                    exit_code=check["metadata"]["exit_code"],
                    passed=True,
                    output_digest=check["metadata"]["command_result_digest"],
                    duration_ms=check["metadata"]["command_duration_ms"],
                    receipt_id=check["operation_id"],
                )
                for name, check in zip(self.checks, checks, strict=True)
            ),
            evidence_receipt_ids=tuple(
                item["operation_id"] for item in (*checks, *((commit,) if commit else ()), snapshot)
            ),
            summary="Updated owned alpha, passed actual named checks and inspected its snapshot",
        )
        return self.handoff


class WorktreeRouter:
    def __init__(self, first):
        self.first = first
        self.by_run = {}

    def adapter(self, route):
        router = self

        class Adapter:
            def gateway_for(self, request, *, broker, lifecycle):
                script = router.by_run.get(request.task.run_id, router.first)
                return script.adapter(route).gateway_for(
                    request, broker=broker, lifecycle=lifecycle
                )

        value = Adapter()
        value.route = route
        return value


async def command_once(case, session_factory, run_id, expected):
    commands = PostgresCommandRepository(session_factory)
    command = await commands.claim_next(worker_id="a3-command", lease_seconds=60)
    assert command is not None and command.command_type == expected and command.run_id == run_id
    async with case.factory() as work:
        await case.handlers[expected](command, work)
    await commands.complete(command.id, worker_id="a3-command")


async def second_run(case, session_factory, router, script):
    actor = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())
    task = await TaskService(case.factory).create_plain_text(
        actor=actor,
        idempotency_key="a3-task-b",
        request=PlainTextTaskRequest(
            project_id=case.run.project_id,
            title="Update alpha in worktree B",
            body="Set alpha/value.txt to alpha-v2 and run unit.",
        ),
    )
    run = await RunService(case.factory, data_root=case.settings.data_root).create_run(
        actor=actor, idempotency_key="a3-run-b", task_id=task.id
    )
    router.by_run[run.id] = script
    await command_once(case, session_factory, run.id, "start_planning")
    worker = case.handlers.subscription_invocations("a3-plan-b")
    planned = await worker.run_once()
    assert planned is not None and planned.admission.task.run_id == run.id
    validator = PlanEvidenceValidator(
        FilesystemArtifactStore(case.settings.artifact_root),
        LocalGitRepositoryInspector(),
        data_root=str(case.settings.data_root),
    )
    await approve_proposal(
        case.factory,
        session_factory,
        SimpleNamespace(producer=SimpleNamespace(run_id=run.id)),
        validator,
        planned.application,
    )
    async with session_factory() as session:
        approval_id = await session.scalar(
            select(RunCommand.id).where(
                RunCommand.run_id == run.id, RunCommand.command_type == "approve_plan"
            )
        )
    await PostgresCommandRepository(session_factory).complete(
        approval_id, worker_id="approval-worker"
    )
    await command_once(case, session_factory, run.id, "prepare_worktree")
    delegated = await worker.run_once()
    assert delegated.admission.task.run_id == run.id
    assert delegated.application.disposition == "delegated"
    return run
