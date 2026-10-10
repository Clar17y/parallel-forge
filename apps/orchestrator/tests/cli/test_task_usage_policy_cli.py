"""CLI uses the same versioned owner action as HTTP."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from forge.application.services.task_usage_policy import MonitoringPolicyRevisionResult
from forge.cli import task_usage_policy as cli
from forge.cli.main import app
from forge.domain.task_usage_contract import MonitoringPolicy
from typer.testing import CliRunner


def test_cli_revise_uses_stable_operator_and_explicit_clear(tmp_path: Path, monkeypatch) -> None:
    project_id = uuid4()
    input_file = tmp_path / "policy.json"
    input_file.write_text('{"expected_revision": 1, "hard_token_cap": null}', encoding="utf-8")
    calls = []

    class Service:
        async def revise(self, **kwargs):
            calls.append(kwargs)
            return MonitoringPolicyRevisionResult(
                policy=MonitoringPolicy(project_id=project_id, revision=2),
                actor_id=kwargs["actor"].actor_id,
                affected_snapshot="a" * 64,
            )

    async def run(operation):
        return await operation(Service())

    monkeypatch.setattr(cli, "_run", run)
    result = CliRunner().invoke(
        app,
        [
            "monitoring-policy",
            "revise",
            "--project-id",
            str(project_id),
            "--file",
            str(input_file),
            "--idempotency-key",
            "clear-1",
        ],
    )
    assert result.exit_code == 0, result.output
    assert calls[0]["actor"].actor_class == "operator"
    assert calls[0]["idempotency_key"] == "clear-1"
    assert calls[0]["request"].model_fields_set == {"expected_revision", "hard_token_cap"}
    assert '"revision": 2' in result.output


def test_cli_rejects_duplicate_fields_without_invoking_service(tmp_path: Path, monkeypatch) -> None:
    input_file = tmp_path / "policy.json"
    input_file.write_text('{"expected_revision": 1, "expected_revision": 2}', encoding="utf-8")

    async def run(_operation):
        raise AssertionError("service should not run")

    monkeypatch.setattr(cli, "_run", run)
    result = CliRunner().invoke(
        app,
        [
            "monitoring-policy",
            "revise",
            "--project-id",
            str(uuid4()),
            "--file",
            str(input_file),
            "--idempotency-key",
            "bad",
        ],
    )
    assert result.exit_code != 0
    assert "monitoring policy input is invalid" in result.output
