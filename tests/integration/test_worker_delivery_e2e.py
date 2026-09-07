"""HTTP approval through the production worker, real managed Git and named checks."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from forge.api.app import create_app
from forge.application.services.worker import Worker
from forge.domain.actor import AgentRole
from forge.domain.agent import (
    AgentFinishStatus,
    AgentResult,
    DeveloperOutput,
    ReviewDecision,
    ReviewOutput,
)
from forge.domain.policy import RunnerMode
from forge.observability.usage import UsageRecord
from forge.persistence.models import Run, RunEvent
from forge.persistence.repositories.commands import PostgresCommandRepository
from forge.settings import Settings
from forge.worker import composition
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from test_worker_planning_e2e import RecordingPlanner, git, post, tick_success
from test_worker_planning_e2e import (
    workflow_session_factory as workflow_session_factory,  # noqa: PLC0414
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


@pytest.mark.parametrize("repair_failed_check", [False, True])
async def test_http_approved_delivery_reaches_pr_gate_with_real_worktree_and_check(
    tmp_path, workflow_session_factory, monkeypatch, repair_failed_check
):
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / ".gitignore").write_text(".worktrees/\n", encoding="utf-8")
    (repository / "README.md").write_text("Original README\n", encoding="utf-8")
    (repository / "check_readme.py").write_text(
        "from pathlib import Path\nassert Path('README.md').read_text() == 'Verified delivery\\n'\n",
        encoding="utf-8",
    )
    git(repository, "init", "-b", "main")
    git(repository, "config", "user.name", "Forge fixture")
    git(repository, "config", "user.email", "fixture@example.invalid")
    git(repository, "add", ".gitignore", "README.md", "check_readme.py")
    git(repository, "commit", "-m", "initial")
    git(repository, "remote", "add", "origin", "https://github.com/example/fixture.git")
    base = git(repository, "rev-parse", "HEAD")
    data_root = tmp_path / "data"
    data_root.mkdir()
    catalog = tmp_path / "pricing.json"
    catalog.write_text(
        json.dumps(
            {
                "version": "fixture-v1",
                "entries": {
                    "google:gemini-2.5-pro": {
                        "input_per_million": "1",
                        "output_per_million": "1",
                        "cached_input_per_million": "1",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    settings = Settings(
        _env_file=None,
        data_root=data_root,
        prompt_root=Path(__file__).resolve().parents[2] / "agents",
        provider_secret_reference="secret://forge/gemini-api-key",
        pricing_catalog_path=catalog,
    )
    roles = []
    planner = RecordingPlanner()

    class ScriptedProvider:
        def __init__(self, **kwargs):
            self.provider = kwargs["tool_provider"]

        async def execute(self, request):
            roles.append(request.role)
            tools = {tool.name: tool.func for tool in self.provider.tools_for(request).tools}
            if request.role is AgentRole.PLANNER:
                return await planner.execute(request)

            async def call(name, **kwargs):
                result = await tools[name](
                    tool_context=SimpleNamespace(
                        invocation_id=str(request.execution_id), function_call_id=name
                    ),
                    **kwargs,
                )
                if result["status"] != "succeeded":
                    raise AssertionError(json.dumps(result, sort_keys=True))
                return result["metadata"]

            if request.role is AgentRole.DEVELOPER:
                needs_repair = repair_failed_check and roles.count(AgentRole.DEVELOPER) == 1
                await call(
                    "repository.write_file",
                    path="README.md",
                    content="Repair needed\n" if needs_repair else "Verified delivery\n",
                )
                await call("git.commit", message="Clarify README")
                candidate = await call("git.diff", scope="candidate")
                output = DeveloperOutput(
                    summary="Updated README",
                    changed_paths=tuple(candidate["changed_paths"]),
                    tests_added_or_changed=(),
                    named_checks_run=(),
                    local_commit_sha=candidate["head_sha"],
                    diff_digest=candidate["diff_digest"],
                    unresolved_concerns=(),
                    plan_deviations=(),
                )
                count = 3
            else:
                await call("validation-results.read")
                output = ReviewOutput(
                    decision=ReviewDecision.APPROVE,
                    findings=(),
                    tested_claims=("README and controller check",),
                    missing_evidence=(),
                    summary="Verified",
                )
                count = 1
            return AgentResult(
                execution_id=request.execution_id,
                role=request.role,
                finish_status=AgentFinishStatus.SUCCEEDED,
                provider=request.provider,
                model=request.model,
                instruction_digest=request.instruction_digest,
                output=output,
                tool_call_count=count,
                duration_ms=1,
                usage=UsageRecord(
                    provider=request.provider,
                    model=request.model,
                    prompt_version=request.instruction_version,
                    tool_call_count=count,
                    duration_ms=1,
                    estimated_cost_minor=0,
                    pricing_version="fixture-v1",
                    currency="USD",
                ),
            )

    monkeypatch.setattr(composition, "GoogleAdkGateway", ScriptedProvider)
    factory = workflow_session_factory
    app = create_app(settings, session_factory=factory)
    bootstrap = await app.state.auth_service.issue_bootstrap()
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
                "name": "Delivery fixture",
                "repository_path": str(repository),
                "github_repository": "example/fixture",
                "default_branch": "main",
                "runner_mode": RunnerMode.TRUSTED_HOST.value,
                "trusted_project": True,
                "commands": [
                    {
                        "kind": "test",
                        "name": "unit",
                        "timeout_seconds": 30,
                        "argv": [sys.executable, "check_readme.py"],
                    }
                ],
            },
        )
        task = await post(
            client,
            "/api/tasks",
            {"project_id": project["id"], "title": "Update README", "body": "Clarify the README."},
        )
        run_id = UUID((await post(client, "/api/runs", {"task_id": task["id"]}))["id"])
        worker = Worker(
            PostgresCommandRepository(factory),
            factory,
            handlers=composition.compose_worker_handlers(settings, factory),
            worker_id="delivery-e2e",
        )
        await tick_success(worker, factory, run_id)
        async with factory() as session:
            run = await session.get(Run, run_id)
            version, digest = run.version, run.pending_evidence_digest
        challenge = await post(
            client,
            f"/api/runs/{run_id}/approval-challenges",
            {"gate": "plan", "run_version": version, "evidence_digest": digest},
            200,
        )
        await post(
            client,
            f"/api/runs/{run_id}/approvals",
            {
                "gate": "plan",
                "run_version": version,
                "evidence_digest": digest,
                "challenge_token": challenge["token"],
            },
            202,
        )
        # A failed check adds one remediation execution and fresh validation.
        for _ in range(7 if repair_failed_check else 5):
            await tick_success(worker, factory, run_id)
        assert await worker.tick() is None
    async with factory() as session:
        run = await session.get(Run, run_id)
        assert run.state == "AWAITING_PR_APPROVAL"
        assert run.pending_evidence_digest
        assert run.local_remediation_count == int(repair_failed_check)
        assert Path(run.worktree_path, "README.md").read_text() == "Verified delivery\n"
        assert git(Path(run.worktree_path), "rev-parse", "HEAD") != base
        audit = (
            await session.scalars(
                select(RunEvent)
                .where(
                    RunEvent.run_id == run_id,
                    RunEvent.event_type.like("runner.trusted_host.%"),
                )
                .order_by(RunEvent.sequence)
            )
        ).all()
        assert [event.event_type for event in audit] == [
            "runner.trusted_host.attempt",
            "runner.trusted_host.completed",
        ] * (2 if repair_failed_check else 1)
        assert all(
            event.actor_class == "worker"
            and event.payload["priority"] == "high"
            and event.payload["command_name"] == "unit"
            and event.payload["unsandboxed"] is True
            for event in audit
        )
    expected_roles = [AgentRole.PLANNER, AgentRole.DEVELOPER]
    if repair_failed_check:
        expected_roles.append(AgentRole.DEVELOPER)
    assert roles == [*expected_roles, AgentRole.REVIEWER]
    assert git(repository, "rev-parse", "HEAD") == base
