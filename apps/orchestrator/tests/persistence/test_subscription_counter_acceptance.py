"""Offline A1: real composition, managed Git, commands and PostgreSQL evidence.

Only the provider is scripted. This does not establish live client capability,
allowance-only enforcement, or the complete A1 live proof. The shared harness
supports the trusted-host case here and the separate real-Docker variant.
"""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from forge.agents.client_process import (
    ClientProcessReceipt,
    ClientProcessResult,
    terminal_launch_proof,
)
from forge.agents.subscription_protocol import ProviderToolCall
from forge.application.adapters.git import LocalGitRepositoryInspector
from forge.application.ports.subscription_gateway import SubscriptionInvocationResult
from forge.application.services.auth import AuthenticatedActor
from forge.application.services.plan_evidence import PlanEvidenceValidator
from forge.application.services.projects import ProjectRegistrationRequest, ProjectService
from forge.application.services.runs import RunService
from forge.application.services.tasks import PlainTextTaskRequest, TaskService
from forge.artifacts.filesystem import FilesystemArtifactStore
from forge.domain.plan import ScopedPlanOutput
from forge.domain.policy import RunnerMode
from forge.domain.run import RunState
from forge.domain.subscription import (
    AcceptanceCriterion,
    CheckResultEvidence,
    DelegateDecision,
    HandoffStatus,
    LogicalTaskContract,
    OperatorProfile,
    ReasoningEffort,
    RolePreference,
    RouteSpec,
    SpecialistPurpose,
    TaskBudget,
    TaskHandoff,
)
from forge.domain.tool import ToolName
from forge.evaluations.check_evidence import read_check_evidence
from forge.evaluations.subscription_fixtures import (
    _run_git_command,
    build_counter_service_fixture,
    get_acceptance_command_specs,
)
from forge.persistence.repositories.commands import PostgresCommandRepository
from forge.persistence.unit_of_work import PostgresUnitOfWork
from forge.settings import Settings
from forge.worker.composition import compose_worker_handlers
from subscription_counter_manifest import retain_counter_manifest
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_plan_gate import approve_proposal
from test_subscription_usage import _known

PRIMARY = RouteSpec(
    provider="openai", client="codex", model="gpt-6-astra", effort=ReasoningEffort.LOW
)
WRITER = RouteSpec(
    provider="google",
    client="gemini_cli",
    model="gemini-3.8-flash-medium",
    effort=ReasoningEffort.MEDIUM,
)
PATHS = ("src/counter.py", "tests/test_counter.py")


class CounterScript:
    """Fake provider uses only the broker capability provided by composition."""

    def __init__(self):
        self.requests = []
        self.receipts = []
        self.errors = []
        self.child_id = uuid4()

    def adapter(self, route):
        script = self

        class Adapter:
            def gateway_for(self, request, *, broker, lifecycle):
                class Gateway:
                    async def execute(self, value):
                        assert value == request
                        script.requests.append(request)
                        # Explicit fake process proof; real transport/supervisor
                        # coverage lives in the separate fake-process suites.
                        process = ClientProcessReceipt(str(uuid4()), 12345, "a1-script", 0.0)
                        await lifecycle.launch_intent(process.launch_id)
                        await lifecycle.started(process)
                        calls = []

                        async def tracked_broker(call):
                            receipt = await broker(call)
                            calls.append(receipt)
                            return receipt

                        try:
                            decision = await script.execute(request, tracked_broker)
                        except Exception as error:
                            script.errors.append(error)
                            raise
                        result = ClientProcessResult(
                            process, 0, (), 100, "", 0, False, False, "exited", True
                        )
                        await lifecycle.finished(process, result)
                        value = (
                            decision
                            if isinstance(decision, SubscriptionInvocationResult)
                            else SubscriptionInvocationResult(
                                attempt=request.attempt, decision=decision, telemetry=_known()
                            )
                        )
                        assert value.attempt == request.attempt
                        return replace(
                            value,
                            telemetry=replace(
                                value.telemetry,
                                tool_call_count=len(calls),
                                named_check_count=sum(
                                    item["tool_name"] == ToolName.BUILD_RUN_NAMED_CHECK.value
                                    for item in calls
                                ),
                            ),
                            launch_proof=terminal_launch_proof(result),
                        )

                return Gateway()

        value = Adapter()
        value.route = route
        return value

    async def execute(self, request, broker):
        if request.run_state is RunState.PLANNING:
            return ScopedPlanOutput(
                summary="Correct counter increment and cover -1",
                assumptions=(),
                affected_components=("src/counter.py", "tests/test_counter.py"),
                steps=("Delegate the bounded change, run unit, repair and self-review",),
                required_checks=("unit",),
                risks=("Counter regression",),
                security_considerations=(),
                dependency_changes=(),
                owned_paths=PATHS,
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
                        purpose=SpecialistPurpose.ROUTINE_IMPLEMENTATION,
                        route=request.envelope.route_for(SpecialistPurpose.ROUTINE_IMPLEMENTATION),
                        owned_paths=PATHS,
                        named_checks=("unit",),
                        budget=TaskBudget(max_provider_attempts=2, max_repairs=1),
                        max_repairs=1,
                        typed_acceptance=(
                            AcceptanceCriterion(
                                criterion_id="counter",
                                description="Increment adds one including -1",
                                required_check_names=("unit",),
                            ),
                        ),
                    ),
                ),
                rationale="One bounded routine writer owns repair and self-review",
            )

        async def call(key, name, arguments):
            receipt = await broker(
                ProviderToolCall(
                    call_key=key,
                    thread_id="a1",
                    turn_id="worker",
                    name=name.value,
                    arguments=arguments,
                )
            )
            self.receipts.append(receipt)
            return receipt

        original = await call("read-counter", ToolName.REPOSITORY_READ_FILE, {"path": PATHS[0]})
        tests = await call("read-tests", ToolName.REPOSITORY_READ_FILE, {"path": PATHS[1]})
        assert original["status"] == tests["status"] == "succeeded"
        source = original["metadata"]["content"]
        test_source = tests["metadata"]["content"]
        wrong = await call(
            "wrong-patch",
            ToolName.REPOSITORY_WRITE_FILE,
            {
                "path": PATHS[0],
                "content": source.replace("value + 2", "value + 0"),
            },
        )
        assert wrong["status"] == "succeeded"
        failed = await call("failed-unit", ToolName.BUILD_RUN_NAMED_CHECK, {"command_name": "unit"})
        assert failed["status"] == "failed" and failed["metadata"]["exit_code"] == 1
        assert "FORGE_EVAL_REPORT_V1" in failed["metadata"]["stdout_text"]
        fixed = await call(
            "repair-counter",
            ToolName.REPOSITORY_WRITE_FILE,
            {
                "path": PATHS[0],
                "content": source.replace("value + 2", "value + 1"),
            },
        )
        boundary = await call(
            "add-boundary",
            ToolName.REPOSITORY_WRITE_FILE,
            {
                "path": PATHS[1],
                "content": test_source + "    assert increment(-1) == 0\n",
            },
        )
        assert fixed["status"] == boundary["status"] == "succeeded"
        passed = await call(
            "passing-unit", ToolName.BUILD_RUN_NAMED_CHECK, {"command_name": "unit"}
        )
        assert passed["status"] == "succeeded" and passed["metadata"]["exit_code"] == 0
        assert "FORGE_EVAL_REPORT_V1" in passed["metadata"]["stdout_text"]
        snapshot = await call("self-review", ToolName.GIT_DIFF, {"scope": "snapshot"})
        assert snapshot["status"] == "succeeded"
        checks = tuple(
            CheckResultEvidence(
                command_name="unit",
                exit_code=item["metadata"]["exit_code"],
                passed=item["status"] == "succeeded",
                output_digest=item["metadata"]["command_result_digest"],
                duration_ms=item["metadata"]["command_duration_ms"],
                receipt_id=item["operation_id"],
            )
            for item in (failed, passed)
        )
        return TaskHandoff(
            run_id=request.task.run_id,
            task_id=request.task.task_id,
            attempt_id=request.attempt.attempt_id,
            status=HandoffStatus.COMPLETED,
            candidate_tree_digest=snapshot["metadata"]["candidate_tree_digest"],
            changed_paths=PATHS,
            check_results=checks,
            evidence_receipt_ids=tuple(item["operation_id"] for item in (failed, passed, snapshot)),
            summary="Repaired increment after failed unit; -1 covered; self-reviewed owned diff",
        )


async def prepared_counter_case(
    session_factory,
    tmp_path,
    *,
    script=None,
    worker_fallbacks=(),
    primary_budget=None,
    fixture_builder=build_counter_service_fixture,
    task_body=None,
    extra_preferences=(),
    start_work=True,
    runner_mode=RunnerMode.TRUSTED_HOST,
    runner_image="",
):
    """Prepare an approved fixture run, optionally delegating; caller closes handlers."""
    fixture = fixture_builder(tmp_path / "repo")
    _run_git_command(
        ["git", "remote", "add", "origin", "https://github.com/example/counter-service.git"],
        fixture.path,
    )
    data = tmp_path / "data"
    data.mkdir()
    factory = lambda: PostgresUnitOfWork(session_factory)
    actor = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())
    project = await ProjectService(factory, data_root=data).register(
        actor=actor,
        idempotency_key="a1-project",
        request=ProjectRegistrationRequest(
            name="Counter acceptance",
            repository_path=str(fixture.path),
            github_repository="example/counter-service",
            default_branch="main",
            runner_mode=runner_mode,
            trusted_project=runner_mode is RunnerMode.TRUSTED_HOST,
            commands=get_acceptance_command_specs(),
        ),
    )
    task = await TaskService(factory).create_plain_text(
        actor=actor,
        idempotency_key="a1-task",
        request=PlainTextTaskRequest(
            project_id=project.id,
            title="Correct counter",
            body=fixture.case_contract.task if task_body is None else task_body,
        ),
    )
    profile = OperatorProfile(
        profile_id=uuid4(),
        version=1,
        preferences=(
            RolePreference(purpose=SpecialistPurpose.PRIMARY, preferred_route=PRIMARY),
            RolePreference(
                purpose=SpecialistPurpose.ROUTINE_IMPLEMENTATION,
                preferred_route=WRITER,
                fallback_routes=worker_fallbacks,
            ),
            *extra_preferences,
        ),
    )
    async with factory() as work:
        await work.subscription.select_project_profile(project.id, profile)
        await work.commit()
    run = await RunService(factory, data_root=data).create_run(
        actor=actor,
        idempotency_key="a1-run",
        task_id=task.id,
    )
    settings = Settings(
        data_root=data,
        prompt_root=Path(__file__).resolve().parents[4] / "agents",
        provider_secret_reference="",
        runner_image=runner_image,
        subscription_primary_budget=primary_budget or TaskBudget(max_provider_attempts=64),
        subscription_attempt_budget=TaskBudget(
            max_duration_seconds=300,
            max_tool_calls=25,
            max_named_checks=2,
            max_provider_attempts=1,
            max_repairs=0,
        ),
    )
    script = CounterScript() if script is None else script
    handlers = compose_worker_handlers(
        settings,
        session_factory,
        subscription_adapters=tuple(
            script.adapter(route)
            for route in dict.fromkeys(
                route
                for preference in profile.preferences
                for route in (preference.preferred_route, *preference.fallback_routes)
            )
        ),
    )
    commands = PostgresCommandRepository(session_factory)

    async def command_once(expected):
        command = await commands.claim_next(worker_id="a1-command", lease_seconds=60)
        assert command is not None and command.command_type == expected
        async with factory() as work:
            await handlers[expected](command, work)
        await commands.complete(command.id, worker_id="a1-command")

    try:
        await command_once("start_planning")
        worker = handlers.subscription_invocations("a1-provider")
        planned = await worker.run_once()
        assert planned is not None and planned.application is not None
        assert await worker.run_once() is None
        validator = PlanEvidenceValidator(
            FilesystemArtifactStore(settings.artifact_root),
            LocalGitRepositoryInspector(),
            data_root=str(data),
        )
        await approve_proposal(
            factory,
            session_factory,
            SimpleNamespace(producer=SimpleNamespace(run_id=run.id)),
            validator,
            planned.application,
        )
        # The approval fixture has executed the handler; acknowledge its lease.
        from forge.persistence.models import RunCommand
        from sqlalchemy import select

        async with session_factory() as session:
            approval_id = await session.scalar(
                select(RunCommand.id).where(
                    RunCommand.run_id == run.id,
                    RunCommand.command_type == "approve_plan",
                )
            )
        await commands.complete(approval_id, worker_id="approval-worker")
        await command_once("prepare_worktree")
        delegated = None
        if start_work:
            delegated = await worker.run_once()
            assert delegated.application.disposition == "delegated"
        return SimpleNamespace(
            fixture=fixture,
            factory=factory,
            run=run,
            settings=settings,
            handlers=handlers,
            worker=worker,
            script=script,
            planned=planned,
            delegated=delegated,
        )
    except BaseException:
        await handlers.aclose()
        raise


@pytest.mark.integration
async def test_a1_counter_repair_retains_failed_and_passing_evidence(session_factory, tmp_path):
    case = await prepared_counter_case(session_factory, tmp_path)
    await verify_counter_repair(case, session_factory, tmp_path)


async def verify_counter_repair(case, session_factory, tmp_path):
    """Exercise the same controlled repair and durable handoff for either runner."""
    fixture = case.fixture
    factory = case.factory
    run = case.run
    settings = case.settings
    handlers = case.handlers
    worker = case.worker
    script = case.script
    try:
        worked = await worker.run_once()
        assert not script.errors, script.errors
        assert worked.attempt.result.failure is None
        assert worked.attempt.settlement.disposition == "decision_pending"
        await handlers.aclose()
        handlers = compose_worker_handlers(
            settings,
            session_factory,
            subscription_adapters=(script.adapter(PRIMARY), script.adapter(WRITER)),
        )
        recovery = await handlers.subscription_decision_recovery.reconcile_all()
        assert recovery.applied == 1 and recovery.deferred == recovery.unsupported == 0
        assert (await handlers.subscription_decision_recovery.reconcile_all()).applied == 0
        async with factory() as work:
            replay = await work.subscription_decisions.handoff_replay(
                worked.admission.attempt.attempt_id
            )
            assert replay.accepted and replay.disposition == "handoff_completed"
            assert (await work.runs.get(run.id)).state is RunState.IMPLEMENTING
            usage = await work.subscription_budget.usage(run.id)
            assert (
                usage.consumed.provider_attempts == 3 and usage.outstanding.provider_attempts == 0
            )
            assert usage.consumed.named_checks == 2
        assert [value.task.purpose for value in script.requests] == [
            SpecialistPurpose.PRIMARY,
            SpecialistPurpose.PRIMARY,
            SpecialistPurpose.ROUTINE_IMPLEMENTATION,
        ]
        assert script.requests[-1].task.task_id == script.child_id
        assert script.requests[0].task.route.effective == PRIMARY
        assert script.requests[-1].task.route.effective == WRITER
        passing = next(
            item
            for item in script.receipts
            if item["tool_name"] == ToolName.BUILD_RUN_NAMED_CHECK.value
            and item["status"] == "succeeded"
        )
        graded = await read_check_evidence(
            fixture.case_contract,
            FilesystemArtifactStore(settings.artifact_root),
            {**passing, "tool_call_id": passing["operation_id"]},
            command_name="unit",
        )
        assert graded is not None
        assert set(graded[0]) == set(fixture.case_contract.required_tests) and all(
            graded[0].values()
        )
        assert set(graded[1]) == set(fixture.case_contract.required_assertions) and all(
            graded[1].values()
        )
        manifest = await retain_counter_manifest(
            factory,
            FilesystemArtifactStore(settings.artifact_root),
            fixture,
            script,
            run_id=run.id,
            tmp_path=tmp_path,
            grade=graded,
        )
        assert manifest.is_file()
        return manifest
    finally:
        await handlers.aclose()
