"""Actual PostgreSQL list data is exposed only through authenticated typed routes."""

from dataclasses import replace
from uuid import uuid4

import pytest
from forge.api.app import create_app
from forge.application.services.projects import _digest
from forge.domain.approval import ApprovalGate
from forge.domain.policy import ProjectPolicy
from forge.domain.run import RunSnapshot, RunState
from forge.persistence.models import (
    AgentExecution,
    EvaluationCase,
    EvaluationSuite,
    ModelUsage,
    OperatorAuditEvent,
    Project,
    ProjectPolicyVersion,
    Run,
    Task,
)
from forge.persistence.unit_of_work import PostgresUnitOfWork
from forge.settings import Settings
from httpx import ASGITransport, AsyncClient

pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)
pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_lists_read_persisted_data_with_auth_bounds_and_typed_contract(
    session_factory, tmp_path
):
    project_id, task_id, run_id, execution_id, suite_id = [uuid4() for _ in range(5)]
    policy = ProjectPolicy(
        id=project_id,
        version=1,
        repository_path=str(tmp_path),
        github_repository="owner/repo",
        default_branch="main",
    )
    async with session_factory() as session, session.begin():
        project = Project(
            id=project_id,
            canonical_path=str(tmp_path),
            github_repository="owner/repo",
            default_branch="main",
        )
        doc = policy.model_dump(mode="json")
        session.add_all(
            [
                project,
                ProjectPolicyVersion(
                    project_id=project_id,
                    version=1,
                    document=doc,
                    document_schema_version=1,
                    policy_digest=_digest(doc),
                ),
                Task(
                    id=task_id,
                    project_id=project_id,
                    normalized_text="list task",
                    task_digest="b" * 64,
                ),
            ]
        )
        await session.flush()
        project.current_policy_version = 1
    run = RunSnapshot(
        id=run_id,
        project_id=project_id,
        task_id=task_id,
        policy_version=1,
        state=RunState.AWAITING_PLAN_APPROVAL,
        pending_gate=ApprovalGate.PLAN,
        pending_evidence_digest="c" * 64,
    )
    async with PostgresUnitOfWork(session_factory) as work:
        await work.runs.create(
            replace(run, state=RunState.CREATED, pending_gate=None, pending_evidence_digest=None)
        )
        await work.commit()
    async with session_factory() as session, session.begin():
        row = await session.get(Run, run_id)
        row.state = run.state.value
        row.pending_gate = run.pending_gate.value
        row.pending_evidence_digest = run.pending_evidence_digest
        session.add(
            AgentExecution(
                id=execution_id,
                run_id=run_id,
                role="planner",
                instruction_version="v1",
                provider="fixture",
                model="test-model",
                status="SUCCEEDED",
            )
        )
        session.add(
            EvaluationSuite(
                id=suite_id,
                name="deterministic",
                fixture_version="f1",
                metric_version="m1",
                status="running",
                idempotency_key="suite-1",
            )
        )
        session.add(
            EvaluationSuite(
                name="empty",
                fixture_version="f1",
                metric_version="m1",
                status="pending",
                idempotency_key="suite-2",
            )
        )
        await session.flush()
        session.add(
            EvaluationCase(
                suite_id=suite_id,
                case_key="case-1",
                fixture_version="f1",
                metric_version="m1",
                role="planner",
                status="pending",
                metrics={
                    "score": 1,
                    "hidden_reasoning": "private-value",
                    "secret_reference": "credential-ref",
                },
            )
        )
        for currency, cost in (("USD", 7), ("USD", None), ("GBP", 3)):
            session.add(
                ModelUsage(
                    run_id=run_id,
                    agent_execution_id=execution_id,
                    provider="fixture",
                    model="test-model",
                    prompt_version="v1",
                    input_tokens=10,
                    output_tokens=2,
                    duration_ms=1,
                    pricing_version="fixture-v1",
                    estimated_cost_minor=cost,
                    currency=currency,
                    unknown_price_reason="unknown" if cost is None else None,
                )
            )
        for index in range(2):
            session.add(
                OperatorAuditEvent(
                    actor_id=uuid4(),
                    event_type=f"fixture.{index}",
                    subject_type="run",
                    subject_id=run_id,
                    payload={"safe": index, "hidden_reasoning": "private-value"},
                )
            )
    app = create_app(Settings(data_root=tmp_path / "data"), session_factory=session_factory)
    paths = [
        "approvals",
        "audit",
        "usage",
        "agents",
        "tool-permissions",
        "evaluations",
        f"projects/{project_id}/policy-projection",
    ]
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://127.0.0.1:3000"
    ) as client:
        for path in paths:
            assert (await client.get(f"/api/{path}")).status_code == 401
        token = await app.state.auth_service.issue_bootstrap()
        assert (
            await client.post(
                "/api/auth/bootstrap",
                json={"token": token},
                headers={"Origin": "http://127.0.0.1:3000"},
            )
        ).status_code == 200
        data = {}
        for path in paths:
            response = await client.get(f"/api/{path}")
            assert response.status_code == 200, (path, response.text)
            assert "private-value" not in response.text
            assert "credential-ref" not in response.text
            data[path] = response.json()
        assert data["approvals"]["items"] == [
            {
                "run_id": str(run_id),
                "task_id": str(task_id),
                "gate": "plan",
                "evidence_digest": "c" * 64,
                "run_version": run.version,
                "policy_version": 1,
            }
        ]
        usage = {row["currency"]: row for row in data["usage"]["items"]}
        assert usage["USD"]["known_cost_minor"] == 7
        assert usage["USD"]["unpriced_calls"] == 1
        assert usage["USD"]["input_tokens"] == 20
        assert usage["GBP"]["known_cost_minor"] == 3
        assert data["agents"]["items"][0]["id"] == str(execution_id)
        assert all(item["source"] == "operator" for item in data["audit"]["items"])
        assert {row["role"] for row in data["tool-permissions"]["items"]} == {
            "planner",
            "developer",
            "reviewer",
        }
        assert all(row["tools"] for row in data["tool-permissions"]["items"])
        cases = data["evaluations"]["items"]
        assert len(cases) == 2 and any(row["case_id"] is None for row in cases)
        assert next(row for row in cases if row["case_id"])["metrics"] == {"score": 1}
        projected_policy = data[paths[-1]]
        assert projected_policy["database_enabled"] is False
        assert projected_policy["limits"]["local_remediation_limit"] == 3
        assert "secret_paths" not in projected_policy
        first = (await client.get("/api/evaluations?limit=1")).json()
        second = (await client.get("/api/evaluations?limit=1&offset=1")).json()
        assert first["truncated"] is True and second["truncated"] is False
        assert first["items"][0]["suite_id"] != second["items"][0]["suite_id"]
        assert (await client.get("/api/audit?offset=-1")).status_code == 422
        assert (await client.get("/api/audit?limit=101")).status_code == 422
        assert (await client.get(f"/api/projects/{uuid4()}/policy-projection")).status_code == 404
    schema = app.openapi()
    for path in paths:
        if path.startswith("projects/"):
            path = "projects/{project_id}/policy-projection"
        operation = schema["paths"][f"/api/{path}"]["get"]
        assert operation["security"] == [{"OperatorSession": []}]
        assert not any(p["name"] == "_method" for p in operation.get("parameters", []))
