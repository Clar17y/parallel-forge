"""A8 public API/worker/CLI acceptance without provider credentials or model calls."""

import asyncio
import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from alembic import command
from forge.domain.operation import canonical_digest
from forge.evaluations.credentials import assert_credential_free
from forge.persistence.database import create_engine
from sqlalchemy import text

from tests.acceptance.process_harness import ForgeProcessHarness

pytestmark = pytest.mark.integration
pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


def test_keyless_operator_quota_survives_api_and_worker_restarts(
    test_database_url, alembic_config_factory, tmp_path, monkeypatch
):
    command.upgrade(alembic_config_factory(test_database_url), "head")
    data_root = tmp_path / "keyless"
    data_root.mkdir()
    absent = (
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "ANTHROPIC_API_KEY",
        "FORGE_PROVIDER_SECRET_REFERENCE",
        "FORGE_GOOGLE_API_KEY_REFERENCE",
        "FORGE_PRICING_CATALOG_PATH",
        "FORGE_GITHUB_TOKEN_REFERENCE",
    )
    for name in absent:
        monkeypatch.setenv(name, "fake-ambient-value-must-not-reach-child")
    with ForgeProcessHarness(
        database_url=test_database_url,
        data_root=data_root,
        prompt_root=Path.cwd() / "agents",
        subscription_only=True,
    ) as harness:
        assert not (data_root / "acceptance-pricing.json").exists()
        assert all(name not in harness._env for name in absent)
        assert not (data_root / ".env").exists()
        harness._env["FORGE_SUBSCRIPTION_QUOTA_POLICY"] = json.dumps(
            {"unknown_reset_cooldown_seconds": 60}
        )
        harness.start_api()
        harness.start_worker()
        harness.assert_process_identities()
        response = httpx.get(harness.base_url + "/api/health", timeout=5)
        assert response.status_code == 200 and response.json() == {"status": "ok", "role": "api"}
        original_pids = {"api": harness.api_pid, "worker": harness.worker_pid}
        assert httpx.get(harness.base_url + "/api/subscription-quota").status_code == 401
        with httpx.Client(
            base_url=harness.base_url, headers={"Origin": harness.base_url}, timeout=5
        ) as client:
            bootstrap = client.post(
                "/api/auth/bootstrap",
                json={"token": harness.bootstrap_token()},
                headers={"Idempotency-Key": str(uuid4())},
            )
            bootstrap.raise_for_status()
            client.headers["X-CSRF-Token"] = bootstrap.json()["csrf_token"]
            first_worker = _wait_worker(client, harness, previous=set())
            assert first_worker["state"] == "current" and first_worker["routes"] == []
            assert client.get("/api/subscription-quota").json() == []
            known_reset = datetime.now(UTC) + timedelta(seconds=20)
            body = {
                "provider": "google",
                "account": "a8-local",
                "pool": "known-reset",
                "reason": "Operator observed the fixture allowance limit",
                "reset_at": known_reset.isoformat(),
            }
            key = str(uuid4())
            path = "/api/subscription-quota/reports"
            assert (
                client.post(
                    path, json=body, headers={"Idempotency-Key": key, "X-CSRF-Token": "invalid"}
                ).status_code
                == 403
            )
            for changes in (
                {"reset_at": "2026-09-13T13:00:00"},
                {"account": "password=not-an-account-label"},
            ):
                assert client.post(
                    path, json=body | changes, headers={"Idempotency-Key": str(uuid4())}
                ).status_code in (400, 422)
            result = client.post(path, json=body, headers={"Idempotency-Key": key})
            result.raise_for_status()
            known = result.json()
            assert known["status"] == "blocked" and known["retry_basis"] == "known_reset"
            assert (
                _datetime(known["reset_at"]) == _datetime(known["next_eligible_at"]) == known_reset
            )
            assert known["recovered_at"] is None and known["probe_attempt_id"] is None

            cli_arguments = [
                "subscription-quota",
                "report-exhaustion",
                "--provider",
                "anthropic",
                "--account",
                "a8-local",
                "--pool",
                "unknown-reset",
                "--reason",
                "Operator observed a fixture limit with no reset time",
                "--idempotency-key",
                str(uuid4()),
            ]
            unknown = json.loads(harness.run_cli(cli_arguments))
            assert unknown["status"] == "blocked" and unknown["retry_basis"] == "probe_cooldown"
            assert unknown["reset_at"] is None and unknown["recovered_at"] is None
            assert (
                _datetime(unknown["next_eligible_at"]) - _datetime(unknown["observed_at"])
            ).total_seconds() == 60
            before = asyncio.run(_stored_evidence(test_database_url))
            assert before["observation_count"] == before["quota_audit_count"] == 2
            assert all(value == 0 for value in before["execution_counts"].values())

            # Keep the durable browser session; only the transport connection is new.
            cookies, headers = (
                client.cookies,
                {"Origin": harness.base_url, "X-CSRF-Token": client.headers["X-CSRF-Token"]},
            )
        harness.restart_api()
        harness.restart_worker()
        harness.assert_process_identities()
        with httpx.Client(
            base_url=harness.base_url, headers=headers, cookies=cookies, timeout=5
        ) as client:
            assert client.get("/api/auth/session").status_code == 200
            second_worker = _wait_worker(
                client, harness, previous={first_worker["worker_instance_id"]}
            )
            assert second_worker["routes"] == []
            registry = json.loads(harness.run_cli(["subscription-runtime", "status"]))
            assert {row["worker_instance_id"] for row in registry["workers"]} == {
                first_worker["worker_instance_id"],
                second_worker["worker_instance_id"],
            }
            deadline = time.monotonic() + 25
            while True:
                listing = client.get("/api/subscription-quota").json()
                retained = next(row for row in listing if row["pool"] == "known-reset")
                if retained["status"] == "eligible":
                    break
                assert time.monotonic() < deadline, "known reset did not become eligible"
                time.sleep(0.5)
            replay = client.post(path, json=body, headers={"Idempotency-Key": key})
            replay.raise_for_status()
            assert replay.json() == retained
            assert retained["revision"] == known["revision"] and retained["recovered_at"] is None
            assert retained["observed_at"] == known["observed_at"]
            assert retained["next_eligible_at"] == known["next_eligible_at"]
            conflict = client.post(
                path, json=body | {"reason": "A different report"}, headers={"Idempotency-Key": key}
            )
            assert conflict.status_code == 409
            assert json.loads(harness.run_cli(cli_arguments)) == unknown
            pages = [
                json.loads(
                    harness.run_cli(
                        ["subscription-quota", "list", "--offset", str(offset), "--limit", "1"]
                    )
                )
                for offset in (0, 1)
            ]
            assert {row["key"]["pool"] for page in pages for row in page} == {
                "known-reset",
                "unknown-reset",
            }
            assert client.get("/api/health").status_code == 200
            assert harness._worker_process.poll() is None
            after = asyncio.run(_stored_evidence(test_database_url))
            assert after == before
            evidence = {
                "scenario": "A8-keyless-operator",
                "proof": "Public API/worker/CLI processes with real PostgreSQL; no registered model adapters",
                "environment_presence": {name: name in harness._env for name in absent},
                "initial_process_ids": original_pids,
                "restarted_process_ids": {"api": harness.api_pid, "worker": harness.worker_pid},
                "initial_worker": first_worker,
                "restarted_worker": second_worker,
                "known_reset_before": known,
                "known_reset_after": retained,
                "unknown_reset": unknown,
                "durable_evidence": after,
                "provider_calls": False,
                "pricing_fixture_created": False,
                "limits": "No run execution, profile-freeze, browser rendering, live capability or billing-conformance proof",
            }
    assert all(process.poll() is not None for process in harness._processes)
    assert asyncio.run(_stored_evidence(test_database_url)) == before
    evidence["terminal_processes"] = [
        {"pid": process.pid, "return_code": process.returncode} for process in harness._processes
    ]
    _retain_evidence(evidence)


def _datetime(value):
    return datetime.fromisoformat(value)


def _wait_worker(client, harness, *, previous):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        assert harness._worker_process.poll() is None, "public worker exited before registration"
        result = client.get("/api/subscription-runtime")
        result.raise_for_status()
        for row in result.json()["workers"]:
            if row["state"] == "current" and row["worker_instance_id"] not in previous:
                return row
        time.sleep(0.1)
    raise AssertionError("public worker did not publish its registry")


async def _stored_evidence(database_url):
    engine = create_engine(database_url)
    try:
        async with engine.connect() as connection:
            observations = [
                dict(row)
                for row in (
                    await connection.execute(
                        text(
                            "SELECT provider, account, pool, observed_at, reset_at, next_eligible_at, retry_basis, reason, evidence_digest "
                            "FROM subscription_quota_observations ORDER BY provider, account, pool"
                        )
                    )
                ).mappings()
            ]
            audits = [
                dict(row)
                for row in (
                    await connection.execute(
                        text(
                            "SELECT actor_id, payload FROM operator_audit_events WHERE event_type = 'subscription.quota_exhaustion_reported' ORDER BY actor_id"
                        )
                    )
                ).mappings()
            ]
            counts = {}
            for table in (
                "subscription_attempts",
                "subscription_client_launches",
                "subscription_quota_admissions",
                "subscription_budget_reservations",
            ):
                counts[table] = await connection.scalar(text(f"SELECT count(*) FROM {table}"))
            return json.loads(
                json.dumps(
                    {
                        "observation_count": len(observations),
                        "quota_audit_count": len(audits),
                        "observations": observations,
                        "audits": audits,
                        "execution_counts": counts,
                    },
                    default=str,
                )
            )
    finally:
        await engine.dispose()


def _retain_evidence(evidence):
    body = json.dumps(
        {"evidence": evidence, "manifest_digest": canonical_digest(evidence)}, indent=2
    )
    assert_credential_free(body)
    if directory := os.environ.get("FORGE_ACCEPTANCE_OUTPUT_ROOT"):
        target = Path(directory) / str(uuid4())
        target.mkdir(parents=True)
        (target / "manifest.json").write_text(body + "\n", encoding="utf-8")
