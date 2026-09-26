"""A8 frozen profiles and queued run controls across public process restarts."""

import asyncio
import json
import os
import subprocess
import time
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from alembic import command
from forge.domain.operation import canonical_digest
from forge.domain.subscription import (
    ExecutionEnvelope,
    SpecialistPurpose,
    decode_subscription_record,
)
from forge.persistence.database import create_engine
from sqlalchemy import text

from tests.acceptance.process_harness import ForgeProcessHarness
from tests.acceptance.test_subscription_operator_process import _retain_evidence, _wait_worker

pytestmark = pytest.mark.integration
pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


def _route(provider, client, model, effort):
    return {
        "provider": provider,
        "client": client,
        "model": model,
        "effort": effort,
        "auth_mode": "subscription",
        "billing_mode": "allowance_only",
    }


ASTRA = _route("openai", "codex_app_server", "gpt-6-astra", "low")
SOL = _route("openai", "codex_app_server", "gpt-5.6-sol", "high")
GEMINI = _route("google", "gemini_cli", "gemini-3.8-flash", "medium")
LUNA = _route("openai", "codex_app_server", "gpt-5.6-luna", "medium")
TERRA = _route("openai", "codex_app_server", "gpt-5.6-terra", "high")
GEMINI_NEXT = GEMINI | {"effort": "high"}


def _profile(primary, *, worker=GEMINI, fallback=LUNA):
    return {
        "preferences": [
            {"purpose": "primary", "preferred_route": primary},
            {
                "purpose": "routine_implementation",
                "preferred_route": worker,
                "fallback_routes": [fallback],
            },
        ],
        "default_billing_mode": "allowance_only",
    }


def _mutation(client, method, path, body, *, key=None):
    response = client.request(
        method, path, json=body, headers={"Idempotency-Key": key or str(uuid4())}
    )
    response.raise_for_status()
    return response.json()


def _wait_run(client, harness, run_id, expected):
    deadline = time.monotonic() + 15
    last = None
    while time.monotonic() < deadline:
        assert harness._worker_process.poll() is None, "public worker exited"
        response = client.get(f"/api/runs/{run_id}")
        response.raise_for_status()
        last = response.json()
        if last["state"] == expected:
            return last
        time.sleep(0.1)
    raise AssertionError(f"run did not reach {expected}: {last}")


def _queued_primary(client, run_id, route):
    response = client.get(f"/api/runs/{run_id}/subscription-tasks")
    response.raise_for_status()
    page = response.json()
    assert page["subscription"] is True and page["has_more"] is False
    assert len(page["tasks"]) == 1
    primary = page["tasks"][0]
    assert primary["purpose"] == "primary" and primary["state"] == "queued"
    assert primary["requested_route"] == primary["effective_route"] == route
    assert primary["fallback_selected"] is False and primary["repairs"] == 0
    assert primary["quota_status"]["status"] == "unknown"
    assert primary["quota_status"]["observed_at"] is None
    assert page["capacity"]["host"]["active"] == page["capacity"]["run"]["active"] == 0
    attempts = client.get(f"/api/runs/{run_id}/subscription-tasks/{primary['task_id']}/attempts")
    attempts.raise_for_status()
    assert attempts.json()["attempts"] == []
    return primary


def _repository(root):
    root.mkdir()
    (root / "README.md").write_text("A8 frozen profile fixture\n", encoding="utf-8")
    environment = {
        **{key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")},
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "Forge fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "Forge fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    }
    for args in (
        ("init", "-b", "main"),
        ("add", "README.md"),
        ("commit", "-m", "fixture"),
        ("remote", "add", "origin", "https://github.com/example/a8-profiles.git"),
    ):
        subprocess.run(
            ["git", "-C", str(root), *args],
            env=environment,
            check=True,
            capture_output=True,
            timeout=15,
        )


def test_profile_edits_and_queued_resume_preserve_frozen_runs_across_process_restarts(
    test_database_url, alembic_config_factory, tmp_path
):
    command.upgrade(alembic_config_factory(test_database_url), "head")
    data_root, repository = tmp_path / "keyless", tmp_path / "repository"
    data_root.mkdir()
    _repository(repository)
    with ForgeProcessHarness(
        database_url=test_database_url,
        data_root=data_root,
        prompt_root=Path.cwd() / "agents",
        subscription_only=True,
    ) as harness:
        harness.start_api()
        harness.start_worker()
        with httpx.Client(
            base_url=harness.base_url, headers={"Origin": harness.base_url}, timeout=5
        ) as client:
            bootstrap = _mutation(
                client, "POST", "/api/auth/bootstrap", {"token": harness.bootstrap_token()}
            )
            client.headers["X-CSRF-Token"] = bootstrap["csrf_token"]
            first_worker = _wait_worker(client, harness, previous=set())
            assert first_worker["routes"] == []
            project = _mutation(
                client,
                "POST",
                "/api/projects",
                {
                    "name": "A8 profiles",
                    "repository_path": str(repository),
                    "github_repository": "example/a8-profiles",
                    "default_branch": "main",
                    "commands": [
                        {
                            "kind": "test",
                            "name": "unit",
                            "argv": ["python", "--version"],
                            "timeout_seconds": 30,
                        }
                    ],
                },
            )
            profile = _mutation(client, "POST", "/api/subscription-profiles", _profile(ASTRA))
            profile_id = profile["profile_id"]
            selection_path = f"/api/projects/{project['id']}/subscription-profile"
            _mutation(
                client, "PUT", selection_path, {"profile_id": profile_id, "profile_version": 1}
            )

            def create_run(title):
                task = _mutation(
                    client,
                    "POST",
                    "/api/tasks",
                    {"project_id": project["id"], "title": title, "body": "Profile freeze fixture"},
                )
                created = _mutation(client, "POST", "/api/runs", {"task_id": task["id"]})
                return _wait_run(client, harness, created["id"], "PLANNING")

            original = create_run("Frozen Astra primary")
            original_id = original["id"]
            primary_before = _queued_primary(client, original_id, ASTRA)
            frozen_before = asyncio.run(_stored(test_database_url))
            assert frozen_before["envelopes"][original_id]["routes"] == {
                "primary": ASTRA,
                "routine_implementation": GEMINI,
            }
            assert frozen_before["envelopes"][original_id]["fallbacks"][
                "routine_implementation"
            ] == [LUNA]
            pause_body = {"command_type": "pause", "expected_run_version": original["version"]}
            pause = _mutation(client, "POST", f"/api/runs/{original_id}/commands", pause_body)
            paused = _wait_run(client, harness, original_id, "PAUSED")
            assert paused["suspended_state"] == "PLANNING"

            # The explicit local operator edits a future default; no model is
            # launched and this fixture does not change any real operator profile.
            profile_file = data_root / "profile-version-2.json"
            profile_file.write_text(
                json.dumps(
                    _profile(SOL, worker=GEMINI_NEXT, fallback=TERRA)
                    | {"expected_current_version": 1}
                ),
                encoding="utf-8",
            )
            append_args = [
                "profile",
                "append",
                "--profile-id",
                profile_id,
                "--file",
                str(profile_file),
                "--idempotency-key",
                str(uuid4()),
            ]
            appended = json.loads(harness.run_cli(append_args))
            assert appended["version"] == 2 and json.loads(harness.run_cli(append_args)) == appended
            assert (
                json.loads(
                    harness.run_cli(
                        ["profile", "show", "--profile-id", profile_id, "--version", "1"]
                    )
                )
                == profile
            )
            selected = _mutation(
                client,
                "PUT",
                selection_path,
                {
                    "profile_id": profile_id,
                    "profile_version": 2,
                    "expected_profile_id": profile_id,
                    "expected_profile_version": 1,
                },
            )
            assert selected == appended
            stale = client.post(
                f"/api/subscription-profiles/{profile_id}/versions",
                json=_profile(ASTRA) | {"expected_current_version": 1},
                headers={"Idempotency-Key": str(uuid4())},
            )
            assert stale.status_code == 409
            assert (
                asyncio.run(_stored(test_database_url))["envelopes"][original_id]
                == frozen_before["envelopes"][original_id]
            )
            future = create_run("Explicitly selected future Sol primary")
            future_id = future["id"]
            _queued_primary(client, future_id, SOL)
            before_restart = asyncio.run(_stored(test_database_url))
            assert before_restart["envelopes"][future_id]["routes"] == {
                "primary": SOL,
                "routine_implementation": GEMINI_NEXT,
            }
            assert before_restart["envelopes"][future_id]["fallbacks"][
                "routine_implementation"
            ] == [TERRA]

            harness.stop_worker()
            resume_key = str(uuid4())
            resume_body = {"command_type": "resume", "expected_run_version": paused["version"]}
            resume = _mutation(
                client, "POST", f"/api/runs/{original_id}/commands", resume_body, key=resume_key
            )
            assert client.get(f"/api/runs/{original_id}").json()["state"] == "PAUSED"
            assert (
                asyncio.run(_stored(test_database_url))["envelopes"] == before_restart["envelopes"]
            )
            harness.restart_api()
            # Persisted browser auth and the queued command survive API restart.
            assert (
                _mutation(
                    client, "POST", f"/api/runs/{original_id}/commands", resume_body, key=resume_key
                )["id"]
                == resume["id"]
            )
            harness.start_worker()
            second_worker = _wait_worker(
                client, harness, previous={first_worker["worker_instance_id"]}
            )
            assert second_worker["routes"] == []
            resumed = _wait_run(client, harness, original_id, "PLANNING")
            assert resumed["version"] > paused["version"]
            primary_after = _queued_primary(client, original_id, ASTRA)
            assert primary_after["task_id"] == primary_before["task_id"]
            _queued_primary(client, future_id, SOL)
            assert client.get(selection_path).json() == appended
            assert (
                json.loads(
                    harness.run_cli(["profile", "project-show", "--project-id", project["id"]])
                )
                == appended
            )
            settled = asyncio.run(_stored(test_database_url))
            assert settled["envelopes"] == before_restart["envelopes"]
            assert {row["profile_version"] for row in settled["envelopes"].values()} == {1, 2}
            assert all(value == 0 for value in settled["execution_counts"].values())
            assert settled["run_controls"][original_id] == {"run.paused": 1, "run.resumed": 1}
            assert settled["profile_versions"] == 2
            assert (
                _mutation(
                    client, "POST", f"/api/runs/{original_id}/commands", resume_body, key=resume_key
                )["id"]
                == resume["id"]
            )
            assert asyncio.run(_stored(test_database_url)) == settled
            evidence = {
                "scenario": "A8-profile-freeze-resume",
                "provider_calls": False,
                "proof": "Public API/worker/CLI, actual PostgreSQL, empty default runtime registry",
                "profile_id": profile_id,
                "original_run": original_id,
                "future_run": future_id,
                "pause_command": pause["id"],
                "resume_command": resume["id"],
                "before_restart": before_restart,
                "after_resume": settled,
                "worker_instances": [
                    first_worker["worker_instance_id"],
                    second_worker["worker_instance_id"],
                ],
                "limits": "No rendered browser, provider execution, active-client drain or billing/isolation conformance",
            }
    assert all(process.poll() is not None for process in harness._processes)
    assert asyncio.run(_stored(test_database_url)) == settled
    evidence["terminal_processes"] = [
        {"pid": process.pid, "return_code": process.returncode} for process in harness._processes
    ]
    _retain_evidence(evidence)


async def _stored(database_url):
    engine = create_engine(database_url)
    try:
        async with engine.connect() as connection:
            envelopes = {}
            for row in (
                await connection.execute(
                    text("SELECT run_id, payload FROM subscription_envelopes ORDER BY run_id")
                )
            ).mappings():
                envelope = decode_subscription_record(row["payload"])
                assert isinstance(envelope, ExecutionEnvelope)
                route = envelope.route_for(SpecialistPurpose.PRIMARY)
                assert route.is_primary and route.requested == route.effective
                assert route.effective.billing_mode.value == "allowance_only"
                envelopes[str(row["run_id"])] = {
                    "digest": canonical_digest(row["payload"]),
                    "profile_version": envelope.profile_version,
                    "model": route.effective.model,
                    "effort": route.effective.effort.value,
                    "routes": {
                        purpose.value: asdict(binding.effective)
                        for purpose, binding in envelope.routes
                    },
                    "fallbacks": {
                        purpose.value: [asdict(route) for route in routes]
                        for purpose, routes in envelope.allowed_fallbacks
                    },
                }
            controls = {}
            for run_id, kind, count in await connection.execute(
                text(
                    "SELECT run_id, event_type, count(*) FROM run_events WHERE event_type IN ('run.paused','run.resumed') GROUP BY run_id,event_type"
                )
            ):
                controls.setdefault(str(run_id), {})[kind] = count
            counts = {}
            for table in (
                "subscription_attempts",
                "subscription_client_launches",
                "subscription_quota_admissions",
                "subscription_budget_reservations",
            ):
                counts[table] = await connection.scalar(text(f"SELECT count(*) FROM {table}"))
            return {
                "envelopes": envelopes,
                "run_controls": controls,
                "execution_counts": counts,
                "profile_versions": await connection.scalar(
                    text("SELECT count(*) FROM subscription_profile_versions")
                ),
            }
    finally:
        await engine.dispose()
