"""Runtime inspection is bounded, read-only and credential-free on failure."""

from datetime import UTC, datetime

from forge.api.schemas.subscription_runtime import SubscriptionRuntimeStatusPage
from forge.cli import subscription_runtime as cli
from forge.cli.main import app
from typer.testing import CliRunner


def test_runtime_cli_prints_unknown_inventory_and_forwards_bounds(monkeypatch):
    calls = []

    async def query(offset, limit):
        calls.append((offset, limit))
        return SubscriptionRuntimeStatusPage(
            observed_at=datetime(2026, 9, 12, tzinfo=UTC),
            fresh_for_seconds=45,
            workers=[],
            has_more=False,
        )

    monkeypatch.setattr(cli, "_status", query)
    result = CliRunner().invoke(
        app, ["subscription-runtime", "status", "--offset", "2", "--limit", "10"]
    )
    assert result.exit_code == 0 and '"workers":[]' in result.output
    assert calls == [(2, 10)]
    assert (
        CliRunner().invoke(app, ["subscription-runtime", "status", "--limit", "101"]).exit_code != 0
    )
    assert len(calls) == 1


def test_runtime_cli_failure_does_not_echo_connection_values(monkeypatch):
    async def query(*_args):
        raise ValueError("password=fixture-secret")

    monkeypatch.setattr(cli, "_status", query)
    result = CliRunner().invoke(app, ["subscription-runtime", "status"])
    assert result.exit_code == 1 and "unavailable" in result.output
    assert "fixture-secret" not in result.output
