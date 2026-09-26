"""Real legacy plan evidence for the retained-v0.1 migration scenario."""

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from forge.application.adapters.git import LocalGitRepositoryInspector
from forge.application.services.auth import AuthenticatedActor
from forge.application.services.plan_evidence import PlanEvidenceValidator
from forge.application.services.projects import ProjectRegistrationRequest, ProjectService
from forge.application.services.runs import RunService
from forge.application.services.tasks import PlainTextTaskRequest, TaskService
from forge.artifacts.filesystem import FilesystemArtifactStore
from forge.domain.actor import AgentRole
from forge.domain.agent import AgentFinishStatus, AgentResult
from forge.domain.plan import PlanOutput
from forge.domain.policy import RunnerMode
from forge.domain.run import RunState
from forge.evaluations.subscription_fixtures import (
    _run_git_command,
    build_counter_service_fixture,
    get_acceptance_command_specs,
)
from forge.observability.usage import UsageRecord
from forge.persistence.repositories.commands import PostgresCommandRepository
from forge.persistence.unit_of_work import PostgresUnitOfWork
from forge.settings import Settings
from forge.worker.composition import compose_worker_handlers


class LegacyPlanGateway:
    """Explicit fake provider; the retained legacy planning service owns evidence."""

    def __init__(self):
        self.requests = []

    async def execute(self, request):
        self.requests.append(request)
        assert request.role is AgentRole.PLANNER
        return legacy_result(
            request,
            PlanOutput(
                summary="Repair the counter and cover negative input",
                assumptions=(),
                affected_components=("src/counter.py", "tests/test_counter.py"),
                steps=("Repair increment and run policy checks",),
                required_checks=tuple(command.name for command in get_acceptance_command_specs()),
                risks=("Counter regression",),
                security_considerations=(),
                dependency_changes=(),
            ),
        )


def legacy_result(request, output, *, tool_count=0):
    """Known scripted telemetry, never a measurement of a live provider."""
    usage = UsageRecord(
        provider=request.provider,
        model=request.model,
        prompt_version=request.instruction_version,
        input_tokens=100,
        output_tokens=50,
        duration_ms=1,
        tool_call_count=tool_count,
        pricing_version="deterministic-a9",
        estimated_cost_minor=1,
        currency="USD",
    )
    return AgentResult(
        execution_id=request.execution_id,
        role=request.role,
        finish_status=AgentFinishStatus.SUCCEEDED,
        output=output,
        parent_execution_id=None,
        provider=request.provider,
        model=request.model,
        instruction_digest=request.instruction_digest,
        usage=usage,
        usage_attempts=(usage,),
        tool_call_count=tool_count,
        duration_ms=1,
    )


async def legacy_plan_case(session_factory, tmp_path, *, gateway=None):
    fixture = build_counter_service_fixture(tmp_path / "repo")
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
        idempotency_key="a9-project",
        request=ProjectRegistrationRequest(
            name="Legacy counter",
            repository_path=str(fixture.path),
            github_repository="example/counter-service",
            default_branch="main",
            runner_mode=RunnerMode.TRUSTED_HOST,
            trusted_project=True,
            commands=get_acceptance_command_specs(),
        ),
    )
    task = await TaskService(factory).create_plain_text(
        actor=actor,
        idempotency_key="a9-task",
        request=PlainTextTaskRequest(
            project_id=project.id, title="Correct counter", body=fixture.case_contract.task
        ),
    )
    run = await RunService(factory, data_root=data).create_run(
        actor=actor,
        idempotency_key="a9-run",
        task_id=task.id,
    )
    settings = Settings(
        data_root=data,
        prompt_root=Path(__file__).resolve().parents[4] / "agents",
        provider_secret_reference="",
    )
    gateway = gateway or LegacyPlanGateway()
    handlers = compose_worker_handlers(settings, session_factory, agent_gateway=gateway)
    try:
        commands = PostgresCommandRepository(session_factory)
        command = await commands.claim_next(worker_id="a9-plan", lease_seconds=60)
        assert command is not None and command.command_type == "start_planning"
        async with factory() as work:
            outcome = await handlers[command.command_type](command, work)
        await commands.complete(command.id, worker_id="a9-plan")
        store = FilesystemArtifactStore(settings.artifact_root)
        validator = PlanEvidenceValidator(store, LocalGitRepositoryInspector(), data_root=str(data))
        async with factory() as work:
            run = await work.runs.get(run.id)
            assert await work.subscription.envelope_for_run(run.id) is None
            assert run.state is RunState.AWAITING_PLAN_APPROVAL, (
                outcome,
                await store.open_bytes(outcome.failure_artifact.digest)
                if outcome.failure_artifact
                else None,
            )
            evidence = await validator.validate(work, run.id)
        return SimpleNamespace(
            factory=factory,
            run=run,
            settings=settings,
            handlers=handlers,
            gateway=gateway,
            fixture=fixture,
            store=store,
            validator=validator,
            outcome=outcome,
            evidence=evidence,
        )
    except BaseException:
        await handlers.aclose()
        raise
