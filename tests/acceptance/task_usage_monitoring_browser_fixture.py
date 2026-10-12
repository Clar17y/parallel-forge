"""Hold real Forge API, worker, web, and PostgreSQL for an owner browser journey.

The only replaced boundaries are test-owned provider/GitHub transports. The
loopback redirect mints a one-use bootstrap token without printing it to logs.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from time import monotonic, sleep
from uuid import uuid4

import httpx
from alembic import command
from forge.domain.subscription import TaskBudget, decode_subscription_record
from forge.persistence.database import create_engine
from sqlalchemy import text
from sqlalchemy.engine import make_url

from apps.orchestrator.tests.persistence.conftest import (
    _create_database,
    _drop_database,
    alembic_config,
    validated_test_database_name,
)
from tests.acceptance.process_harness import ForgeProcessHarness, free_loopback_port
from tests.acceptance.test_task_usage_monitoring_process import (
    _pending_run_evidence,
    _repository_with_managed_worktrees,
)
from tests.acceptance.test_task_usage_reporting_process import _seed_no_policy_samples


def _wait_web(origin: str, process: subprocess.Popen[bytes]) -> None:
    deadline = monotonic() + 45
    while monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"web process exited early: {process.returncode}")
        try:
            if httpx.get(origin, timeout=1).status_code < 500:
                return
        except httpx.HTTPError:
            pass
        sleep(0.2)
    raise RuntimeError("web did not become ready")


def _mutate(client: httpx.Client, path: str, body: dict, method: str = "POST") -> dict:
    reply = client.request(
        method, path, json=body, headers={"Idempotency-Key": str(uuid4())}
    )
    if reply.status_code >= 400:
        raise RuntimeError(f"{path}: HTTP {reply.status_code}: {reply.text[:300]}")
    return reply.json()


def _prepare_scenarios(
    harness: ForgeProcessHarness, repository: Path, database_url: str, web_origin: str
) -> dict[str, str]:
    with httpx.Client(
        base_url=web_origin,
        headers={"Origin": web_origin},
        timeout=10,
    ) as client:
        session = _mutate(client, "/api/auth/bootstrap", {"token": harness.bootstrap_token()})
        client.headers["X-CSRF-Token"] = session["csrf_token"]
        project = _mutate(client, "/api/projects", {
            "name": "Observed browser checkpoint", "repository_path": str(repository),
            "github_repository": "example/a8-profiles", "default_branch": "main",
            "runner_mode": "trusted_host", "trusted_project": True,
            "commands": [{"kind": "test", "name": "unit", "argv": ["python", "--version"],
                          "timeout_seconds": 30}],
        })
        project_id = project["id"]
        primary = {"provider": "openai", "client": "codex_app_server",
                   "model": "gpt-6-astra", "effort": "low", "auth_mode": "subscription",
                   "billing_mode": "allowance_only"}
        child = {"provider": "google", "client": "gemini_cli",
                 "model": "gemini-3.8-flash", "effort": "medium",
                 "auth_mode": "subscription", "billing_mode": "allowance_only"}
        profile = _mutate(client, "/api/subscription-profiles", {"preferences": [
            {"purpose": "primary", "preferred_route": primary},
            {"purpose": "routine_implementation", "preferred_route": child},
            {"purpose": "exploration", "preferred_route": child},
        ]})
        _mutate(client, f"/api/projects/{project_id}/subscription-profile", {
            "profile_id": profile["profile_id"], "profile_version": 1,
        }, "PUT")
        _mutate(client, f"/api/projects/{project_id}/monitoring-policy", {
            "expected_revision": 1, "mode": "checkpoint", "manual_estimate_tokens": 20,
            "hard_token_cap": 5000,
        })
        requirement_id, item_id = str(uuid4()), str(uuid4())
        brief_content = {
            "problem": "Review exact shared epic caps",
            "outcomes": ["The owner can edit the shared ceiling"],
            "requirements": [{
                "requirement_id": requirement_id,
                "text": "Retain exact token ceilings",
                "acceptance_criteria": ["Read back the saved numeric ceiling"],
            }],
        }
        budget_epic = _mutate(client, "/api/epics", {
            "project_id": project_id, "title": "Exact browser budget",
            "draft": brief_content,
        })
        budget_epic_id = budget_epic["epic_id"]
        budget_path = f"/api/epics/{budget_epic_id}"
        saved_brief = _mutate(client, budget_path + "/brief-revisions", {
            "expected_epic_version": 1, "content": brief_content,
        })
        _mutate(client, budget_path + "/brief-adoptions", {
            "expected_epic_version": 2,
            "brief_revision_id": saved_brief["brief_revision_id"],
            "brief_digest": saved_brief["content_digest"],
        })
        saved_graph = _mutate(client, budget_path + "/graph-revisions", {
            "expected_epic_version": 3,
            "brief_revision_id": saved_brief["brief_revision_id"],
            "brief_digest": saved_brief["content_digest"],
            "items": [{
                "item_id": item_id, "ordinal": 0, "disposition": "required",
                "title": "Verify ceiling", "outcome": "Exact numeric storage",
                "acceptance_criteria": ["Match the submitted integer"],
                "source_requirement_ids": [requirement_id], "dependency_item_ids": [],
            }],
        })
        _mutate(client, budget_path + "/graph-adoptions", {
            "expected_epic_version": 4,
            "graph_revision_id": saved_graph["graph_revision_id"],
            "graph_digest": saved_graph["graph_digest"],
        })
        _mutate(client, budget_path + "/executions", {"expected_epic_version": 5})
        epic = _mutate(client, "/api/epics", {
            "project_id": project_id, "title": "Observed browser authoring",
        })
        epic_id = epic["epic_id"]
        conversation = _mutate(
            client, f"/api/epics/{epic_id}/brainstorm-conversations",
            {"project_id": project_id, "text": "Investigate observed usage"},
        )
        conversation_id = conversation["conversation_id"]
        turns = client.get(
            f"/api/epics/{epic_id}/brainstorm-conversations/{conversation_id}/turns",
            params={"project_id": project_id},
        )
        turns.raise_for_status()
        job = _mutate(
            client, f"/api/epics/{epic_id}/brainstorm-conversations/{conversation_id}/jobs",
            {"project_id": project_id, "prompt_turn_id": turns.json()[0]["turn_id"],
             "expected_epic_version": 1,
             "expected_conversation_version": conversation["version"]},
        )
        task = _mutate(client, "/api/tasks", {
            "project_id": project_id, "title": "Observed delegated browser run",
            "body": "Bounded README update with a measured child provider",
        })
        run = _mutate(client, "/api/runs", {"task_id": task["id"]})
        run_id = run["id"]
        deadline = monotonic() + 30
        while True:
            response = client.get(f"/api/runs/{run_id}")
            response.raise_for_status()
            if response.json()["state"] == "AWAITING_PLAN_APPROVAL":
                break
            if monotonic() >= deadline:
                raise RuntimeError("run did not reach plan approval")
            sleep(0.1)
        current = response.json()
        approval = {
            "gate": "plan", "run_version": current["version"],
            "evidence_digest": asyncio.run(_pending_run_evidence(database_url, run_id)),
            "policy_version": current["policy_version"],
        }
        challenge = _mutate(client, f"/api/runs/{run_id}/approval-challenges", approval)
        _mutate(client, f"/api/runs/{run_id}/approvals", {
            "gate": "plan", "run_version": approval["run_version"],
            "evidence_digest": approval["evidence_digest"],
            "challenge_token": challenge["token"],
        })
        return {
            "project_id": project_id,
            "epic_id": epic_id,
            "job_id": job["job_id"],
            "run_id": run_id,
            "authoring_path": f"/epics/{epic_id}?project_id={project_id}&job_id={job['job_id']}",
            "run_path": f"/runs/{run_id}",
            "budget_epic_id": budget_epic_id,
        }


async def _storage_snapshot(database_url: str, scenario: dict[str, str]) -> dict[str, object]:
    engine = create_engine(database_url)
    try:
        async with engine.connect() as db:
            project = (await db.execute(text(
                "SELECT p.id, s.profile_id, s.profile_version FROM projects p "
                "LEFT JOIN project_subscription_profiles s ON s.project_id = p.id "
                "WHERE p.id = :project"
            ), {"project": scenario["project_id"]})).one()
            policy = (await db.execute(text(
                "SELECT revision, mode, manual_estimate_tokens, hard_token_cap, "
                "history_limit, minimum_comparables, window_days, hard_context_cap "
                "FROM task_usage_policy_revisions WHERE project_id = :project "
                "ORDER BY revision DESC LIMIT 1"
            ), {"project": scenario["project_id"]})).one()
            epic = (await db.execute(text(
                "SELECT version, accepted_brief_revision_id FROM epics WHERE id = :epic"
            ), {"epic": scenario["epic_id"]})).one()
            job = (await db.execute(text(
                "SELECT state, version, proposal_digest, adopted_revision_id "
                "FROM epic_brainstorm_jobs WHERE id = :job"
            ), {"job": scenario["job_id"]})).one()
            run = (await db.execute(text(
                "SELECT state, version FROM runs WHERE id = :run"
            ), {"run": scenario["run_id"]})).one()
            checkpoint = (await db.execute(text(
                "SELECT subject_kind, state, version, generation, process_settled, "
                "next_checkpoint_threshold_tokens, usage_pauses_disabled "
                "FROM task_usage_checkpoints "
                "WHERE subject_id IN (:job, :run) ORDER BY subject_kind, generation"
            ), {"job": scenario["job_id"], "run": scenario["run_id"]})).all()
            attempts = (await db.execute(text(
                "SELECT count(*) FILTER (WHERE a.task_row_id IN "
                "(SELECT id FROM subscription_tasks WHERE run_id = :run)), "
                "count(*) FILTER (WHERE c.attempt_id IS NOT NULL) "
                "FROM subscription_attempts a LEFT JOIN subscription_attempt_consumption c "
                "ON c.attempt_id = a.id WHERE a.run_id = :run"
            ), {"run": scenario["run_id"]})).one()
            policy_history = (await db.execute(text(
                "SELECT revision, mode, manual_estimate_tokens, hard_token_cap, "
                "warning_multiplier FROM task_usage_policy_revisions "
                "WHERE project_id = :project ORDER BY revision"
            ), {"project": scenario["project_id"]})).all()
            jobs = (await db.execute(text(
                "SELECT id, state, version, snapshot->>'kind', proposal_digest, "
                "adopted_revision_id FROM epic_brainstorm_jobs "
                "WHERE project_id = :project ORDER BY created_at"
            ), {"project": scenario["project_id"]})).all()
            admissions = (await db.execute(text(
                "SELECT subject_kind, subject_id, policy_revision, configured_route_digest "
                "FROM task_usage_subject_admissions WHERE project_id = :project "
                "ORDER BY subject_kind, admitted_at"
            ), {"project": scenario["project_id"]})).all()
            baselines = (await db.execute(text(
                "SELECT subject_kind, subject_id, policy_revision, snapshot_digest, "
                "status, reference_tokens FROM task_usage_baselines WHERE project_id = :project "
                "ORDER BY subject_kind, created_at"
            ), {"project": scenario["project_id"]})).all()
            sources = (await db.execute(text(
                "SELECT u.subject_kind, u.subject_id, u.source_kind, u.source_id, "
                "count(o.id), count(o.id) FILTER (WHERE o.final), "
                "max(o.input_tokens), max(o.output_tokens) "
                "FROM task_usage_work_units u LEFT JOIN task_usage_observations o "
                "ON o.work_unit_id = u.id WHERE u.project_id = :project "
                "GROUP BY u.subject_kind, u.subject_id, u.source_kind, u.source_id "
                "ORDER BY u.subject_kind, u.subject_id, u.source_kind, u.source_id"
            ), {"project": scenario["project_id"]})).all()
            report_sources = (await db.execute(text(
                "SELECT u.project_id, count(DISTINCT u.id), count(o.id), "
                "coalesce(sum(o.input_tokens), 0)::bigint, "
                "coalesce(sum(o.output_tokens), 0)::bigint "
                "FROM task_usage_work_units u LEFT JOIN task_usage_observations o "
                "ON o.work_unit_id = u.id "
                "WHERE u.project_id IN (:measured, :unknown) GROUP BY u.project_id "
                "ORDER BY u.project_id"
            ), {"measured": scenario["measured_project_id"],
                "unknown": scenario["unknown_project_id"]})).all()
            budget_row = (await db.execute(text(
                "SELECT version, ceiling FROM epic_brainstorm_budget_ledgers "
                "WHERE epic_id = :epic"
            ), {"epic": scenario["budget_epic_id"]})).one_or_none()
            project_policy = (await db.execute(text(
                "SELECT version, document #>> '{planner_model,max_input_tokens}', "
                "document #>> '{planner_model,max_output_tokens}' "
                "FROM project_policy_versions WHERE project_id = :project "
                "ORDER BY version DESC LIMIT 1"
            ), {"project": scenario["project_id"]})).one()
            profile_versions = (await db.execute(text(
                "SELECT profile_id, version, payload "
                "FROM subscription_profile_versions ORDER BY profile_id, version"
            ))).all()
            owner_commands = (await db.execute(text(
                "SELECT c.checkpoint_id, c.action, c.expected_version, c.result_version "
                "FROM task_usage_owner_commands c JOIN task_usage_checkpoints p "
                "ON p.id = c.checkpoint_id WHERE p.subject_id IN (:job, :run) "
                "ORDER BY c.created_at, c.id"
            ), {"job": scenario["job_id"], "run": scenario["run_id"]})).all()
            budget = decode_subscription_record(budget_row[1]) if budget_row else None
            if budget_row is not None and not isinstance(budget, TaskBudget):
                raise TypeError("stored epic ceiling was not a task budget")
            stored_profile_versions = []
            for profile_id, version, payload in profile_versions:
                profile = decode_subscription_record(payload)
                role_budget = profile.preferences[0].token_budget
                stored_profile_versions.append([
                    str(profile_id), version,
                    None if role_budget is None or role_budget.max_input_tokens is None
                    else str(role_budget.max_input_tokens),
                    None if role_budget is None or role_budget.max_output_tokens is None
                    else str(role_budget.max_output_tokens),
                ])
            return {
                "project": [str(project[0]), str(project[1]) if project[1] else None, project[2]],
                "policy": list(policy),
                "policy_exact": {
                    "revision": policy[0],
                    "manual_estimate_tokens": None if policy[2] is None else str(policy[2]),
                    "hard_token_cap": None if policy[3] is None else str(policy[3]),
                    "history_limit": str(policy[4]),
                    "minimum_comparables": str(policy[5]),
                    "window_days": None if policy[6] is None else str(policy[6]),
                    "hard_context_cap": None if policy[7] is None else str(policy[7]),
                },
                "policy_revision_exact": str(policy[0]),
                "epic": [epic[0], str(epic[1]) if epic[1] else None],
                "job": [job[0], job[1], job[2], str(job[3]) if job[3] else None],
                "run": list(run), "checkpoints": [list(row) for row in checkpoint],
                "attempts": list(attempts),
                "policy_history": [list(row) for row in policy_history],
                "jobs": [[str(row[0]), *[str(value) if hasattr(value, "hex") else value
                                     for value in row[1:]]] for row in jobs],
                "admissions": [[row[0], str(row[1]), row[2], row[3]] for row in admissions],
                "baselines": [[row[0], str(row[1]), *row[2:]] for row in baselines],
                "sources": [[row[0], str(row[1]), row[2], str(row[3]), *row[4:]]
                            for row in sources],
                "report_sources": [[str(row[0]), *row[1:]] for row in report_sources],
                "budget": None if budget_row is None else {
                    "version": budget_row[0],
                    "max_input_tokens": None if budget.max_input_tokens is None else str(budget.max_input_tokens),
                    "max_output_tokens": None if budget.max_output_tokens is None else str(budget.max_output_tokens),
                },
                "project_model_caps": {
                    "version": project_policy[0],
                    "max_input_tokens": project_policy[1],
                    "max_output_tokens": project_policy[2],
                },
                "profile_versions": stored_profile_versions,
                "owner_commands": [[str(row[0]), row[1], str(row[2]), str(row[3])]
                                   for row in owner_commands],
            }
    finally:
        await engine.dispose()


async def _seed_high_owner_versions(database_url: str, scenario: dict[str, str]) -> dict[str, object]:
    """Test-only counter seeding; subsequent actions still cross the real API and worker."""
    engine = create_engine(database_url)
    try:
        async with engine.begin() as db:
            seeded = {}
            for kind, subject, version in (
                ("run", scenario["run_id"], 9007199254740993),
                ("authoring_job", scenario["job_id"], 9223372036854775805),
            ):
                row = (await db.execute(text(
                    "UPDATE task_usage_checkpoints SET version = :version "
                    "WHERE subject_kind = :kind AND subject_id = :subject "
                    "AND state = 'paused' AND process_settled RETURNING id, version"
                ), {"version": version, "kind": kind, "subject": subject})).one()
                seeded[kind] = {"checkpoint_id": str(row[0]), "version": str(row[1])}
            return seeded
    finally:
        await engine.dispose()


async def _seed_high_monitoring_revision(database_url: str, scenario: dict[str, str]) -> str:
    """Seed only the disposable fixture's next owner revision boundary."""
    engine = create_engine(database_url)
    try:
        async with engine.begin() as db:
            row = (await db.execute(text(
                "INSERT INTO task_usage_policy_revisions "
                "(project_id, revision, mode, warning_multiplier, checkpoint_multiplier, "
                "history_limit, minimum_comparables, window_days, hard_token_cap, "
                "hard_context_cap, manual_estimate_tokens, allow_cross_project_fallback, actor_id) "
                "SELECT project_id, 9223372036854775806, mode, warning_multiplier, "
                "checkpoint_multiplier, history_limit, minimum_comparables, window_days, "
                "hard_token_cap, hard_context_cap, manual_estimate_tokens, "
                "allow_cross_project_fallback, actor_id FROM task_usage_policy_revisions "
                "WHERE project_id = :project ORDER BY revision DESC LIMIT 1 "
                "RETURNING revision"
            ), {"project": scenario["project_id"]})).one()
            return str(row[0])
    finally:
        await engine.dispose()


def main() -> None:
    root = Path.cwd()
    source = make_url("postgresql+asyncpg://forge:forge@127.0.0.1:5435/forge")
    resumed = os.environ.get("FORGE_BROWSER_RESUME_ROOT")
    database_name = validated_test_database_name(
        os.environ.get("FORGE_BROWSER_RESUME_DB") or f"forge_test_{uuid4().hex}"
    )
    database_url = source.set(database=database_name).render_as_string(hide_password=False)
    if not resumed:
        asyncio.run(_create_database(source, database_name))
    try:
        if not resumed:
            command.upgrade(alembic_config(database_url), "head")
        # Win32 artifact temp files need a short scratch root in this deep worktree.
        workspace = Path(resumed).resolve() if resumed else root / f".browser-live-{uuid4().hex[:8]}"
        if resumed and (workspace.parent != root.resolve() or not workspace.name.startswith(".browser-live-")):
            raise ValueError("resume root must be this managed worktree's browser fixture scratch")
        data_root = workspace / "data"
        data_root.mkdir(parents=True, exist_ok=True)
        repository = workspace / "repository"
        if not resumed:
            _repository_with_managed_worktrees(repository)
        bare = workspace / "remote.git"
        if not resumed:
            subprocess.run(["git", "init", "--bare", str(bare)], check=True,
                           capture_output=True, timeout=15)
        web_port = int(os.environ["FORGE_BROWSER_WEB_PORT"]) if resumed else free_loopback_port()
        web_origin = f"http://127.0.0.1:{web_port}"
        with ForgeProcessHarness(
            database_url=database_url, data_root=data_root,
            prompt_root=root / "agents", bare_remote_path=bare,
            api_port=int(os.environ["FORGE_BROWSER_API_PORT"]) if resumed else None,
            web_origin=web_origin, task_usage_process=True,
            task_usage_delegated=True,
        ) as harness:
            harness.start_api()
            harness.start_worker()
            node = shutil.which("node")
            if node is None:
                raise RuntimeError("Node.js is required for browser fixture")
            web_log = (data_root / "web.log").open("w", encoding="utf-8")
            try:
                web = subprocess.Popen(
                    [node, str(root / "node_modules" / "next" / "dist" / "bin" / "next"), "start",
                     "--hostname", "127.0.0.1", "--port", str(web_port)],
                    cwd=root / "apps" / "web",
                    env={**os.environ, "FORGE_API_INTERNAL_ORIGIN": harness.base_url,
                         "FORGE_WEB_ORIGIN": web_origin},
                    stdin=subprocess.DEVNULL, stdout=web_log,
                    stderr=subprocess.STDOUT,
                )
                try:
                    _wait_web(web_origin, web)
                    if resumed:
                        scenario = json.loads(Path(os.environ["FORGE_BROWSER_SCENARIO"]).read_text(encoding="utf-8"))
                    else:
                        scenario = _prepare_scenarios(
                            harness, repository, database_url, web_origin
                        )
                        # Browser report filtering uses real registered PostgreSQL
                        # projects with measured, zero, and unavailable samples.
                        measured_project, unknown_project = asyncio.run(
                            _seed_no_policy_samples(database_url)
                        )
                        scenario["measured_project_id"] = str(measured_project)
                        scenario["unknown_project_id"] = str(unknown_project)

                    class Redirect(BaseHTTPRequestHandler):
                        def do_POST(self) -> None:
                            if self.path not in ("/seed-high-owner-versions", "/seed-high-monitoring-revision"):
                                self.send_error(404)
                                return
                            value = json.dumps(asyncio.run(
                                _seed_high_owner_versions(database_url, scenario)
                                if self.path == "/seed-high-owner-versions" else
                                _seed_high_monitoring_revision(database_url, scenario)
                            ))
                            body = value.encode("utf-8")
                            self.send_response(200)
                            self.send_header("Content-Type", "application/json")
                            self.send_header("Content-Length", str(len(body)))
                            self.end_headers()
                            self.wfile.write(body)

                        def do_GET(self) -> None:
                            if self.path == "/snapshot":
                                value = json.dumps(asyncio.run(_storage_snapshot(database_url, scenario)))
                                body = value.encode("utf-8")
                                self.send_response(200)
                                self.send_header("Content-Type", "application/json")
                                self.send_header("Cache-Control", "no-store")
                                self.send_header("Content-Length", str(len(body)))
                                self.end_headers()
                                self.wfile.write(body)
                                return
                            if self.path not in ("/open-authoring", "/open-run"):
                                self.send_error(404)
                                return
                            token = harness.bootstrap_token()
                            path = scenario["authoring_path"] if self.path == "/open-authoring" else scenario["run_path"]
                            self.send_response(302)
                            self.send_header("Location", f"{web_origin}{path}#bootstrap={token}")
                            self.send_header("Cache-Control", "no-store")
                            self.end_headers()

                        def log_message(self, *_args: object) -> None:
                            pass

                    control = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
                    thread = Thread(target=control.serve_forever, daemon=True)
                    thread.start()
                    session_info = {
                        "control_origin": f"http://127.0.0.1:{control.server_port}",
                        "web_origin": web_origin, "api_origin": harness.base_url,
                        **scenario,
                        "api_pid": harness.api_pid, "worker_pid": harness.worker_pid,
                        "web_pid": web.pid, "database_name": database_name,
                        "data_root": str(data_root),
                    }
                    if output := os.environ.get("FORGE_BROWSER_SCENARIO_OUTPUT"):
                        Path(output).write_text(json.dumps(session_info, indent=2), encoding="utf-8")
                    print(json.dumps(session_info), flush=True)
                    try:
                        while web.poll() is None:
                            sleep(0.5)
                    except KeyboardInterrupt:
                        pass
                    finally:
                        control.shutdown()
                        control.server_close()
                        thread.join(timeout=2)
                finally:
                    if web.poll() is None:
                        web.terminate()
                        with suppress(subprocess.TimeoutExpired):
                            web.wait(timeout=5)
                    if web.poll() is None:
                        web.kill()
                        web.wait(timeout=5)
            finally:
                web_log.close()
    finally:
        if not resumed:
            asyncio.run(_drop_database(source, database_name))


if __name__ == "__main__":
    main()
