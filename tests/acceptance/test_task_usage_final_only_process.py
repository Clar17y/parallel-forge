"""Final-only provider telemetry across real API, worker, and PostgreSQL."""

import asyncio
import subprocess
from pathlib import Path
from time import monotonic, sleep
from uuid import uuid4

import httpx
import pytest
from alembic import command

from tests.acceptance.process_harness import ForgeProcessHarness
from tests.acceptance.test_task_usage_monitoring_process import (
    _authoring_journey,
    _repository_with_managed_worktrees,
)

pytestmark = pytest.mark.integration
pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


@pytest.mark.parametrize(("telemetry_mode", "reference_kind", "cap", "crossing"), [
    ("final_only", "manual", None, False), ("final_only", "manual", 5000, False),
    ("final_only", "learned", None, False), ("final_only", "learned", 5000, False),
    ("streaming", "learned", None, False), ("streaming", "learned", 5000, False),
    ("streaming", "learned", None, True),
])
def test_final_only_supervised_authoring_keeps_proposal_and_manual_reference(
    test_database_url: str, alembic_config_factory, tmp_path: Path,
    telemetry_mode: str, reference_kind: str, cap: int | None, crossing: bool,
) -> None:
    command.upgrade(alembic_config_factory(test_database_url), "head")
    repository = tmp_path / "repository"
    _repository_with_managed_worktrees(repository)
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True, timeout=15)
    data_root = tmp_path / "final-only"
    data_root.mkdir()
    with ForgeProcessHarness(database_url=test_database_url, data_root=data_root,
                             prompt_root=Path.cwd() / "agents", bare_remote_path=bare,
                             task_usage_process=True,
                             task_usage_final_only=telemetry_mode == "final_only",
                             task_usage_streaming_success=telemetry_mode == "streaming",
                             task_usage_learned_crossing=crossing) as harness:
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
                "name": "Final only authoring", "repository_path": str(repository),
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
            policy = mutate(f"/api/projects/{project_id}/monitoring-policy", {
                "expected_revision": 1, "mode": "checkpoint",
                "manual_estimate_tokens": 20 if reference_kind == "manual" else None,
                "minimum_comparables": 1,
                "hard_token_cap": cap,
            })
            assert policy["hard_token_cap"] == cap
            epic = mutate("/api/epics", {"project_id": project_id, "title": "Final only"})
            epic_id = epic["epic_id"]
            conversation = mutate(f"/api/epics/{epic_id}/brainstorm-conversations", {
                "project_id": project_id, "text": "Final-only usage",
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
            job_path = f"/api/epics/{epic_id}/brainstorm-jobs/{job_id}"
            checkpoint_path = job_path + "/usage-checkpoint"
            deadline = monotonic() + 20
            while monotonic() < deadline:
                outcome = client.get(job_path, params={"project_id": project_id})
                outcome.raise_for_status()
                if outcome.json()["state"] == "proposed":
                    break
                sleep(0.1)
            else:
                pytest.fail(str((outcome.json(), (data_root / "worker.log").read_text()[-1800:])))
            checkpoint = client.get(checkpoint_path, params={"project_id": project_id})
            checkpoint.raise_for_status()
            report = checkpoint.json()["report"]
            assert report["input_tokens"] == 60 and report["output_tokens"] == 40
            if reference_kind == "manual":
                assert report["baseline"]["status"] == "manual"
                assert report["baseline"]["reference_tokens"] == 20
            else:
                assert report["baseline"]["status"] == "insufficient_history"
                assert report["baseline"]["reference_tokens"] is None
            assert report["binding"]["subject"]["subject_id"] == job_id
            journey = asyncio.run(_authoring_journey(test_database_url, job_id))
            assert len(journey["attempts"]) == 1
            assert len(journey["observations"]) == (2 if telemetry_mode == "streaming" else 1)
            assert journey["attempts"][0][2] == "settled"
            assert journey["debits"] == [(1, "60", "40", None)]
            assert journey["repair_debits"] == 0
            assert all(row[1:3] == (60, 40) for row in journey["observations"])
            marker = ("[AUTHORING FINAL ONLY] process settled before telemetry"
                      if telemetry_mode == "final_only" else
                      "[AUTHORING STREAMING SUCCESS] usage persisted before process settled")
            assert marker in (data_root / "worker.log").read_text()
            if reference_kind == "learned":
                next_epic = mutate("/api/epics", {
                    "project_id": project_id, "title": "Learned successor",
                })
                next_epic_id = next_epic["epic_id"]
                next_conversation = mutate(
                    f"/api/epics/{next_epic_id}/brainstorm-conversations", {
                        "project_id": project_id, "text": "Use completed comparable history",
                    })
                next_conversation_id = next_conversation["conversation_id"]
                next_turns = client.get(
                    f"/api/epics/{next_epic_id}/brainstorm-conversations/{next_conversation_id}/turns",
                    params={"project_id": project_id},
                )
                next_turns.raise_for_status()
                successor = mutate(
                    f"/api/epics/{next_epic_id}/brainstorm-conversations/{next_conversation_id}/jobs", {
                        "project_id": project_id,
                        "prompt_turn_id": next_turns.json()[0]["turn_id"],
                        "expected_epic_version": 1,
                        "expected_conversation_version": next_conversation["version"],
                    })
                successor_checkpoint = (
                    f"/api/epics/{next_epic_id}/brainstorm-jobs/{successor['job_id']}/usage-checkpoint"
                )
                deadline = monotonic() + 20
                while monotonic() < deadline:
                    latest = client.get(successor_checkpoint, params={"project_id": project_id})
                    latest.raise_for_status()
                    if latest.json()["report"] is not None and latest.json()["report"]["baseline"] is not None:
                        break
                    sleep(0.1)
                else:
                    pytest.fail("learned successor never admitted")
                reference = latest.json()["report"]["baseline"]
                assert reference["status"] == "ready"
                assert reference["reference_tokens"] == 100
                assert reference["sample_count"] == 1
                successor_path = (
                    f"/api/epics/{next_epic_id}/brainstorm-jobs/{successor['job_id']}"
                )
                deadline = monotonic() + 30
                while monotonic() < deadline:
                    completed = client.get(successor_path, params={"project_id": project_id})
                    completed.raise_for_status()
                    if completed.json()["state"] == ("paused" if crossing else "proposed"):
                        break
                    sleep(0.1)
                else:
                    pytest.fail(f"learned successor did not finish: {completed.json()}")
                finished = client.get(successor_checkpoint, params={"project_id": project_id})
                finished.raise_for_status()
                successor_report = finished.json()["report"]
                assert successor_report["baseline"]["status"] == "ready"
                assert successor_report["baseline"]["reference_tokens"] == 100
                assert successor_report["baseline"]["sample_count"] == 1
                frozen_digest = successor_report["baseline"]["snapshot_digest"]
                assert successor_report["input_tokens"] == (120 if crossing else 60)
                assert successor_report["output_tokens"] == (100 if crossing else 40)
                if crossing:
                    assert successor_report["baseline"]["checkpoint_threshold_tokens"] == 200
                    checkpoint = finished.json()["checkpoint"]
                    assert checkpoint["state"] == "paused" and checkpoint["process_settled"]
                    continuation = client.post(successor_checkpoint + "/commands", json={
                        "checkpoint_id": checkpoint["checkpoint_id"],
                        "expected_version": checkpoint["version"],
                        "idempotency_key": "learned-crossing-continue",
                        "action": "continue", "continue_mode": "next_checkpoint",
                    }, params={"project_id": project_id})
                    assert continuation.status_code == 200, continuation.text
                    deadline = monotonic() + 30
                    while monotonic() < deadline:
                        completed = client.get(successor_path, params={"project_id": project_id})
                        completed.raise_for_status()
                        if completed.json()["state"] == "proposed":
                            break
                        sleep(0.1)
                    else:
                        pytest.fail(f"continued learned successor did not finish: {completed.json()}")
                    finished = client.get(successor_checkpoint, params={"project_id": project_id})
                    finished.raise_for_status()
                    successor_report = finished.json()["report"]
                    assert successor_report["input_tokens"] == 180
                    assert successor_report["output_tokens"] == 140
                    assert successor_report["baseline"]["snapshot_digest"] == frozen_digest
                    assert finished.json()["checkpoint"]["generation"] == 2
                successor_journey = asyncio.run(
                    _authoring_journey(test_database_url, successor["job_id"])
                )
                assert len(successor_journey["attempts"]) == (2 if crossing else 1)
                assert all(row[2] == "settled" for row in successor_journey["attempts"])
                assert successor_journey["debits"] == (
                    [(1, "120", "100", "usage_checkpoint"), (2, "60", "40", None)]
                    if crossing else [(1, "60", "40", None)]
                )
                assert successor_journey["repair_debits"] == 0
                assert len(successor_journey["units"]) == (2 if crossing else 1)
                assert len({row[0] for row in successor_journey["units"]}) == len(
                    successor_journey["units"]
                )
                assert successor_journey["units"][0][0] != journey["units"][0][0]
                assert len(successor_journey["observations"]) == (
                    3 if crossing else 2 if telemetry_mode == "streaming" else 1
                )
                expected_counts = ({(120, 100), (60, 40)} if crossing else {(60, 40)})
                assert {row[1:3] for row in successor_journey["observations"]} == expected_counts
                assert len({row[0] for row in successor_journey["observations"]}) == len(
                    successor_journey["observations"]
                )
                assert completed.json()["proposal"]["problem"] == "A resumed observed proposal"
