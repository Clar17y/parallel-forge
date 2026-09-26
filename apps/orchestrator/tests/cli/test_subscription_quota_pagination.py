"""CLI callers can inspect durable quota pools beyond the first status page."""

import json

import pytest
from forge.cli import subscription_quota as cli
from forge.cli.main import app
from typer.testing import CliRunner


def test_cli_lists_a_later_quota_page(monkeypatch):
    calls = []

    class Service:
        async def list(self, *, offset=0, limit=100):
            calls.append((offset, limit))
            return [{"pool": f"pool-{index}"} for index in range(125)][offset : offset + limit]

    class Engine:
        disposed = False

        async def dispose(self):
            self.disposed = True

    engine = Engine()
    monkeypatch.setattr(cli, "_service", lambda: (Service(), engine))
    result = CliRunner().invoke(
        app, ["subscription-quota", "list", "--offset", "100", "--limit", "25"]
    )
    assert result.exit_code == 0, result.output
    assert calls == [(100, 25)] and engine.disposed
    assert [item["pool"] for item in json.loads(result.stdout)] == [
        f"pool-{index}" for index in range(100, 125)
    ]


@pytest.mark.parametrize("arguments", [["--offset", "-1"], ["--limit", "101"]])
def test_cli_rejects_invalid_quota_page_before_database_setup(monkeypatch, arguments):
    def unexpected_setup():
        pytest.fail("invalid page must not connect to PostgreSQL")

    monkeypatch.setattr(cli, "_service", unexpected_setup)
    result = CliRunner().invoke(app, ["subscription-quota", "list", *arguments])
    assert result.exit_code == 2
