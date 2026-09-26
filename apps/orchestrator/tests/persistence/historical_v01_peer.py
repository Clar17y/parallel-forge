"""Test-only driver; every imported Forge module must be the pinned v0.1 source."""

import asyncio
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

from alembic import command
from alembic.config import Config
from forge.application.adapters.git import LocalGitRepositoryInspector
from forge.application.services.approved_plan import ApprovedPlanLoader
from forge.application.services.auth import AuthenticatedActor
from forge.application.services.plan_evidence import PlanEvidenceValidator
from forge.application.services.projects import ProjectRegistrationRequest, ProjectService
from forge.application.services.runs import RunService
from forge.application.services.tasks import PlainTextTaskRequest, TaskService
from forge.artifacts.filesystem import FilesystemArtifactStore
from forge.domain.actor import AgentRole
from forge.domain.agent import AgentFinishStatus, AgentResult
from forge.domain.plan import PlanOutput
from forge.domain.policy import CommandSpec, RunnerMode
from forge.domain.run import RunState
from forge.observability.usage import UsageRecord
from forge.persistence.database import create_engine, create_session_factory
from forge.persistence.repositories.commands import PostgresCommandRepository
from forge.persistence.unit_of_work import PostgresUnitOfWork
from forge.settings import Settings
from forge.worker.composition import compose_worker_handlers
from historical_v01_delivery import HistoricalDelivery, advance_legacy
from pydantic import TypeAdapter
from sqlalchemy import text
from sqlalchemy.engine import make_url


class Planner:
    def __init__(self, commands):
        self.commands, self.requests = commands, []
        self.delivery = None

    async def execute(self, request):
        self.requests.append(request)
        if request.role is not AgentRole.PLANNER:
            assert self.delivery is not None
            return await self.delivery.execute(request, self.result_for)
        return self.result_for(
            request,
            PlanOutput(
                summary="Repair the counter and cover negative input",
                assumptions=(),
                affected_components=("src/counter.py", "tests/test_counter.py"),
                steps=("Repair increment and run policy checks",),
                required_checks=tuple(item.name for item in self.commands),
                risks=("Counter regression",),
                security_considerations=(),
                dependency_changes=(),
            ),
        )

    @staticmethod
    def result_for(request, output, *, tool_count=0):
        usage = UsageRecord(
            provider=request.provider,
            model=request.model,
            prompt_version=request.instruction_version,
            input_tokens=100,
            output_tokens=50,
            duration_ms=1,
            tool_call_count=tool_count,
            pricing_version="deterministic-historical-a9",
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


async def execute(payload, database_url):
    engine = create_engine(database_url)
    session_factory = create_session_factory(engine)
    factory = lambda: PostgresUnitOfWork(session_factory)
    data, source = Path(payload["data_root"]), Path(payload["source_root"])
    settings = Settings(data_root=data, prompt_root=source / "agents", provider_secret_reference="")
    handlers = None
    requests = []
    extra = {}
    scenario = payload.get("scenario", "plan")
    try:
        if payload["action"] in ("seed-plan", "seed-state"):
            actor = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())
            commands = TypeAdapter(tuple[CommandSpec, ...]).validate_python(payload["commands"])
            project = await ProjectService(factory, data_root=data).register(
                actor=actor,
                idempotency_key="historical-a9-project",
                request=ProjectRegistrationRequest(
                    name="Historical legacy counter",
                    repository_path=payload["repository"],
                    github_repository="example/counter-service",
                    default_branch="main",
                    runner_mode=RunnerMode.TRUSTED_HOST,
                    trusted_project=True,
                    commands=commands,
                ),
            )
            task = await TaskService(factory).create_plain_text(
                actor=actor,
                idempotency_key="historical-a9-task",
                request=PlainTextTaskRequest(
                    project_id=project.id, title="Correct counter", body=payload["task"]
                ),
            )
            run = await RunService(factory, data_root=data).create_run(
                actor=actor, idempotency_key="a9-run", task_id=task.id
            )
            gateway = Planner(commands)
            handlers = compose_worker_handlers(settings, session_factory, agent_gateway=gateway)
            repository = PostgresCommandRepository(session_factory)
            queued = await repository.claim_next(worker_id="historical-a9-plan", lease_seconds=60)
            assert queued is not None and queued.command_type == "start_planning"
            async with factory() as work:
                await handlers[queued.command_type](queued, work)
            await repository.complete(queued.id, worker_id="historical-a9-plan")
            requests = gateway.requests
            run_id = run.id
            if scenario != "plan":
                assert scenario in ("unfinished", "review")
                async with factory() as work:
                    run = await work.runs.get(run_id)
                case = SimpleNamespace(
                    run=run,
                    factory=factory,
                    settings=settings,
                    session_factory=session_factory,
                    store=FilesystemArtifactStore(settings.artifact_root),
                    handlers=handlers,
                )
                gateway.delivery = HistoricalDelivery(case, unfinished=scenario == "unfinished")
                extra = await advance_legacy(
                    case, scenario=scenario, control=payload.get("control")
                )
                extra["delivery"] = gateway.delivery.evidence
        else:
            assert payload["action"] in ("read-plan", "read-state")
            run_id = UUID(payload["run_id"])
        store = FilesystemArtifactStore(settings.artifact_root)
        validator = PlanEvidenceValidator(store, LocalGitRepositoryInspector(), data_root=str(data))
        async with factory() as work:
            assert not hasattr(work, "subscription")
            run = await work.runs.get(run_id)
            expected_state = {
                "plan": RunState.AWAITING_PLAN_APPROVAL,
                "unfinished": RunState.IMPLEMENTING,
                "review": RunState.AWAITING_PR_APPROVAL,
            }[scenario]
            assert run.state is expected_state
            if scenario == "plan":
                await validator.validate(work, run_id)
            else:
                await ApprovedPlanLoader(store).load(work, run_id)
        async with session_factory() as session:
            revision = await session.scalar(text("SELECT version_num FROM alembic_version"))
        return {
            "completed": True,
            "action": payload["action"],
            "scenario": scenario,
            "schema_revision": revision,
            "run_id": str(run.id),
            "run": {
                "state": run.state.value,
                "version": run.version,
                "pending_evidence_digest": run.pending_evidence_digest,
            },
            "provider_requests": [
                {
                    "execution_id": str(request.execution_id),
                    "run_id": str(request.run_id),
                    "role": request.role.value,
                    "provider": request.provider,
                    "model": request.model,
                    "instruction_version": request.instruction_version,
                    "instruction_digest": request.instruction_digest,
                }
                for request in requests
            ],
            "python_version": ".".join(map(str, sys.version_info[:3])),
            "model_calls": False,
            **extra,
        }
    finally:
        if handlers is not None:
            await handlers.aclose()
        await engine.dispose()


def loaded_forge_source(source):
    files, packages = {}, {}
    for name, module in tuple(sys.modules.items()):
        if name != "forge" and not name.startswith("forge."):
            continue
        package_paths = list(getattr(module, "__path__", ()))
        for value in package_paths:
            assert Path(value).resolve().is_relative_to(source / "apps/orchestrator/src/forge"), (
                name
            )
        if package_paths:
            packages[name] = [
                Path(value).resolve().relative_to(source).as_posix() for value in package_paths
            ]
        location = getattr(module, "__file__", None)
        if location is None:
            assert package_paths, name
            continue
        path = Path(location).resolve()
        assert path.is_relative_to(source / "apps/orchestrator/src/forge"), name
        files[path.relative_to(source).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    assert files and "forge.domain.subscription" not in sys.modules
    return {
        "loaded_forge_files": dict(sorted(files.items())),
        "loaded_forge_packages": dict(sorted(packages.items())),
    }


def main():
    assert sys.version_info[:2] == (3, 14)
    payload = json.loads(sys.stdin.readline())
    source = Path(payload["source_root"]).resolve()
    assert source == Path.cwd().resolve()
    loaded_forge_source(source)
    database_url = os.environ["FORGE_HISTORICAL_DATABASE_SECRET_URL"]
    assert re.fullmatch(r"forge_test_[0-9a-f]{32}", make_url(database_url).database or "")
    if payload["action"] in ("seed-plan", "seed-state"):
        config = Config(str(source / "alembic.ini"))
        config.set_main_option("script_location", str(source / "apps/orchestrator/migrations"))
        config.attributes["database_url"] = database_url
        command.upgrade(config, "head")
    result = asyncio.run(execute(payload, database_url))
    result.update(loaded_forge_source(source))
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
