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
    Approval,
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
                instruction_digest="d" * 64,
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
        evaluation_case = EvaluationCase(
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
        session.add(evaluation_case)
        usage_rows = []
        for provider, model, currency, cost, input_tokens, duration_ms in (
            ("fixture", "test-model", "USD", 7, 10, 1),
            ("fixture", "test-model", "USD", None, 10, 1),
            ("fixture", "test-model", "GBP", 3, 10, 1),
            ("other", "other-model", "USD", 5, 4, 4),
        ):
            usage_rows.append(
                ModelUsage(
                    run_id=run_id,
                    agent_execution_id=execution_id,
                    provider=provider,
                    model=model,
                    prompt_version="v1",
                    input_tokens=input_tokens,
                    output_tokens=2,
                    duration_ms=duration_ms,
                    pricing_version="fixture-v1",
                    estimated_cost_minor=cost,
                    currency=currency,
                    unknown_price_reason="unknown" if cost is None else None,
                )
            )
        session.add_all(usage_rows)
        await session.flush()
        evaluation_case.model_usage_id = usage_rows[0].id
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
    async with session_factory() as session, session.begin():
        from datetime import UTC, datetime

        for gate, invalidated in (("plan", False), ("pr", True)):
            session.add(
                Approval(
                    run_id=run_id,
                    gate=gate,
                    evidence_digest="e" * 64,
                    run_version=1,
                    policy_version=1,
                    authenticated_actor_id=uuid4(),
                    invalidated_at=datetime.now(UTC) if invalidated else None,
                    invalidation_reason="candidate changed" if invalidated else None,
                )
            )
    from forge.domain.event import RunEvent
    from forge.domain.operation import canonical_digest

    event_actor = uuid4()
    async with PostgresUnitOfWork(session_factory) as work:
        operation = await work.operations.begin(
            run_id=run_id,
            operation_type="fixture.effect",
            idempotency_key="audit-effect",
            request_payload={},
            request_digest=canonical_digest({}),
        )
        await work.events.append(
            RunEvent(
                run_id=run_id,
                run_version=0,
                event_type="tool.observed",
                actor_class="worker",
                actor_id=event_actor,
                payload={
                    "operation_intent_id": str(operation.id),
                    "hidden_reasoning": "private-value",
                },
            )
        )
        other_run = RunSnapshot(
            id=uuid4(), project_id=project_id, task_id=task_id, policy_version=1
        )
        await work.runs.create(other_run)
        foreign_operation = await work.operations.begin(
            run_id=other_run.id,
            operation_type="foreign.effect",
            idempotency_key="foreign-audit-effect",
            request_payload={},
            request_digest=canonical_digest({}),
        )
        await work.events.append(
            RunEvent(
                run_id=run_id,
                run_version=0,
                event_type="tool.unknown_reference",
                actor_class="worker",
                payload={
                    "operation_intent_id": "not-a-uuid",
                    "publication_intent_id": str(foreign_operation.id),
                },
            )
        )
        await work.commit()
    async with session_factory() as session, session.begin():
        for subject_type, subject_id in (("task", task_id), ("project", project_id)):
            session.add(
                OperatorAuditEvent(
                    actor_id=uuid4(),
                    subject_type=subject_type,
                    subject_id=subject_id,
                    event_type=f"fixture.{subject_type}",
                    payload={},
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
        assert (await client.get(f"/api/audit/{uuid4()}")).status_code == 401
        assert (await client.get(f"/api/audit/run-events/{uuid4()}")).status_code == 401
        assert (await client.get(f"/api/runs/{run_id}/usage")).status_code == 401
        assert (await client.get(f"/api/runs/{run_id}/approval-history")).status_code == 401
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
        assert (await client.get(f"/api/runs/{other_run.id}/usage")).json()["items"] == []
        approval_response = await client.get(f"/api/runs/{run_id}/approval-history?limit=1")
        assert approval_response.status_code == 200, approval_response.text
        approval_page = approval_response.json()
        assert approval_page["truncated"] and len(approval_page["items"]) == 1
        approval_next = (
            await client.get(f"/api/runs/{run_id}/approval-history?limit=1&offset=1")
        ).json()
        approval_records = approval_page["items"] + approval_next["items"]
        assert {item["gate"] for item in approval_records} == {"plan", "pr"}
        assert any(
            item["invalidated_at"] and item["invalidation_reason"] == "candidate changed"
            for item in approval_records
        )
        assert all(item["evidence_digest"] == "e" * 64 for item in approval_records)
        assert (await client.get(f"/api/runs/{other_run.id}/approval-history")).json()[
            "items"
        ] == []
        assert (await client.get(f"/api/runs/{uuid4()}/approval-history")).status_code == 404
        assert (
            await client.get(f"/api/runs/{run_id}/approval-history?limit=101")
        ).status_code == 422
        calls_response = await client.get(f"/api/runs/{run_id}/usage?limit=2")
        assert calls_response.status_code == 200, calls_response.text
        calls = calls_response.json()
        assert len(calls["items"]) == 2 and calls["truncated"]
        remaining = (await client.get(f"/api/runs/{run_id}/usage?offset=2&limit=2")).json()
        all_calls = calls["items"] + remaining["items"]
        assert len({item["id"] for item in all_calls}) == 4
        assert not remaining["truncated"]
        assert all(item["instruction_digest"] == "d" * 64 for item in all_calls)
        assert all(item["pricing_version"] == "fixture-v1" for item in all_calls)
        assert all(item["tool_call_count"] == 0 for item in all_calls)
        assert any(
            item["estimated_cost_minor"] is None and item["unknown_price_reason"] == "unknown"
            for item in all_calls
        )
        assert (await client.get(f"/api/runs/{uuid4()}/usage")).status_code == 404
        assert (await client.get(f"/api/runs/{run_id}/usage?limit=101")).status_code == 422
        async with session_factory() as session, session.begin():
            execution = await session.get(AgentExecution, execution_id)
            execution.instruction_digest = None
        legacy_calls = (await client.get(f"/api/runs/{run_id}/usage")).json()["items"]
        assert len(legacy_calls) == 4 and all(
            item["instruction_digest"] is None for item in legacy_calls
        )
        usage = {
            (row["project_id"], row["run_id"], row["provider"], row["model"], row["currency"]): row
            for row in data["usage"]["items"]
        }
        fixture_usage = usage[(str(project_id), str(run_id), "fixture", "test-model", "USD")]
        assert fixture_usage["known_cost_minor"] == 7
        assert fixture_usage["unpriced_calls"] == 1
        assert fixture_usage["input_tokens"] == 20
        assert fixture_usage["duration_ms"] == 2
        assert (
            usage[(str(project_id), str(run_id), "fixture", "test-model", "GBP")][
                "known_cost_minor"
            ]
            == 3
        )
        assert usage[(str(project_id), str(run_id), "other", "other-model", "USD")] == {
            "project_id": str(project_id),
            "run_id": str(run_id),
            "provider": "other",
            "model": "other-model",
            "currency": "USD",
            "input_tokens": 4,
            "output_tokens": 2,
            "duration_ms": 4,
            "known_cost_minor": 5,
            "unpriced_calls": 0,
            "model_calls": 1,
        }
        assert data["agents"]["items"][0]["id"] == str(execution_id)
        assert {item["source"] for item in data["audit"]["items"]} == {"operator", "run"}
        run_events = (await client.get("/api/audit", params={"run_id": str(run_id)})).json()[
            "items"
        ]
        assert any(item["event_type"] == "fixture.task" for item in run_events)
        assert all(item["event_type"] != "fixture.project" for item in run_events)
        project_events = (
            await client.get("/api/audit", params={"project_id": str(project_id)})
        ).json()["items"]
        assert {"fixture.task", "fixture.project"} <= {
            item["event_type"] for item in project_events
        }
        audit_row = next(item for item in data["audit"]["items"] if item["source"] == "operator")
        causal = next(
            item for item in data["audit"]["items"] if item["event_type"] == "tool.observed"
        )
        assert causal["run_id"] == str(run_id) and causal["project_id"] == str(project_id)
        assert causal["actor_class"] == "worker"
        unbound = next(
            item
            for item in data["audit"]["items"]
            if item["event_type"] == "tool.unknown_reference"
        )
        assert unbound["operations"] == []
        assert (
            await client.get(
                "/api/audit",
                params={"operation_id": str(foreign_operation.id), "run_id": str(run_id)},
            )
        ).json()["items"] == []
        pages = [
            (await client.get("/api/audit", params={"offset": offset, "limit": 1})).json()
            for offset in range(len(data["audit"]["items"]))
        ]
        assert [page["items"][0]["id"] for page in pages] == [
            item["id"] for item in data["audit"]["items"]
        ]
        assert pages[-1]["truncated"] is False
        assert (await client.get(f"/api/audit/run-events/{uuid4()}")).status_code == 404
        assert causal["operations"] == [
            {"id": str(operation.id), "kind": "fixture.effect", "status": "PENDING"}
        ]
        assert (await client.get(f"/api/audit/run-events/{causal['id']}")).json() == {
            key: value for key, value in causal.items() if key != "operations"
        }
        for key, value in {
            "run_id": run_id,
            "project_id": project_id,
            "actor_id": event_actor,
            "operation_id": operation.id,
            "operation_status": "PENDING",
        }.items():
            selected = await client.get("/api/audit", params={key: str(value)})
            assert selected.status_code == 200
            assert causal in selected.json()["items"]
        selected = await client.get(
            "/api/audit",
            params={
                "actor_id": str(event_actor),
                "operation_id": str(operation.id),
                "operation_status": "PENDING",
            },
        )
        assert selected.json()["items"] == [causal]
        for key in ("run_id", "project_id", "actor_id", "operation_id"):
            assert (await client.get("/api/audit", params={key: str(uuid4())})).json()[
                "items"
            ] == []
        assert (await client.get("/api/audit?operation_status=SUCCEEDED")).json()["items"] == []
        assert (await client.get("/api/audit?operation_status=made-up")).status_code == 422
        detail = await client.get(f"/api/audit/{audit_row['id']}")
        assert detail.status_code == 200
        assert detail.json() == {
            key: value for key, value in audit_row.items() if key != "operations"
        }
        assert (await client.get(f"/api/audit/{uuid4()}")).status_code == 404
        assert {row["role"] for row in data["tool-permissions"]["items"]} == {
            "planner",
            "developer",
            "reviewer",
        }
        assert all(row["tools"] for row in data["tool-permissions"]["items"])
        cases = data["evaluations"]["items"]
        assert len(cases) == 2 and any(row["case_id"] is None for row in cases)
        linked_case = next(row for row in cases if row["case_id"])
        assert linked_case["metrics"] == {"score": 1}
        assert linked_case["provider"] == "fixture"
        assert linked_case["model"] == "test-model"
        assert linked_case["prompt_version"] == "v1"
        assert linked_case["input_tokens"] == 10
        assert linked_case["output_tokens"] == 2
        assert linked_case["duration_ms"] == 1
        assert linked_case["currency"] == "USD"
        assert linked_case["estimated_cost_minor"] == 7
        empty_case = next(row for row in cases if row["case_id"] is None)
        assert empty_case["provider"] is None
        assert empty_case["prompt_version"] is None
        assert empty_case["estimated_cost_minor"] is None
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
        from forge.domain.operation import OperationOutcome

        async with PostgresUnitOfWork(session_factory) as work:
            claim = await work.operations.claim_for_recovery(
                operation.id, owner_id="audit-test", lease_seconds=30
            )
            assert claim.acquired
            await work.operations.complete(
                operation.id, OperationOutcome(payload={"observed": True}), owner_id="audit-test"
            )
            await work.commit()
        observed = (
            await client.get(
                "/api/audit", params={"actor_id": str(event_actor), "operation_status": "SUCCEEDED"}
            )
        ).json()["items"]
        assert observed[0]["operations"][0]["status"] == "SUCCEEDED"
        assert (await client.get(f"/api/audit/run-events/{causal['id']}")).json() == {
            key: value for key, value in causal.items() if key != "operations"
        }
    schema = app.openapi()
    for path in paths:
        if path.startswith("projects/"):
            path = "projects/{project_id}/policy-projection"
        operation = schema["paths"][f"/api/{path}"]["get"]
        assert operation["security"] == [{"OperatorSession": []}]
        assert not any(p["name"] == "_method" for p in operation.get("parameters", []))
