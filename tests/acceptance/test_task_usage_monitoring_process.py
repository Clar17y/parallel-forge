"""Checkpoint settlement and authenticated continuation across real API/worker processes."""

import asyncio
import subprocess
from pathlib import Path
from time import monotonic, sleep
from uuid import uuid4

import httpx
import pytest
from alembic import command
from forge.domain.task_usage_contract import RouteCohort, RouteMember
from forge.persistence.database import create_engine
from sqlalchemy import text

from tests.acceptance.process_harness import ForgeProcessHarness
from tests.acceptance.test_subscription_profile_process import _repository

pytestmark = pytest.mark.integration
pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


async def _seed_checkpoint(database_url: str) -> tuple[str, str]:
    project, task, run, execution, unit, checkpoint, actor = (uuid4() for _ in range(7))
    members = '[{"role":"developer","provider":"fixture","model":"fixture","ordinal":0}]'
    digest = RouteCohort.from_members(
        (RouteMember(role="developer", provider="fixture", model="fixture", ordinal=0),)
    ).fingerprint
    engine = create_engine(database_url)
    try:
        async with engine.begin() as db:
            await db.execute(text("INSERT INTO projects (id, canonical_path, github_repository, default_branch) VALUES (:id, :path, :repo, 'main')"), {"id": project, "path": f"/tmp/{project}", "repo": f"owner/{project}"})
            await db.execute(text("INSERT INTO project_policy_versions (project_id, version, policy_digest, document_schema_version, document) VALUES (:id, 1, :digest, 1, '{}'::jsonb)"), {"id": project, "digest": digest})
            await db.execute(text("INSERT INTO tasks (id, project_id, normalized_text, task_digest) VALUES (:task, :project, 'checkpoint process', :digest)"), {"task": task, "project": project, "digest": "b" * 64})
            await db.execute(text("INSERT INTO runs (id, project_id, task_id, policy_version, state, version, local_remediation_count, remote_remediation_count, token_budget, cost_budget_minor, duration_budget_seconds, database_state) VALUES (:run, :project, :task, 1, 'CREATED', 0, 0, 0, 0, 0, 0, 'DISABLED')"), {"run": run, "project": project, "task": task})
            await db.execute(text("INSERT INTO agent_executions (id, run_id, role, instruction_version, provider, model, status) VALUES (:execution, :run, 'developer', 'v1', 'fixture', 'fixture', 'SUCCEEDED')"), {"execution": execution, "run": run})
            await db.execute(text("INSERT INTO task_usage_work_units (id, project_id, subject_kind, subject_id, run_id, admitted_phase_id, configured_route_digest, configured_route_members, effective_route_digest, effective_route_members, source_kind, source_id, agent_execution_id, schema_version) VALUES (:unit, :project, 'run', :run, :run, 'implement', :digest, CAST(:members AS jsonb), :digest, CAST(:members AS jsonb), 'agent_execution', :execution, :execution, 1)"), {"unit": unit, "project": project, "run": run, "execution": execution, "digest": digest, "members": members})
            await db.execute(text("INSERT INTO task_usage_policy_revisions (project_id, revision, mode, warning_multiplier, checkpoint_multiplier, history_limit, minimum_comparables, actor_id) VALUES (:project, 1, 'checkpoint', 1.5, 2, 50, 20, :actor)"), {"project": project, "actor": actor})
            await db.execute(text("INSERT INTO task_usage_observations (id, project_id, work_unit_id, event_id, sequence, counter_kind, scope, input_tokens, context_occupied_tokens, context_capacity_tokens, compaction_count, final, final_status, observed_at, schema_version) VALUES (:id, :project, :unit, 'process-event', 1, 'cumulative', 'source', 100, 40, 100, 2, true, 'succeeded', now(), 1)"), {"id": uuid4(), "project": project, "unit": unit})
            await db.execute(text("INSERT INTO task_usage_baselines (work_unit_id, project_id, subject_kind, subject_id, policy_revision, history_digest, snapshot_digest, estimator_version, window_limit, minimum_comparables, cohort_digest, frozen_at, schema_version, sample_count, status, reference_tokens, warning_threshold_tokens, checkpoint_threshold_tokens, frozen_payload) VALUES (:unit, :project, 'run', :run, 1, :history, :snapshot, 'median_p90_v1', 50, 20, :cohort, now(), 1, 0, 'manual', 25, 37.5, 50, '{}'::jsonb)"), {"unit": unit, "project": project, "run": run, "history": "c" * 64, "snapshot": "d" * 64, "cohort": digest})
            await db.execute(text("INSERT INTO task_usage_checkpoints (id, project_id, work_unit_id, baseline_work_unit_id, subject_kind, subject_id, policy_revision, reference_digest, trigger_event_id, state, version, generation, process_settled) VALUES (:id, :project, :unit, :unit, 'run', :run, 1, :reference, 'process-event', 'checkpoint_requested', 0, 1, false)"), {"id": checkpoint, "project": project, "unit": unit, "run": run, "reference": "d" * 64})
    finally:
        await engine.dispose()
    return str(run), str(checkpoint)


async def _run_diagnostics(database_url: str, run_id: str) -> dict[str, object]:
    engine = create_engine(database_url)
    try:
        async with engine.connect() as db:
            commands = (await db.execute(text(
                "SELECT command_type, status FROM run_commands WHERE run_id = :run ORDER BY created_at"
            ), {"run": run_id})).all()
            tasks = (await db.execute(text(
                "SELECT state FROM subscription_tasks WHERE run_id = :run"
            ), {"run": run_id})).all()
            attempts = (await db.execute(text(
                "SELECT a.status, r.disposition, r.accepted, r.result_payload->>'failure' "
                "FROM subscription_attempts a LEFT JOIN subscription_attempt_results r "
                "ON r.attempt_id = a.id WHERE a.run_id = :run"
            ), {"run": run_id})).all()
            operations = (await db.execute(text(
                "SELECT operation_kind, status, last_error FROM operation_intents WHERE run_id = :run"
            ), {"run": run_id})).all()
            schedule = (await db.execute(text(
                "SELECT st.state, st.version, st.pause_requested, st.cancel_requested, "
                "ss.state, ss.owned_paths, ss.dependency_task_ids, ss.parent_task_id, "
                "ss.provider, ss.read_only, ss.lease_owner, ss.lease_generation, "
                "sa.task_version, sa.status "
                "FROM subscription_tasks st JOIN subscription_scheduled_tasks ss "
                "ON ss.run_id = st.run_id AND ss.task_id = st.id "
                "LEFT JOIN subscription_attempts sa ON sa.task_row_id = st.id "
                "WHERE st.run_id = :run ORDER BY sa.attempt_number"
            ), {"run": run_id})).all()
            scheduler = (await db.execute(text(
                "SELECT admitted, candidate_epoch, candidate_state, effective_run_limit "
                "FROM subscription_scheduler_runs WHERE run_id = :run"
            ), {"run": run_id})).all()
            effects = (await db.execute(text(
                "SELECT task_id, state, whole_worktree_exclusive "
                "FROM subscription_scheduled_effects WHERE run_id = :run"
            ), {"run": run_id})).all()
            attempt_guards = (await db.execute(text(
                "SELECT a.task_row_id, a.status, a.lease_owner IS NOT NULL, "
                "c.attempt_id IS NOT NULL, a.attempt_number "
                "FROM subscription_attempts a LEFT JOIN subscription_attempt_consumption c "
                "ON c.attempt_id = a.id WHERE a.run_id = :run ORDER BY a.created_at"
            ), {"run": run_id})).all()
            quota = (await db.execute(text(
                "SELECT provider, pool, blocked, probe_attempt_id IS NOT NULL "
                "FROM subscription_quota_pools"
            ))).all()
            run_guard = (await db.execute(text(
                "SELECT state, pending_gate, version FROM runs WHERE id = :run"
            ), {"run": run_id})).all()
            return {"commands": [tuple(row) for row in commands],
                    "tasks": [tuple(row) for row in tasks],
                    "attempts": [tuple(row) for row in attempts],
                    "operations": [tuple(row) for row in operations],
                    "schedule": [tuple(row) for row in schedule],
                    "scheduler": [tuple(row) for row in scheduler],
                    "effects": [tuple(row) for row in effects],
                    "attempt_guards": [tuple(row) for row in attempt_guards],
                    "quota": [tuple(row) for row in quota],
                    "run_guard": [tuple(row) for row in run_guard]}
    finally:
        await engine.dispose()


async def _pending_run_evidence(database_url: str, run_id: str) -> str:
    engine = create_engine(database_url)
    try:
        async with engine.connect() as db:
            value = await db.scalar(text(
                "SELECT pending_evidence_digest FROM runs WHERE id = :run"
            ), {"run": run_id})
            assert isinstance(value, str) and len(value) == 64
            return value
    finally:
        await engine.dispose()


async def _authoring_journey(database_url: str, job_id: str) -> dict[str, object]:
    engine = create_engine(database_url)
    try:
        async with engine.connect() as db:
            attempts = (await db.execute(text(
                "SELECT id, number, state, failure, process_started, process_settled "
                "FROM epic_brainstorm_attempts WHERE job_id = :job ORDER BY number"
            ), {"job": job_id})).all()
            units = (await db.execute(text(
                "SELECT source_id FROM task_usage_work_units "
                "WHERE subject_kind = 'authoring_job' AND subject_id = :job "
                "ORDER BY created_at"
            ), {"job": job_id})).all()
            observations = (await db.execute(text(
                "SELECT o.event_id, o.input_tokens, o.output_tokens "
                "FROM task_usage_observations o JOIN task_usage_work_units u "
                "ON u.id = o.work_unit_id "
                "WHERE u.subject_kind = 'authoring_job' AND u.subject_id = :job "
                "ORDER BY o.observed_at"
            ), {"job": job_id})).all()
            return {"attempts": [tuple(row) for row in attempts],
                    "units": [tuple(row) for row in units],
                    "observations": [tuple(row) for row in observations]}
    finally:
        await engine.dispose()


async def _delegated_journey(database_url: str, run_id: str) -> dict[str, object]:
    engine = create_engine(database_url)
    try:
        async with engine.connect() as db:
            attempts = (await db.execute(text(
                "SELECT a.id, a.task_row_id, a.attempt_number, a.status, "
                "c.charged->>'provider_attempts' "
                "FROM subscription_attempts a "
                "LEFT JOIN subscription_attempt_consumption c ON c.attempt_id = a.id "
                "WHERE a.run_id = :run ORDER BY a.created_at"
            ), {"run": run_id})).all()
            units = (await db.execute(text(
                "SELECT source_id, admitted_phase_id, effective_route_members "
                "FROM task_usage_work_units WHERE subject_kind = 'run' AND subject_id = :run "
                "ORDER BY created_at"
            ), {"run": run_id})).all()
            observations = (await db.execute(text(
                "SELECT u.source_id, o.event_id, o.input_tokens, o.output_tokens "
                "FROM task_usage_observations o JOIN task_usage_work_units u "
                "ON u.id = o.work_unit_id "
                "WHERE u.subject_kind = 'run' AND u.subject_id = :run "
                "ORDER BY o.observed_at"
            ), {"run": run_id})).all()
            repairs = await db.scalar(text(
                "SELECT count(*) FROM subscription_repair_debits d "
                "JOIN subscription_attempts a ON a.id = d.attempt_id WHERE a.run_id = :run"
            ), {"run": run_id})
            return {"attempts": [tuple(row) for row in attempts],
                    "units": [tuple(row) for row in units],
                    "observations": [tuple(row) for row in observations],
                    "repair_debits": repairs}
    finally:
        await engine.dispose()


def _repository_with_managed_worktrees(repository: Path) -> None:
    _repository(repository)
    (repository / ".gitignore").write_text(".worktrees/\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", ".gitignore"],
                   check=True, capture_output=True, timeout=15)
    subprocess.run(["git", "-C", str(repository), "-c", "user.name=Forge fixture",
                    "-c", "user.email=fixture@example.invalid", "commit", "-m", "Ignore managed worktrees"],
                   check=True, capture_output=True, timeout=15)


def test_checkpoint_reconciles_in_worker_and_continues_via_authenticated_api(
    test_database_url: str, alembic_config_factory, tmp_path: Path
) -> None:
    command.upgrade(alembic_config_factory(test_database_url), "head")
    run_id, checkpoint_id = asyncio.run(_seed_checkpoint(test_database_url))
    data_root = tmp_path / "keyless"
    data_root.mkdir()
    with ForgeProcessHarness(
        database_url=test_database_url,
        data_root=data_root,
        prompt_root=Path.cwd() / "agents",
        subscription_only=True,
    ) as harness:
        harness.start_api()
        harness.start_worker()
        harness.assert_process_identities()
        path = f"/api/runs/{run_id}/usage-checkpoint"
        assert httpx.get(harness.base_url + path, timeout=5).status_code == 401
        with httpx.Client(base_url=harness.base_url, headers={"Origin": harness.base_url}, timeout=5) as client:
            bootstrap = client.post(
                "/api/auth/bootstrap",
                json={"token": harness.bootstrap_token()},
                headers={"Idempotency-Key": str(uuid4())},
            )
            bootstrap.raise_for_status()
            client.headers["X-CSRF-Token"] = bootstrap.json()["csrf_token"]
            deadline = monotonic() + 15
            while True:
                response = client.get(path)
                assert response.status_code == 200, response.text
                checkpoint = response.json()["checkpoint"]
                if checkpoint["state"] == "paused":
                    break
                assert monotonic() < deadline, checkpoint
                sleep(0.1)
            assert checkpoint["process_settled"] is True
            assert response.json()["report"]["input_tokens"] == 100
            assert response.json()["report"]["context_occupied_tokens"] == 40
            assert response.json()["report"]["context_peak_tokens"] == 40
            assert response.json()["report"]["compaction_count"] == 2
            body = {
                "checkpoint_id": checkpoint_id,
                "expected_version": checkpoint["version"],
                "idempotency_key": "process-continue",
                "action": "continue",
                "continue_mode": "next_checkpoint",
            }
            forbidden = client.post(path + "/commands", json=body, headers={"X-CSRF-Token": "invalid"})
            assert forbidden.status_code == 403
            continued = client.post(path + "/commands", json=body)
            continued.raise_for_status()
            assert continued.json()["checkpoint"]["state"] == "resumed"
            assert continued.json()["checkpoint"]["next_checkpoint_threshold_tokens"] == 125
            replay = client.post(path + "/commands", json=body)
            replay.raise_for_status()
            assert replay.json()["receipt"]["replayed"] is True


def test_supervised_authoring_observation_requests_checkpoint_across_processes(
    test_database_url: str, alembic_config_factory, tmp_path: Path
) -> None:
    command.upgrade(alembic_config_factory(test_database_url), "head")
    repository = tmp_path / "repository"
    _repository_with_managed_worktrees(repository)
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True, timeout=15)
    data_root = tmp_path / "authoring-data"
    data_root.mkdir()
    with ForgeProcessHarness(
        database_url=test_database_url, data_root=data_root,
        prompt_root=Path.cwd() / "agents", bare_remote_path=bare,
        task_usage_process=True,
    ) as harness:
        harness.start_api()
        harness.start_worker()
        harness.assert_process_identities()
        with httpx.Client(base_url=harness.base_url, headers={"Origin": harness.base_url}, timeout=8) as client:
            def mutate(path: str, body: dict, method: str = "POST") -> dict:
                reply = client.request(method, path, json=body, headers={"Idempotency-Key": str(uuid4())})
                assert reply.status_code < 400, (path, reply.status_code, reply.text)
                return reply.json()

            session = mutate("/api/auth/bootstrap", {"token": harness.bootstrap_token()})
            client.headers["X-CSRF-Token"] = session["csrf_token"]
            project = mutate("/api/projects", {
                "name": "Observed authoring process", "repository_path": str(repository),
                "github_repository": "example/a8-profiles", "default_branch": "main",
                "commands": [{"kind": "test", "name": "unit", "argv": ["python", "--version"], "timeout_seconds": 30}],
            })
            project_id = project["id"]
            route = {
                "provider": "google", "client": "gemini_cli", "model": "gemini-3.8-flash",
                "effort": "medium", "auth_mode": "subscription", "billing_mode": "allowance_only",
            }
            profile = mutate("/api/subscription-profiles", {"preferences": [
                {"purpose": "primary", "preferred_route": route},
                {"purpose": "exploration", "preferred_route": route},
            ], "default_billing_mode": "allowance_only"})
            mutate(f"/api/projects/{project_id}/subscription-profile", {
                "profile_id": profile["profile_id"], "profile_version": 1,
            }, "PUT")
            policy = mutate(f"/api/projects/{project_id}/monitoring-policy", {
                "expected_revision": 1, "mode": "checkpoint", "manual_estimate_tokens": 20,
            })
            assert policy["revision"] == 2
            epic = mutate("/api/epics", {"project_id": project_id, "title": "Observed process"})
            epic_id = epic["epic_id"]
            conversation = mutate(f"/api/epics/{epic_id}/brainstorm-conversations", {
                "project_id": project_id, "text": "Investigate usage",
            })
            conversation_id = conversation["conversation_id"]
            turns = client.get(f"/api/epics/{epic_id}/brainstorm-conversations/{conversation_id}/turns", params={"project_id": project_id})
            turns.raise_for_status()
            receipt = mutate(f"/api/epics/{epic_id}/brainstorm-conversations/{conversation_id}/jobs", {
                "project_id": project_id, "prompt_turn_id": turns.json()[0]["turn_id"],
                "expected_epic_version": 1, "expected_conversation_version": conversation["version"],
            })
            job_id = receipt["job_id"]
            checkpoint_path = f"/api/epics/{epic_id}/brainstorm-jobs/{job_id}/usage-checkpoint"
            deadline = monotonic() + 20
            while True:
                response = client.get(checkpoint_path, params={"project_id": project_id})
                assert response.status_code == 200, response.text
                checkpoint = response.json()["checkpoint"]
                if checkpoint and checkpoint["state"] == "paused" and checkpoint["process_settled"]:
                    break
                assert monotonic() < deadline, (
                    None if checkpoint is None else {
                        "state": checkpoint["state"], "process_settled": checkpoint["process_settled"],
                    }, response.json()["report"], (data_root / "worker.log").read_text()[-1600:],
                )
                sleep(0.1)
            report = response.json()["report"]
            assert report["input_tokens"] == 60 and report["output_tokens"] == 40
            assert report["baseline"]["reference_tokens"] == 20
            command_body = {
                "checkpoint_id": checkpoint["checkpoint_id"], "expected_version": checkpoint["version"],
                "idempotency_key": "authoring-process-continue", "action": "continue",
                "continue_mode": "next_checkpoint",
            }
            continued = client.post(checkpoint_path + "/commands", params={"project_id": project_id}, json=command_body)
            assert continued.status_code == 200, continued.text
            assert continued.json()["checkpoint"]["state"] == "resumed"
            deadline = monotonic() + 20
            job_path = f"/api/epics/{epic_id}/brainstorm-jobs/{job_id}"
            while True:
                outcome = client.get(job_path, params={"project_id": project_id})
                outcome.raise_for_status()
                if outcome.json()["state"] == "proposed":
                    break
                assert monotonic() < deadline, (
                    outcome.json(), asyncio.run(_authoring_journey(test_database_url, job_id)),
                    (data_root / "worker.log").read_text()[-1800:],
                )
                sleep(0.1)
            journey = asyncio.run(_authoring_journey(test_database_url, job_id))
            attempts = journey["attempts"]
            assert len(attempts) == 2, journey
            assert [row[1] for row in attempts] == [1, 2]
            assert all(row[2] == "settled" and row[4] and row[5] for row in attempts)
            assert len(journey["units"]) == len(journey["observations"]) == 2
            assert len({row[0] for row in journey["observations"]}) == 2
            assert [(row[1], row[2]) for row in journey["observations"]] == [(60, 40), (10, 5)]
            final = client.get(checkpoint_path, params={"project_id": project_id})
            final.raise_for_status()
            assert final.json()["report"]["input_tokens"] == 70
            assert final.json()["report"]["output_tokens"] == 45
            assert final.json()["report"]["baseline"]["reference_tokens"] == 20
            assert outcome.json()["proposal"]["problem"] == "A resumed observed proposal"


def test_supervised_delegated_observation_requests_checkpoint_across_processes(
    test_database_url: str, alembic_config_factory, tmp_path: Path
) -> None:
    command.upgrade(alembic_config_factory(test_database_url), "head")
    repository = tmp_path / "repository"
    _repository_with_managed_worktrees(repository)
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True, timeout=15)
    data_root = tmp_path / "delegated-data"
    data_root.mkdir()
    with ForgeProcessHarness(
        database_url=test_database_url, data_root=data_root,
        prompt_root=Path.cwd() / "agents", bare_remote_path=bare,
        task_usage_process=True, task_usage_delegated=True,
    ) as harness:
        harness.start_api()
        harness.start_worker()
        harness.assert_process_identities()
        with httpx.Client(base_url=harness.base_url, headers={"Origin": harness.base_url}, timeout=8) as client:
            def mutate(path: str, body: dict, method: str = "POST") -> dict:
                reply = client.request(method, path, json=body, headers={"Idempotency-Key": str(uuid4())})
                assert reply.status_code < 400, (path, reply.status_code, reply.text)
                return reply.json()

            session = mutate("/api/auth/bootstrap", {"token": harness.bootstrap_token()})
            client.headers["X-CSRF-Token"] = session["csrf_token"]
            project = mutate("/api/projects", {
                "name": "Observed delegated process", "repository_path": str(repository),
                "github_repository": "example/a8-profiles", "default_branch": "main",
                "runner_mode": "trusted_host", "trusted_project": True,
                "commands": [{"kind": "test", "name": "unit", "argv": ["python", "--version"], "timeout_seconds": 30}],
            })
            project_id = project["id"]
            primary = {"provider": "openai", "client": "codex_app_server", "model": "gpt-6-astra",
                       "effort": "low", "auth_mode": "subscription", "billing_mode": "allowance_only"}
            child = {"provider": "google", "client": "gemini_cli", "model": "gemini-3.8-flash",
                     "effort": "medium", "auth_mode": "subscription", "billing_mode": "allowance_only"}
            profile = mutate("/api/subscription-profiles", {"preferences": [
                {"purpose": "primary", "preferred_route": primary},
                {"purpose": "routine_implementation", "preferred_route": child},
            ]})
            mutate(f"/api/projects/{project_id}/subscription-profile", {
                "profile_id": profile["profile_id"], "profile_version": 1,
            }, "PUT")
            policy = mutate(f"/api/projects/{project_id}/monitoring-policy", {
                "expected_revision": 1, "mode": "checkpoint", "manual_estimate_tokens": 20,
            })
            assert policy["revision"] == 2
            task = mutate("/api/tasks", {
                "project_id": project_id, "title": "Observed delegated README",
                "body": "Bounded README update with measured child usage",
            })
            run = mutate("/api/runs", {"task_id": task["id"]})
            run_id = run["id"]
            deadline = monotonic() + 30
            while True:
                current = client.get(f"/api/runs/{run_id}")
                current.raise_for_status()
                state = current.json()["state"]
                if state == "AWAITING_PLAN_APPROVAL":
                    break
                if monotonic() >= deadline:
                    pytest.fail(str((
                        state, asyncio.run(_run_diagnostics(test_database_url, run_id)),
                        (data_root / "worker.log").read_text()[-1800:],
                    )))
                sleep(0.1)
            current = client.get(f"/api/runs/{run_id}")
            current.raise_for_status()
            body = {"gate": "plan", "run_version": current.json()["version"],
                    "evidence_digest": asyncio.run(_pending_run_evidence(test_database_url, run_id)),
                    "policy_version": current.json()["policy_version"]}
            challenge = mutate(f"/api/runs/{run_id}/approval-challenges", body)
            mutate(f"/api/runs/{run_id}/approvals", {
                "gate": "plan", "run_version": body["run_version"],
                "evidence_digest": body["evidence_digest"],
                "challenge_token": challenge["token"],
            })
            checkpoint_path = f"/api/runs/{run_id}/usage-checkpoint"
            deadline = monotonic() + 35
            while True:
                response = client.get(checkpoint_path)
                assert response.status_code == 200, response.text
                checkpoint = response.json()["checkpoint"]
                if checkpoint and checkpoint["state"] == "paused" and checkpoint["process_settled"]:
                    break
                if monotonic() >= deadline:
                    pytest.fail(str((
                        None if checkpoint is None else checkpoint["state"],
                        client.get(f"/api/runs/{run_id}").json()["state"],
                        asyncio.run(_run_diagnostics(test_database_url, run_id)),
                        (data_root / "worker.log").read_text()[-1800:],
                    )))
                sleep(0.1)
            report = response.json()["report"]
            assert report["input_tokens"] == 62 and report["output_tokens"] == 42
            assert report["baseline"]["reference_tokens"] == 20
            assert "[DELEGATED OBSERVED] usage persisted while process live" in (data_root / "worker.log").read_text()
            before = asyncio.run(_delegated_journey(test_database_url, run_id))
            first_child_sources = [row for row in before["observations"]
                                   if row[1].startswith("scripted-child-") and row[2] == 60 and row[3] == 40]
            assert len(first_child_sources) == 1, before
            assert any(row[0] == first_child_sources[0][0] for row in before["units"])
            assert len(before["attempts"]) == 3, before
            assert sum(row[4] == "1" for row in before["attempts"]) == 3, before
            assert before["repair_debits"] == 0, before
            continued = client.post(checkpoint_path + "/commands", json={
                "checkpoint_id": checkpoint["checkpoint_id"],
                "expected_version": checkpoint["version"],
                "idempotency_key": "delegated-process-continue",
                "action": "continue", "continue_mode": "next_checkpoint",
            })
            assert continued.status_code == 200, continued.text
            assert continued.json()["checkpoint"]["state"] == "resumed"
            deadline = monotonic() + 35
            while True:
                response = client.get(checkpoint_path)
                assert response.status_code == 200, response.text
                current = response.json()["checkpoint"]
                after = asyncio.run(_delegated_journey(test_database_url, run_id))
                observed_children = [row for row in after["observations"]
                                     if row[1].startswith("scripted-child-") and row[2] == 60 and row[3] == 40]
                if (len(observed_children) >= 2 and current and current["generation"] >= 2
                        and current["state"] == "paused" and current["process_settled"]):
                    break
                if monotonic() >= deadline:
                    pytest.fail(str((current, after, asyncio.run(_run_diagnostics(test_database_url, run_id)),
                                     (data_root / "worker.log").read_text()[-1800:])))
                sleep(0.1)
            assert len(observed_children) == 2, after
            assert len({row[0] for row in observed_children}) == 2, after
            assert len({row[1] for row in observed_children}) == 2, after
            assert len(after["attempts"]) == 4, after
            assert sum(row[4] == "1" for row in after["attempts"]) == 4, after
            assert after["repair_debits"] == before["repair_debits"] == 0, after
            assert response.json()["report"]["baseline"]["reference_tokens"] == 20
            assert response.json()["report"]["baseline"]["snapshot_digest"] == report["baseline"]["snapshot_digest"]
            assert response.json()["report"]["input_tokens"] == 122
            assert response.json()["report"]["output_tokens"] == 82
