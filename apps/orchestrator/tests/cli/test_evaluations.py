"""CLI integration tests for evaluation suites."""

from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from click import unstyle
from forge.cli.main import app
from typer.testing import CliRunner

FIXTURES_ROOT = Path(__file__).resolve().parents[4] / "tests" / "evaluations" / "fixtures"
EXPECTED_ROOT = Path(__file__).resolve().parents[4] / "tests" / "evaluations" / "expected"


def test_cli_eval_help() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["eval", "--help"])
    assert result.exit_code == 0
    assert "run" in result.stdout

    result_run = runner.invoke(app, ["eval", "run", "--help"])
    assert result_run.exit_code == 0
    help_text = unstyle(result_run.stdout)
    assert "--suite" in help_text
    assert "--provider-reference" in help_text
    assert "--fixtures-dir" in help_text
    assert "--expected-dir" in help_text
    assert "--idempotency-key" in help_text


def test_cli_eval_run_live_without_provider_reference_exits_2(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("FORGE_DATA_ROOT", str(tmp_path / "forge_data"))
    runner = CliRunner()
    result = runner.invoke(app, ["eval", "run", "--suite", "live"])
    assert result.exit_code == 2
    assert "Error: live evaluation requires an explicit --provider-reference" in result.stderr


def test_cli_eval_run_deterministic_success(
    monkeypatch: pytest.MonkeyPatch, migrated_database_url: str, tmp_path: Path
) -> None:
    monkeypatch.setenv("FORGE_DATABASE_URL", migrated_database_url)
    monkeypatch.setenv("FORGE_DATA_ROOT", str(tmp_path / "forge_data"))

    runner = CliRunner()
    key = f"cli-test-{uuid4()}"
    result = runner.invoke(
        app,
        [
            "eval",
            "run",
            "--suite",
            "deterministic",
            "--idempotency-key",
            key,
            "--fixtures-dir",
            str(FIXTURES_ROOT),
            "--expected-dir",
            str(EXPECTED_ROOT),
        ],
    )
    assert result.exit_code == 0, f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    assert "PASSED" in result.stdout
    assert "planner/basic-change" in result.stdout
    assert "reviewer/missing-authorization" in result.stdout


def test_cli_eval_run_deterministic_replay_preserves_results(
    monkeypatch: pytest.MonkeyPatch, migrated_database_url: str, tmp_path: Path
) -> None:
    monkeypatch.setenv("FORGE_DATABASE_URL", migrated_database_url)
    monkeypatch.setenv("FORGE_DATA_ROOT", str(tmp_path / "forge_data"))

    runner = CliRunner()
    key = f"cli-replay-{uuid4()}"
    args = [
        "eval",
        "run",
        "--suite",
        "deterministic",
        "--idempotency-key",
        key,
        "--fixtures-dir",
        str(FIXTURES_ROOT),
        "--expected-dir",
        str(EXPECTED_ROOT),
    ]
    # First execution
    res1 = runner.invoke(app, args)
    assert res1.exit_code == 0

    # Replay execution
    res2 = runner.invoke(app, args)
    assert res2.exit_code == 0
    assert "PASSED" in res2.stdout


def test_cli_eval_run_deterministic_regression_failure_exits_1(
    monkeypatch: pytest.MonkeyPatch, migrated_database_url: str, tmp_path: Path
) -> None:
    monkeypatch.setenv("FORGE_DATABASE_URL", migrated_database_url)
    monkeypatch.setenv("FORGE_DATA_ROOT", str(tmp_path / "forge_data"))

    runner = CliRunner()
    key = f"cli-regress-{uuid4()}"
    result = runner.invoke(
        app,
        [
            "eval",
            "run",
            "--suite",
            "deterministic",
            "--idempotency-key",
            key,
            "--fixtures-dir",
            str(FIXTURES_ROOT),
            "--expected-dir",
            str(EXPECTED_ROOT),
            "--baseline-fixture-version",
            "mismatched-fixture-v0",
        ],
    )
    assert result.exit_code == 1
    assert "FAILED" in result.stdout


def test_cli_eval_promote_help() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["eval", "promote", "--help"])
    assert result.exit_code == 0
    help_text = unstyle(result.stdout)
    assert "--suite-id" in help_text
    assert "--name" in help_text
    assert "--floor" in help_text
    assert "--ceiling" in help_text
    assert "--promoted-by" in help_text


def test_cli_eval_promote_and_compare_lifecycle(
    monkeypatch: pytest.MonkeyPatch, migrated_database_url: str, tmp_path: Path
) -> None:
    import re

    monkeypatch.setenv("FORGE_DATABASE_URL", migrated_database_url)
    monkeypatch.setenv("FORGE_DATA_ROOT", str(tmp_path / "forge_data"))

    runner = CliRunner()
    key = f"cli-promote-{uuid4()}"
    run_res = runner.invoke(
        app,
        [
            "eval",
            "run",
            "--suite",
            "deterministic",
            "--idempotency-key",
            key,
            "--fixtures-dir",
            str(FIXTURES_ROOT),
            "--expected-dir",
            str(EXPECTED_ROOT),
        ],
    )
    assert run_res.exit_code == 0
    match = re.search(r"Evaluation suite 'deterministic' \(([a-f0-9\-]+)\): PASSED", run_res.stdout)
    assert match is not None
    suite_id = match.group(1)

    # Promote the suite
    promote_res = runner.invoke(
        app,
        [
            "eval",
            "promote",
            "--suite-id",
            suite_id,
            "--name",
            "live",
            "--floor",
            "tool_count=0.0",
            "--ceiling",
            "estimated_cost_minor=1000.0",
            "--promoted-by",
            "ci-operator",
        ],
    )
    assert promote_res.exit_code == 0
    assert "Promoted baseline 'live'" in promote_res.stdout
    assert "ci-operator" not in promote_res.stderr

    # Attempt to promote non-existent suite -> exit code 1
    bad_res = runner.invoke(
        app,
        [
            "eval",
            "promote",
            "--suite-id",
            str(uuid4()),
        ],
    )
    assert bad_res.exit_code == 1
    assert "Baseline promotion error" in bad_res.stderr


def test_cli_eval_run_live_with_mocked_gateway(
    monkeypatch: pytest.MonkeyPatch, migrated_database_url: str, tmp_path: Path
) -> None:
    from functools import partial
    from unittest.mock import AsyncMock, patch

    from forge.agents.fake_gateway import FakeAgentGateway, FakeAgentStep
    from forge.application.services.evaluations import EvaluationService
    from forge.domain.actor import AgentRole
    from forge.domain.agent import DeveloperOutput, ReviewOutput
    from forge.domain.plan import PlanOutput
    from forge.evaluations.loader import load_expected_output

    monkeypatch.setenv("FORGE_DATABASE_URL", migrated_database_url)
    monkeypatch.setenv("FORGE_DATA_ROOT", str(tmp_path / "forge_data"))

    # This isolated CLI boundary test explicitly designates trusted-host fixtures;
    # production evaluation construction keeps the Docker default.
    monkeypatch.setattr(
        "forge.cli.evaluations.EvaluationService",
        partial(EvaluationService, trusted_fixture_execution=True),
    )

    plan_data = load_expected_output(EXPECTED_ROOT / "planner-basic-change.json")
    review_data = load_expected_output(EXPECTED_ROOT / "reviewer-missing-authorization.json")
    dev_data = load_expected_output(EXPECTED_ROOT / "developer-basic-change.json")
    fake_gw = FakeAgentGateway(
        {
            AgentRole.PLANNER: [FakeAgentStep.success(PlanOutput.model_validate(plan_data), cost_minor=5)],
            AgentRole.REVIEWER: [FakeAgentStep.success(ReviewOutput.model_validate(review_data), cost_minor=5)],
            AgentRole.DEVELOPER: [FakeAgentStep.success(DeveloperOutput.model_validate(dev_data), cost_minor=5)],
        }
    )

    fake_gw.evaluation_provider = "test"
    runner = CliRunner()
    key = f"cli-live-mock-{uuid4()}"

    with patch(
        "forge.application.services.evaluations.resolve_live_evaluation_gateway",
        return_value=fake_gw,
    ), patch(
        "forge.tools.provider_credentials.LocalProviderCredentialResolver.resolve",
        new=AsyncMock(return_value="mock-key"),
    ):
        # 1. Live evaluation without baseline selection is nonblocking -> exit code 0
        res = runner.invoke(
            app,
            [
                "eval",
                "run",
                "--suite",
                "live",
                "--provider-reference",
                "secret://forge/google_ai_studio_api_key",
                "--model",
                "mock-model",
                "--idempotency-key",
                key,
                "--fixtures-dir",
                str(FIXTURES_ROOT),
                "--expected-dir",
                str(EXPECTED_ROOT),
            ],
        )
        assert res.exit_code == 0, f"STDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
        assert "Evaluation suite 'live'" in res.stdout

        # 2. Live evaluation with --promoted-baseline when no baseline exists -> exit code 1
        res_fail = runner.invoke(
            app,
            [
                "eval",
                "run",
                "--suite",
                "live",
                "--provider-reference",
                "secret://forge/google_ai_studio_api_key",
                "--model",
                "mock-model",
                "--idempotency-key",
                f"live-no-base-{uuid4()}",
                "--promoted-baseline",
                "--fixtures-dir",
                str(FIXTURES_ROOT),
                "--expected-dir",
                str(EXPECTED_ROOT),
            ],
        )
        assert res_fail.exit_code == 1
        assert "no persisted baseline found" in res_fail.stderr


def test_cli_eval_secret_store_receives_data_root_not_double_path(
    monkeypatch: pytest.MonkeyPatch, migrated_database_url: str, tmp_path: Path
) -> None:
    from unittest.mock import patch

    monkeypatch.setenv("FORGE_DATABASE_URL", migrated_database_url)
    data_root = tmp_path / "forge_data"
    monkeypatch.setenv("FORGE_DATA_ROOT", str(data_root))

    captured_root: list[Path] = []
    from forge.tools.secrets import LocalSecretStore

    orig_init = LocalSecretStore.__init__

    def spy_init(self: Any, base_path: Path) -> None:
        captured_root.append(base_path)
        orig_init(self, base_path)

    runner = CliRunner()
    with patch.object(LocalSecretStore, "__init__", spy_init):
        runner.invoke(
            app,
            [
                "eval",
                "run",
                "--suite",
                "live",
                "--provider-reference",
                "secret://forge/test_ref",
                "--model",
                "mock-model",
                "--fixtures-dir",
                str(FIXTURES_ROOT),
                "--expected-dir",
                str(EXPECTED_ROOT),
            ],
        )
    assert len(captured_root) >= 1
    # Verify it was passed data_root directly, NOT data_root / "secrets" (which would result in double path)
    assert captured_root[0] == data_root
    assert not str(captured_root[0]).endswith("secrets")


def test_live_cli_loads_operator_pricing_catalog(monkeypatch, tmp_path):
    import json
    from unittest.mock import AsyncMock

    from forge.cli import evaluations as cli
    from forge.persistence.repositories.evaluations import EvaluationConflict

    catalog = tmp_path / "pricing.json"
    catalog.write_text(json.dumps({"version": "operator-prices-v1", "entries": {
        "google:test-model": {"input_per_million": "1", "output_per_million": "2",
                              "cached_input_per_million": "0.5"}
    }}), encoding="utf-8")
    monkeypatch.setenv("FORGE_PRICING_CATALOG_PATH", str(catalog))
    monkeypatch.setenv("FORGE_DATA_ROOT", str(tmp_path / "runtime"))
    captured = {}
    class Service:
        def __init__(self, **kwargs):
            captured.update(kwargs)
        async def run_suite(self, **kwargs):
            raise EvaluationConflict("admission stopped by test")
    monkeypatch.setattr(cli, "EvaluationService", Service)
    monkeypatch.setattr(cli, "create_engine", lambda _: AsyncMock())
    monkeypatch.setattr(cli, "create_session_factory", lambda _: object())
    result = CliRunner().invoke(app, ["eval", "run", "--suite", "live",
        "--provider-reference", "secret://forge/google_ai_studio_api_key",
        "--model", "test-model"])
    assert result.exit_code == 1
    assert captured["pricing_catalog"].version == "operator-prices-v1"
