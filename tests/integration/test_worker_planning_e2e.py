"""HTTP authentication and durable worker proof for the plan approval boundary."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from forge.api.app import create_app
from forge.application.services.worker import Worker
from forge.domain.agent import AgentFinishStatus, AgentResult
from forge.domain.plan import PlanOutput
from forge.observability.usage import UsageRecord
from forge.persistence.models import (
    AgentExecution,
    Approval,
    ApprovalChallenge,
    ModelUsage,
    Run,
    RunCommand,
)
from forge.persistence.repositories.commands import PostgresCommandRepository
from forge.settings import Settings
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


class RecordingPlanner:
    def __init__(self):
        self.requests = []

    async def execute(self, request):
        self.requests.append(request)
        return AgentResult(
            execution_id=request.execution_id,
            role=request.role,
            finish_status=AgentFinishStatus.SUCCEEDED,
            provider=request.provider,
            model=request.model,
            instruction_digest=request.instruction_digest,
            output=PlanOutput(
                summary="Update the README",
                assumptions=(),
                affected_components=("README.md",),
                steps=("Edit the README",),
                required_checks=("unit",),
                risks=("Documentation can become stale",),
                security_considerations=(),
                dependency_changes=(),
            ),
            usage=UsageRecord(
                provider=request.provider,
                model=request.model,
                prompt_version=request.instruction_version,
                input_tokens=11,
                output_tokens=7,
                estimated_cost_minor=1,
                pricing_version="fixture-v1",
                currency="USD",
                provider_request_id=f"planner-{len(self.requests)}",
            ),
            tool_call_count=0,
            duration_ms=0,
        )


def git(repository: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repository), *args], text=True, stderr=subprocess.STDOUT
    ).strip()


async def post(client, path, body, expected=201):
    response = await client.post(path, json=body, headers={"Idempotency-Key": str(uuid4())})
    assert response.status_code == expected, response.text
    return response.json()


async def tick_success(worker, session_factory, run_id):
    result = await worker.tick()
    if result is not True:
        async with session_factory() as session:
            commands = (
                await session.scalars(select(RunCommand).where(RunCommand.run_id == run_id))
            ).all()
            details = [
                (command.command_type, command.status, command.error_summary)
                for command in commands
            ]
        pytest.fail(f"worker did not complete command: {details}")


async def tamper_restart(session, run_id, scenario):
    queued = await session.scalar(
        select(RunCommand).where(
            RunCommand.run_id == run_id,
            RunCommand.command_type == "start_planning",
            RunCommand.status == "PENDING",
        )
    )
    if scenario.endswith("removed_feedback"):
        queued.payload = {"semantic_attempt": queued.payload["semantic_attempt"]}
    elif scenario.endswith("null_feedback"):
        queued.payload = {**queued.payload, "feedback_digest": None}
    elif scenario.endswith("substituted_command"):
        queued.id = uuid4()
    else:
        queued.payload = {**queued.payload, "feedback_digest": "b" * 64}


def handlers(settings, session_factory, gateway):
    from forge.worker.composition import compose_worker_handlers

    return compose_worker_handlers(settings, session_factory, agent_gateway=gateway)


@pytest_asyncio.fixture
async def workflow_session_factory(migrated_database_url):
    from forge.persistence.database import create_engine, create_session_factory

    engine = create_engine(migrated_database_url)
    try:
        yield create_session_factory(engine)
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    "scenario",
    [
        "approve",
        "revision",
        "revision_twice",
        "revision_crash",
        "revision_crash_tamper",
        "revision_crash_removed_feedback",
        "revision_crash_null_feedback",
        "revision_crash_substituted_command",
        "revision_removed_feedback",
        "revision_null_feedback",
        "revision_substituted_command",
        "drift_before_api",
        "drift_before_worker",
        "drift_before_worker_crash",
        "drift_before_worker_crash_queue",
        "drift_before_worker_crash_approval",
        "drift_before_worker_crash_actor",
        "wrong_actor",
        "wrong_approval_policy",
    ],
)
async def test_http_plan_requires_exact_approval_before_preparation(
    tmp_path, workflow_session_factory, scenario
):
    session_factory = workflow_session_factory
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "README.md").write_text("Example repository\n", encoding="utf-8")
    git(repository, "init", "-b", "main")
    git(repository, "config", "user.name", "Forge fixture")
    git(repository, "config", "user.email", "fixture@example.invalid")
    git(repository, "add", "README.md")
    git(repository, "commit", "-m", "fixture")
    git(repository, "remote", "add", "origin", "https://github.com/example/fixture.git")
    data_root = tmp_path / "data"
    data_root.mkdir()
    settings = Settings(
        _env_file=None,
        data_root=data_root,
        prompt_root=Path(__file__).resolve().parents[2] / "agents",
        web_origin="http://127.0.0.1:3000",
        runner_image="sha256:" + "1" * 64,
    )
    app = create_app(settings, session_factory=session_factory)
    bootstrap = await app.state.auth_service.issue_bootstrap()
    gateway = RecordingPlanner()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url=settings.web_origin,
        headers={"Origin": settings.web_origin},
    ) as client:
        auth = await post(client, "/api/auth/bootstrap", {"token": bootstrap}, 200)
        client.headers["X-CSRF-Token"] = auth["csrf_token"]
        project = await post(
            client,
            "/api/projects",
            {
                "name": "Planning fixture",
                "repository_path": str(repository),
                "github_repository": "example/fixture",
                "default_branch": "main",
                "planner_model": {"provider": "test", "model": "fixture-planner"},
                "commands": [
                    {
                        "kind": "test",
                        "name": "unit",
                        "argv": ["python", "-m", "pytest"],
                        "timeout_seconds": 60,
                    }
                ],
            },
        )
        task = await post(
            client,
            "/api/tasks",
            {"project_id": project["id"], "title": "Update docs", "body": "Clarify the README."},
        )
        created = await post(client, "/api/runs", {"task_id": task["id"]})
        run_id = UUID(created["id"])
        worker = Worker(
            PostgresCommandRepository(session_factory),
            session_factory,
            handlers=handlers(settings, session_factory, gateway),
            worker_id="planning-e2e",
        )
        await tick_success(worker, session_factory, run_id)
        async with session_factory() as session:
            run = await session.get(Run, run_id)
            assert run.state == "AWAITING_PLAN_APPROVAL"
            version, digest = run.version, run.pending_evidence_digest
            commands = (
                await session.scalars(select(RunCommand).where(RunCommand.run_id == run_id))
            ).all()
            assert [c.command_type for c in commands] == ["start_planning"]
            assert (
                len(
                    (
                        await session.scalars(select(ModelUsage).where(ModelUsage.run_id == run_id))
                    ).all()
                )
                == 1
            )
        assert len(gateway.requests) == 1
        challenge = await post(
            client,
            f"/api/runs/{run_id}/approval-challenges",
            {"gate": "plan", "run_version": version, "evidence_digest": digest},
            200,
        )
        body = {
            "gate": "plan",
            "run_version": version,
            "evidence_digest": digest,
            "challenge_token": challenge["token"],
        }
        if scenario.startswith("revision"):
            feedback = "Keep the examples and explain installation first."
            await post(
                client,
                f"/api/runs/{run_id}/commands",
                {
                    "command_type": "request_plan_revision",
                    "expected_run_version": version,
                    "feedback": feedback,
                },
                202,
            )
            if scenario.startswith("revision_crash"):
                original_complete = worker._commands.complete

                async def crash_completion(*args, **kwargs):
                    raise RuntimeError("injected crash before queue completion")

                worker._commands.complete = crash_completion
                with pytest.raises(RuntimeError, match="injected crash"):
                    await worker.tick()
                worker._commands.complete = original_complete
                async with session_factory() as session, session.begin():
                    revision_command = await session.scalar(
                        select(RunCommand).where(
                            RunCommand.run_id == run_id,
                            RunCommand.command_type == "request_plan_revision",
                        )
                    )
                    assert revision_command.status == "LEASED"
                    revision_command.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
                    if scenario != "revision_crash":
                        await tamper_restart(session, run_id, scenario)
            if scenario.startswith("revision_crash_"):
                assert await worker.tick() is False
                assert await worker.tick() is False
                assert len(gateway.requests) == 1
                async with session_factory() as session:
                    run = await session.get(Run, run_id)
                    assert run.state == "PLANNING" and run.version == version + 1
                return
            await tick_success(worker, session_factory, run_id)
            if scenario in {
                "revision_removed_feedback",
                "revision_null_feedback",
                "revision_substituted_command",
            }:
                async with session_factory() as session, session.begin():
                    await tamper_restart(session, run_id, scenario)
                assert await worker.tick() is False
                assert len(gateway.requests) == 1
                return
            async with session_factory() as session:
                challenges = (
                    await session.scalars(
                        select(ApprovalChallenge).where(ApprovalChallenge.run_id == run_id)
                    )
                ).all()
                assert challenges and all(c.expires_at <= datetime.now(UTC) for c in challenges)
                assert all(c.consumed_at is None for c in challenges)
            stale = await client.post(f"/api/runs/{run_id}/approvals", json=body)
            assert stale.status_code == 409
            await tick_success(worker, session_factory, run_id)
            assert len(gateway.requests) == 2
            assert feedback in gateway.requests[1].context.model_dump_json()
            assert feedback not in gateway.requests[1].system_instruction
            assert (
                gateway.requests[1].context.original_task
                == gateway.requests[0].context.original_task
            )
            async with session_factory() as session:
                run = await session.get(Run, run_id)
                assert run.state == "AWAITING_PLAN_APPROVAL" and run.version > version
                commands = (
                    await session.scalars(select(RunCommand).where(RunCommand.run_id == run_id))
                ).all()
                assert not any(c.command_type == "prepare_worktree" for c in commands)
                planning = [c for c in commands if c.command_type == "start_planning"]
                assert len(planning) == 2 and len({c.idempotency_key for c in planning}) == 2
                second_version, second_digest = run.version, run.pending_evidence_digest
                assert second_digest != digest
            if scenario == "revision_twice":
                await post(
                    client,
                    f"/api/runs/{run_id}/commands",
                    {
                        "command_type": "request_plan_revision",
                        "expected_run_version": second_version,
                        "feedback": feedback,
                    },
                    202,
                )
                await tick_success(worker, session_factory, run_id)
                await tick_success(worker, session_factory, run_id)
                assert len(gateway.requests) == 3
                assert feedback in gateway.requests[2].context.model_dump_json()
                async with session_factory() as session:
                    run = await session.get(Run, run_id)
                    assert run.state == "AWAITING_PLAN_APPROVAL"
                    assert run.pending_evidence_digest not in {digest, second_digest}
            return
        if scenario == "drift_before_api":
            git(repository, "commit", "--allow-empty", "-m", "base changed")
            stale = await client.post(f"/api/runs/{run_id}/approvals", json=body)
            assert stale.status_code == 409
            async with session_factory() as session:
                run = await session.get(Run, run_id)
                assert run.state == "AWAITING_PLAN_APPROVAL" and run.version == version
                assert not (
                    await session.scalars(select(Approval).where(Approval.run_id == run_id))
                ).all()
                commands = (
                    await session.scalars(select(RunCommand).where(RunCommand.run_id == run_id))
                ).all()
                assert len(commands) == 1
            return
        accepted = await post(client, f"/api/runs/{run_id}/approvals", body, 202)
        if scenario in {"wrong_actor", "wrong_approval_policy"}:
            async with session_factory() as session:
                if scenario == "wrong_actor":
                    queued = await session.scalar(
                        select(RunCommand).where(
                            RunCommand.run_id == run_id,
                            RunCommand.command_type == "approve_plan",
                        )
                    )
                    queued.actor_id = uuid4()
                else:
                    approval = await session.get(Approval, UUID(accepted["approval_id"]))
                    approval.policy_version += 1
                await session.commit()
            assert await worker.tick() is False
            async with session_factory() as session:
                run = await session.get(Run, run_id)
                approval = await session.get(Approval, UUID(accepted["approval_id"]))
                assert run.state == "AWAITING_PLAN_APPROVAL" and run.version == version
                assert approval.invalidated_at is None
                commands = (
                    await session.scalars(select(RunCommand).where(RunCommand.run_id == run_id))
                ).all()
                assert not any(c.command_type == "prepare_worktree" for c in commands)
            assert len(gateway.requests) == 1
            return
        if scenario.startswith("drift_before_worker"):
            git(repository, "commit", "--allow-empty", "-m", "base changed")
            if scenario.startswith("drift_before_worker_crash"):
                original_complete = worker._commands.complete

                async def crash_stale_completion(*args, **kwargs):
                    raise RuntimeError("injected stale-approval acknowledgement crash")

                worker._commands.complete = crash_stale_completion
                with pytest.raises(RuntimeError, match="acknowledgement crash"):
                    await worker.tick()
                worker._commands.complete = original_complete
                async with session_factory() as session, session.begin():
                    queued_approval = await session.scalar(
                        select(RunCommand).where(
                            RunCommand.run_id == run_id,
                            RunCommand.command_type == "approve_plan",
                        )
                    )
                    assert queued_approval.status == "LEASED"
                    queued_approval.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
                    if scenario.endswith("_queue"):
                        await tamper_restart(session, run_id, "substituted_command")
                    elif scenario.endswith("_approval"):
                        approval = await session.get(Approval, UUID(accepted["approval_id"]))
                        approval.evidence_digest = "b" * 64
                    elif scenario.endswith("_actor"):
                        queued_approval.actor_id = uuid4()
                if scenario != "drift_before_worker_crash":
                    assert await worker.tick() is False
                    assert await worker.tick() is False
                    assert len(gateway.requests) == 1
                    async with session_factory() as session:
                        run = await session.get(Run, run_id)
                        assert run.state == "PLANNING" and run.version == version + 1
                    return
            await tick_success(worker, session_factory, run_id)
            async with session_factory() as session:
                run = await session.get(Run, run_id)
                assert run.state == "PLANNING" and run.version > version
                approval = await session.get(Approval, UUID(accepted["approval_id"]))
                assert approval.invalidated_at is not None
                commands = (
                    await session.scalars(select(RunCommand).where(RunCommand.run_id == run_id))
                ).all()
                assert not any(c.command_type == "prepare_worktree" for c in commands)
                assert len([c for c in commands if c.command_type == "start_planning"]) == 2
            await tick_success(worker, session_factory, run_id)
            assert len(gateway.requests) == 2
            assert gateway.requests[1].context.base_commit == git(repository, "rev-parse", "HEAD")
            return
        replay = await client.post(f"/api/runs/{run_id}/approvals", json=body)
        assert replay.status_code == 409
        await tick_success(worker, session_factory, run_id)
        async with session_factory() as session:
            run = await session.get(Run, run_id)
            assert run.state == "PREPARING_WORKTREE"
            commands = (
                await session.scalars(select(RunCommand).where(RunCommand.run_id == run_id))
            ).all()
            preparation = [c for c in commands if c.command_type == "prepare_worktree"]
            assert len(preparation) == 1
            assert preparation[0].status == "PENDING"
            approvals = (
                await session.scalars(select(Approval).where(Approval.run_id == run_id))
            ).all()
            assert len(approvals) == 1 and str(approvals[0].id) == accepted["approval_id"]
            executions = (
                await session.scalars(select(AgentExecution).where(AgentExecution.run_id == run_id))
            ).all()
            assert len(executions) == 1
        assert len(gateway.requests) == 1


@pytest.mark.parametrize(
    "read_path,expected_status", [("README.md", "succeeded"), (".env", "failed")]
)
async def test_composed_planner_tools_use_durable_admission(
    tmp_path, workflow_session_factory, read_path, expected_status
):
    from dataclasses import replace
    from types import SimpleNamespace

    from forge.agents.prompt_loader import PromptLoader
    from forge.application.services.planning import PlanningService
    from forge.domain.tool import ToolName
    from forge.observability.redaction import Redactor
    from forge.persistence.models import ToolCall
    from forge.persistence.unit_of_work import PostgresUnitOfWork
    from forge.tools.repository import RepositoryReader
    from forge.worker.composition import BoundPlanningGateway
    from test_planning_failed_usage import _build_case

    factory = workflow_session_factory
    case = await _build_case(tmp_path, factory, fail_invalid=False)
    (tmp_path / "repository" / ".env").write_text(
        "PRIVATE_FIXTURE_MARKER=keep-out", encoding="utf-8"
    )
    captured = []

    class ToolCallingPlanner(RecordingPlanner):
        def __init__(self, tool_provider):
            super().__init__()
            self.tool_provider = tool_provider

        async def execute(self, request):
            bound = self.tool_provider.tools_for(request)
            assert set(bound.names) == {
                ToolName.REPOSITORY_LIST_FILES,
                ToolName.REPOSITORY_READ_FILE,
                ToolName.REPOSITORY_SEARCH,
                ToolName.REPOSITORY_READ_INSTRUCTIONS,
            }
            tool = next(
                tool for tool in bound.tools if tool.name == ToolName.REPOSITORY_READ_FILE.value
            )
            result = await tool.func(
                path=read_path,
                tool_context=SimpleNamespace(
                    invocation_id="fixture-invoke", function_call_id="fixture-call"
                ),
            )
            captured.append(result)
            assert "PRIVATE_FIXTURE_MARKER" not in str(result)
            response = await super().execute(request)
            return response.model_copy(
                update={"usage": replace(response.usage, tool_call_count=1), "tool_call_count": 1}
            )

    redactor = Redactor()
    prompts = PromptLoader(tmp_path / "prompts")
    gateway = BoundPlanningGateway(
        unit_of_work_factory=lambda: PostgresUnitOfWork(factory, redactor=redactor),
        artifact_store=case.artifact_store,
        prompt_loader=prompts,
        redactor=redactor,
        underlying_gateway_factory=ToolCallingPlanner,
    )
    service = PlanningService(
        gateway,
        case.artifact_store,
        prompts,
        lambda policy: RepositoryReader(
            policy.repository_path,
            secret_paths=policy.effective_secret_paths,
            force_python_search=True,
        ),
    )
    async with PostgresUnitOfWork(factory, redactor=redactor) as work:
        outcome = await service.execute(case.command, work)
    assert len(captured) == 1
    assert captured[0]["status"] == expected_status, captured[0]
    if read_path == ".env":
        assert captured[0]["error"]["code"] == "adapter_error"
    assert outcome.run_state.value == "AWAITING_PLAN_APPROVAL"
    async with factory() as session:
        execution = (
            await session.scalars(
                select(AgentExecution).where(AgentExecution.run_id == case.run_id)
            )
        ).one()
        call = (await session.scalars(select(ToolCall).where(ToolCall.run_id == case.run_id))).one()
        assert call.agent_execution_id == execution.id
        assert call.result_metadata["step_id"] == str(execution.step_id)
        assert "PRIVATE_FIXTURE_MARKER" not in str(call.result_metadata)
        assert call.tool_name == "repository.read_file" and call.status == expected_status.upper()
        assert captured[0]["agent_execution_id"] == str(execution.id)
        assert captured[0]["step_id"] == str(execution.step_id)
        usage = (
            await session.scalars(select(ModelUsage).where(ModelUsage.run_id == case.run_id))
        ).one()
        assert usage.tool_call_count == 1


@pytest.mark.parametrize(
    "drift",
    [
        "none",
        "base",
        "run_base",
        "task_digest",
        "legacy_evidence",
        "policy",
        "runner",
        "budget",
        "plan_digest",
        "expected_version",
    ],
)
async def test_http_authorization_revalidates_persisted_plan_gate(
    tmp_path, workflow_session_factory, drift
):
    import hashlib
    import json

    from forge.agents.prompt_loader import PromptLoader
    from forge.application.services.planning import PlanningService
    from forge.artifacts.filesystem import FilesystemArtifactStore
    from forge.persistence.models import Project, ProjectPolicyVersion
    from forge.persistence.unit_of_work import PostgresUnitOfWork
    from forge.tools.repository import RepositoryReader
    from test_planning_failed_usage import _build_case

    factory = workflow_session_factory
    case = await _build_case(tmp_path, factory, fail_invalid=False)
    repository = tmp_path / "repository"
    git(repository, "init", "-b", "main")
    git(repository, "config", "user.name", "Forge fixture")
    git(repository, "config", "user.email", "fixture@example.invalid")
    git(repository, "add", "README.md")
    git(repository, "commit", "-m", "fixture")
    async with factory() as session, session.begin():
        run = await session.get(Run, case.run_id)
        run.base_sha = git(repository, "rev-parse", "HEAD")
        project = await session.get(Project, run.project_id)
        repository_identity = project.github_repository
        project_id = run.project_id
    git(repository, "remote", "add", "origin", f"https://github.com/{repository_identity}.git")
    data_root = tmp_path / "data"
    data_root.mkdir()
    settings = Settings(_env_file=None, data_root=data_root, web_origin="http://127.0.0.1:3000")
    store = FilesystemArtifactStore(settings.artifact_root)
    service = PlanningService(
        case.gateway,
        store,
        PromptLoader(tmp_path / "prompts"),
        lambda policy: RepositoryReader(
            policy.repository_path,
            secret_paths=policy.effective_secret_paths,
            force_python_search=True,
        ),
    )
    async with PostgresUnitOfWork(factory) as work:
        outcome = await service.execute(case.command, work)
    assert outcome.run_state.value == "AWAITING_PLAN_APPROVAL"
    async with factory() as session:
        run = await session.get(Run, case.run_id)
        version, digest = run.version, run.pending_evidence_digest
    if drift in {"legacy_evidence", "task_digest", "runner", "budget", "plan_digest"}:
        document = json.loads(await store.open_bytes(digest))
        if drift == "legacy_evidence":
            del document["task_digest"]
        elif drift == "runner":
            document["runner_mode"] = "trusted_host"
        elif drift == "budget":
            document["cost_budget_minor"] += 1
        elif drift == "plan_digest":
            document["plan_digest"] = "b" * 64
        else:
            document["task_digest"] = "b" * 64
        changed_bytes = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
        descriptor = await store.put_bytes(changed_bytes, media_type="application/json")
        async with PostgresUnitOfWork(factory) as work:
            await work.artifacts.record(
                descriptor,
                run_id=case.run_id,
                producer_type="plan_approval_evidence",
                producer_id=uuid4(),
            )
            await work.commit()
        async with factory() as session, session.begin():
            run = await session.get(Run, case.run_id)
            run.pending_evidence_digest = descriptor.digest
        digest = descriptor.digest
    app = create_app(settings, session_factory=factory)
    bootstrap = await app.state.auth_service.issue_bootstrap()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url=settings.web_origin,
        headers={"Origin": settings.web_origin},
    ) as client:
        auth = await post(client, "/api/auth/bootstrap", {"token": bootstrap}, 200)
        client.headers["X-CSRF-Token"] = auth["csrf_token"]
        challenge = await post(
            client,
            f"/api/runs/{case.run_id}/approval-challenges",
            {"gate": "plan", "run_version": version, "evidence_digest": digest},
            200,
        )
        if drift == "base":
            git(repository, "commit", "--allow-empty", "-m", "base changed")
        elif drift == "run_base":
            async with factory() as session, session.begin():
                run = await session.get(Run, case.run_id)
                run.base_sha = "b" * 40
        elif drift == "policy":
            async with factory() as session, session.begin():
                old = await session.get(ProjectPolicyVersion, (project_id, 1))
                document = dict(old.document)
                document["version"] = 2
                document["local_remediation_limit"] = 1
                policy_digest = hashlib.sha256(
                    json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                session.add(
                    ProjectPolicyVersion(
                        project_id=project_id,
                        version=2,
                        policy_digest=policy_digest,
                        document_schema_version=1,
                        document=document,
                    )
                )
                await session.flush()
                project = await session.get(Project, project_id)
                project.current_policy_version = 2
        response = await client.post(
            f"/api/runs/{case.run_id}/approvals",
            json={
                "gate": "plan",
                "run_version": version + (1 if drift == "expected_version" else 0),
                "evidence_digest": digest,
                "challenge_token": challenge["token"],
            },
        )
        assert response.status_code == (202 if drift == "none" else 409), response.text
    async with factory() as session:
        approvals = (
            await session.scalars(select(Approval).where(Approval.run_id == case.run_id))
        ).all()
        commands = (
            await session.scalars(
                select(RunCommand).where(
                    RunCommand.run_id == case.run_id,
                    RunCommand.command_type == "approve_plan",
                )
            )
        ).all()
        assert len(approvals) == len(commands) == (1 if drift == "none" else 0)
        run = await session.get(Run, case.run_id)
        assert run.state == "AWAITING_PLAN_APPROVAL" and run.version == version
