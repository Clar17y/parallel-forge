"""Authenticated report filtering over durable no-policy usage in separate processes."""

import asyncio
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from alembic import command
from forge.domain.subscription import TaskBudget, decode_subscription_record
from forge.domain.task_usage_contract import RouteCohort, RouteMember
from forge.persistence.database import create_engine
from sqlalchemy import text

from tests.acceptance.process_harness import ForgeProcessHarness

pytestmark = pytest.mark.integration
pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


async def _seed_no_policy_samples(url: str) -> tuple[UUID, UUID]:
    first, second = uuid4(), uuid4()
    route_digest = RouteCohort.from_members((RouteMember(
        role="developer", provider="fixture", model="fixture", ordinal=0,
    ),)).fingerprint
    engine = create_engine(url)
    try:
        async with engine.begin() as db:
            for project in (first, second):
                await db.execute(text(
                    "INSERT INTO projects (id, canonical_path, github_repository, default_branch) "
                    "VALUES (:id, :path, :repo, 'main')"
                ), {"id": project, "path": f"/tmp/{project}", "repo": f"owner/{project}"})
                await db.execute(text(
                    "INSERT INTO project_policy_versions (project_id, version, policy_digest, document_schema_version, document) "
                    "VALUES (:id, 1, :digest, 1, '{}'::jsonb)"
                ), {"id": project, "digest": "a" * 64})
            for project, counts in ((first, (10, 5)), (first, (0, 0)), (second, None)):
                task, run, execution, unit = (uuid4() for _ in range(4))
                await db.execute(text(
                    "INSERT INTO tasks (id, project_id, normalized_text, task_digest) "
                    "VALUES (:task, :project, 'report fixture', :digest)"
                ), {"task": task, "project": project, "digest": "b" * 64})
                await db.execute(text(
                    "INSERT INTO runs (id, project_id, task_id, policy_version, state, version, "
                    "local_remediation_count, remote_remediation_count, token_budget, cost_budget_minor, "
                    "duration_budget_seconds, database_state) VALUES "
                    "(:run, :project, :task, 1, 'COMPLETED', 0, 0, 0, 0, 0, 0, 'DISABLED')"
                ), {"run": run, "project": project, "task": task})
                await db.execute(text(
                    "INSERT INTO agent_executions (id, run_id, role, instruction_version, provider, model, status) "
                    "VALUES (:execution, :run, 'developer', 'v1', 'fixture', 'fixture', 'SUCCEEDED')"
                ), {"execution": execution, "run": run})
                await db.execute(text(
                    "INSERT INTO task_usage_work_units (id, project_id, subject_kind, subject_id, run_id, "
                    "admitted_phase_id, configured_route_digest, configured_route_members, "
                    "effective_route_digest, effective_route_members, source_kind, source_id, agent_execution_id, schema_version) "
                    "VALUES (:unit, :project, 'run', :run, :run, 'implement', :digest, CAST(:members AS jsonb), "
                    ":digest, CAST(:members AS jsonb), 'agent_execution', :execution, :execution, 1)"
                ), {"unit": unit, "project": project, "run": run, "execution": execution,
                    "digest": route_digest, "members": '[{"role":"developer","provider":"fixture","model":"fixture","ordinal":0}]'})
                if counts is not None:
                    await db.execute(text(
                        "INSERT INTO task_usage_observations (id, project_id, work_unit_id, event_id, sequence, "
                        "counter_kind, scope, input_tokens, output_tokens, final, final_status, observed_at, schema_version) "
                        "VALUES (:id, :project, :unit, :event, 1, 'cumulative', 'source', :input, :output, "
                        "true, 'succeeded', now(), 1)"
                    ), {"id": uuid4(), "project": project, "unit": unit, "event": str(uuid4()),
                        "input": counts[0], "output": counts[1]})
                    await db.execute(text(
                        "INSERT INTO model_usage (id, run_id, agent_execution_id, provider, model, "
                        "prompt_version, input_tokens, output_tokens, cached_input_tokens, "
                        "input_tokens_complete, output_tokens_complete, cached_input_tokens_complete, "
                        "duration_ms, tool_call_count, pricing_version, currency, estimated_cost_minor) "
                        "VALUES (:id, :run, :execution, 'fixture', 'fixture', 'v1', :input, :output, 0, "
                        "true, true, true, 1, 0, 'fixture', 'USD', 0)"
                    ), {"id": uuid4(), "run": run, "execution": execution,
                        "input": counts[0], "output": counts[1]})
    finally:
        await engine.dispose()
    return first, second


def test_no_policy_report_scopes_two_owner_accessible_projects_and_unknown_id(
    test_database_url: str, alembic_config_factory, tmp_path: Path,
) -> None:
    command.upgrade(alembic_config_factory(test_database_url), "head")
    first, second = asyncio.run(_seed_no_policy_samples(test_database_url))
    data_root = tmp_path / "no-policy-report"
    data_root.mkdir()
    with ForgeProcessHarness(database_url=test_database_url, data_root=data_root,
                             prompt_root=Path.cwd() / "agents", subscription_only=True) as harness:
        harness.start_api()
        harness.start_worker()
        harness.assert_process_identities()
        route = "/api/task-usage-report"
        assert httpx.get(harness.base_url + route, timeout=5).status_code == 401
        with httpx.Client(base_url=harness.base_url, headers={"Origin": harness.base_url}, timeout=5) as client:
            login = client.post("/api/auth/bootstrap", json={"token": harness.bootstrap_token()},
                                headers={"Idempotency-Key": str(uuid4())})
            login.raise_for_status()
            all_projects = client.get(route)
            all_projects.raise_for_status()
            assert set(all_projects.json()["filters"]["project_ids"]) == {str(first), str(second)}
            assert all_projects.json()["overview"]["eligible_count"] == 2, all_projects.json()
            assert all_projects.json()["overview"]["sample_count"] == 2
            assert all_projects.json()["overview"]["mean_tokens"] == 7.5
            assert all_projects.json()["overview"]["reference_tokens"] is None
            assert all_projects.json()["overview"]["excluded_count"] >= 1
            assert all_projects.json()["by_project"][str(second)]["sample_count"] == 0
            selected = client.get(route, params={"project_id": str(first), "subject_kind": "run",
                                                 "provider": "fixture", "model": "fixture"})
            selected.raise_for_status()
            assert selected.json()["filters"]["project_ids"] == [str(first)]
            assert selected.json()["overview"]["eligible_count"] == 2
            assert selected.json()["overview"]["sample_count"] == 2
            empty = client.get(route, params={"project_id": str(uuid4())})
            empty.raise_for_status()
            assert empty.json()["overview"]["eligible_count"] == 0
            policy_route = f"/api/projects/{first}/monitoring-policy"
            assert client.get(policy_route).json() is None
            client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
            current = 0
            for cap in (9007199254740991, 9007199254740992,
                        9007199254740993, 9223372036854775807):
                saved = client.post(policy_route, json={
                    "expected_revision": current, "hard_token_cap": cap,
                    "manual_estimate_tokens": cap,
                }, headers={"Idempotency-Key": str(uuid4())})
                saved.raise_for_status()
                current += 1
                assert saved.json()["revision"] == current
                assert saved.json()["hard_token_cap"] == cap
                assert saved.json()["manual_estimate_tokens"] == cap
                assert client.get(policy_route).json()["hard_token_cap"] == cap
            for invalid in (-1, 1.5, 9223372036854775808):
                denied = client.post(policy_route, json={
                    "expected_revision": current, "hard_token_cap": invalid,
                }, headers={"Idempotency-Key": str(uuid4())})
                assert denied.status_code == 422
            cleared = client.post(policy_route, json={
                "expected_revision": current, "hard_token_cap": None,
                "manual_estimate_tokens": None,
                "history_limit": 9223372036854775807,
                "minimum_comparables": 9223372036854775807,
                "window_days": 9223372036854775807,
            }, headers={"Idempotency-Key": str(uuid4())})
            cleared.raise_for_status()
            assert cleared.json()["hard_token_cap"] is None
            assert cleared.json()["manual_estimate_tokens"] is None
            assert cleared.json()["window_days"] == 9223372036854775807
            assert client.get(policy_route).json()["minimum_comparables"] == 9223372036854775807
            wide_report = client.get(route, params={
                "project_id": str(first), "window_days": 9223372036854775807,
            })
            wide_report.raise_for_status()
            assert wide_report.json()["overview"]["sample_count"] == 2

            epic = client.post("/api/epics", json={
                "project_id": str(first), "title": "Exact epic caps",
            }, headers={"Idempotency-Key": str(uuid4())})
            epic.raise_for_status()
            epic_id = epic.json()["epic_id"]
            budget_route = f"/api/epics/{epic_id}/budget"
            initial = client.get(budget_route)
            initial.raise_for_status()
            assert initial.json()["ceiling"]["max_input_tokens"] is None
            version = initial.json()["version"]

            async def saved_epic_cap() -> int | None:
                engine = create_engine(test_database_url)
                try:
                    async with engine.connect() as db:
                        raw = await db.scalar(text(
                            "SELECT ceiling "
                            "FROM epic_brainstorm_budget_ledgers WHERE epic_id = :epic"
                        ), {"epic": UUID(epic_id)})
                        decoded = decode_subscription_record(raw)
                        assert isinstance(decoded, TaskBudget)
                        return decoded.max_input_tokens
                finally:
                    await engine.dispose()

            for cap in (9007199254740991, 9007199254740992,
                        9007199254740993, 9223372036854775807):
                updated = client.put(budget_route, json={
                    "expected_version": version,
                    "ceiling": {"max_input_tokens": cap, "max_output_tokens": cap},
                    "note": None,
                }, headers={"Idempotency-Key": str(uuid4())})
                updated.raise_for_status()
                version = updated.json()["version"]
                assert updated.json()["ceiling"]["max_input_tokens"] == cap
                assert client.get(budget_route).json()["ceiling"]["max_output_tokens"] == cap
                assert asyncio.run(saved_epic_cap()) == cap
            for invalid in (-1, 1.5):
                denied_budget = client.put(budget_route, json={
                    "expected_version": version,
                    "ceiling": {"max_input_tokens": invalid},
                }, headers={"Idempotency-Key": str(uuid4())})
                assert denied_budget.status_code == 422
            cleared_budget = client.put(budget_route, json={
                "expected_version": version, "ceiling": {
                    "max_input_tokens": None, "max_output_tokens": None,
                },
            }, headers={"Idempotency-Key": str(uuid4())})
            cleared_budget.raise_for_status()
            assert cleared_budget.json()["ceiling"]["max_input_tokens"] is None

        async def stored_epic_caps() -> list[tuple[int, int | None, int | None]]:
            engine = create_engine(test_database_url)
            try:
                async with engine.connect() as db:
                    rows = (await db.execute(text(
                        "SELECT version, ceiling "
                        "FROM epic_brainstorm_budget_ledgers WHERE epic_id = :epic"
                    ), {"epic": UUID(epic_id)})).all()
                    budgets = [(version, decode_subscription_record(raw)) for version, raw in rows]
                    assert all(isinstance(budget, TaskBudget) for _, budget in budgets)
                    return [(version, budget.max_input_tokens, budget.max_output_tokens)
                            for version, budget in budgets if isinstance(budget, TaskBudget)]
            finally:
                await engine.dispose()

        assert asyncio.run(stored_epic_caps()) == [(version + 1, None, None)]

        async def stored_caps() -> list[tuple[int, int | None, int | None]]:
            engine = create_engine(test_database_url)
            try:
                async with engine.connect() as db:
                    return [tuple(row) for row in (await db.execute(text(
                        "SELECT revision, hard_token_cap, manual_estimate_tokens "
                        "FROM task_usage_policy_revisions WHERE project_id = :project ORDER BY revision"
                    ), {"project": first})).all()]
            finally:
                await engine.dispose()

        assert asyncio.run(stored_caps()) == [
            (1, 9007199254740991, 9007199254740991),
            (2, 9007199254740992, 9007199254740992),
            (3, 9007199254740993, 9007199254740993),
            (4, 9223372036854775807, 9223372036854775807),
            (5, None, None),
        ]
