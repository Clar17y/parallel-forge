"""Controlled foreign-host metadata while a real supervised child is unsettled."""

import asyncio
import subprocess
from pathlib import Path
from time import monotonic, sleep
from uuid import UUID, uuid4

import httpx
import pytest
from alembic import command
from forge.persistence.database import create_engine
from sqlalchemy import text

from tests.acceptance.process_harness import ForgeProcessHarness
from tests.acceptance.test_task_usage_monitoring_process import _repository_with_managed_worktrees

pytestmark = pytest.mark.integration
pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


async def _attempt_host(url: str, job_id: str, replacement: str | None = None) -> tuple[str, bool, bool] | None:
    engine = create_engine(url)
    try:
        async with engine.begin() as db:
            if replacement is not None:
                await db.execute(text(
                    "UPDATE epic_brainstorm_attempts SET origin_host = :host, "
                    "lease_expires_at = now() - interval '1 second' WHERE job_id = :job"
                ), {"host": replacement, "job": UUID(job_id)})
            row = (await db.execute(text(
                "SELECT origin_host, process_started, process_settled "
                "FROM epic_brainstorm_attempts WHERE job_id = :job ORDER BY number DESC LIMIT 1"
            ), {"job": UUID(job_id)})).one_or_none()
            return tuple(row) if row is not None else None
    finally:
        await engine.dispose()


def test_foreign_host_lease_simulation_does_not_falsely_pause_or_resume(
    test_database_url: str, alembic_config_factory, tmp_path: Path,
) -> None:
    command.upgrade(alembic_config_factory(test_database_url), "head")
    repository = tmp_path / "repository"
    _repository_with_managed_worktrees(repository)
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True, timeout=15)
    data_root = tmp_path / "foreign-host"
    data_root.mkdir()
    with ForgeProcessHarness(database_url=test_database_url, data_root=data_root,
                             prompt_root=Path.cwd() / "agents", bare_remote_path=bare,
                             task_usage_process=True, task_usage_slow_settlement=True) as harness:
        harness.start_api()
        harness.start_worker()
        harness.assert_process_identities()
        with httpx.Client(base_url=harness.base_url, headers={"Origin": harness.base_url}, timeout=8) as client:
            def mutate(path: str, body: dict, method: str = "POST") -> dict:
                reply = client.request(method, path, json=body,
                                       headers={"Idempotency-Key": str(uuid4())})
                assert reply.status_code < 400, (path, reply.status_code, reply.text)
                return reply.json()

            session = mutate("/api/auth/bootstrap", {"token": harness.bootstrap_token()})
            client.headers["X-CSRF-Token"] = session["csrf_token"]
            project = mutate("/api/projects", {
                "name": "Foreign host checkpoint", "repository_path": str(repository),
                "github_repository": "example/a8-profiles", "default_branch": "main",
                "commands": [{"kind": "test", "name": "unit", "argv": ["python", "--version"],
                              "timeout_seconds": 30}],
            })
            project_id = project["id"]
            route = {"provider": "google", "client": "gemini_cli", "model": "gemini-3.8-flash",
                     "effort": "medium", "auth_mode": "subscription", "billing_mode": "allowance_only"}
            profile = mutate("/api/subscription-profiles", {"preferences": [
                {"purpose": "primary", "preferred_route": route},
                {"purpose": "exploration", "preferred_route": route},
            ], "default_billing_mode": "allowance_only"})
            mutate(f"/api/projects/{project_id}/subscription-profile", {
                "profile_id": profile["profile_id"], "profile_version": 1,
            }, "PUT")
            mutate(f"/api/projects/{project_id}/monitoring-policy", {
                "expected_revision": 1, "mode": "checkpoint", "manual_estimate_tokens": 20,
            })
            epic = mutate("/api/epics", {"project_id": project_id, "title": "Foreign host"})
            epic_id = epic["epic_id"]
            conversation = mutate(f"/api/epics/{epic_id}/brainstorm-conversations", {
                "project_id": project_id, "text": "Observe unsettled provider",
            })
            conversation_id = conversation["conversation_id"]
            turns = client.get(f"/api/epics/{epic_id}/brainstorm-conversations/{conversation_id}/turns",
                               params={"project_id": project_id})
            turns.raise_for_status()
            receipt = mutate(f"/api/epics/{epic_id}/brainstorm-conversations/{conversation_id}/jobs", {
                "project_id": project_id, "prompt_turn_id": turns.json()[0]["turn_id"],
                "expected_epic_version": 1, "expected_conversation_version": conversation["version"],
            })
            job_id = receipt["job_id"]
            path = f"/api/epics/{epic_id}/brainstorm-jobs/{job_id}/usage-checkpoint"
            deadline = monotonic() + 15
            while monotonic() < deadline:
                response = client.get(path, params={"project_id": project_id})
                response.raise_for_status()
                checkpoint = response.json()["checkpoint"]
                host = asyncio.run(_attempt_host(test_database_url, job_id))
                if checkpoint and checkpoint["state"] in {"checkpoint_requested", "pausing"} and host and host[1]:
                    break
                sleep(0.1)
            else:
                pytest.fail(str((checkpoint, host, (data_root / "worker.log").read_text()[-1200:])))
            assert host is not None
            local_host = host[0]
            assert local_host and host[1] and not host[2]
            asyncio.run(_attempt_host(test_database_url, job_id, "fixture-foreign-host"))
            owner_pid = harness.worker_pid
            peer_pid = harness.start_peer_worker()
            assert peer_pid not in {owner_pid, harness.api_pid}
            harness.stop_worker()
            sleep(1)
            uncertain = client.get(path, params={"project_id": project_id})
            uncertain.raise_for_status()
            current = uncertain.json()["checkpoint"]
            assert current is not None and current["state"] != "paused"
            assert current["process_settled"] is False
            assert asyncio.run(_attempt_host(test_database_url, job_id))[0] == "fixture-foreign-host"
            refused = client.post(path + "/commands", params={"project_id": project_id}, json={
                "checkpoint_id": current["checkpoint_id"],
                "expected_version": current["version"],
                "idempotency_key": "foreign-unsettled-continue",
                "action": "continue", "continue_mode": "next_checkpoint",
            })
            assert refused.status_code == 409, refused.text
            # Restore the original local-host proof only after observing the
            # foreign uncertainty. Recovery may wait for the real child exit.
            asyncio.run(_attempt_host(test_database_url, job_id, local_host))
            deadline = monotonic() + 25
            while monotonic() < deadline:
                settled = client.get(path, params={"project_id": project_id})
                settled.raise_for_status()
                recovered = settled.json()["checkpoint"]
                if recovered and recovered["state"] == "paused" and recovered["process_settled"]:
                    break
                sleep(0.2)
            else:
                pytest.fail(str((recovered, (data_root / "peer-worker.log").read_text()[-1800:])))
