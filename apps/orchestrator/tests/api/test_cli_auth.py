"""CLI authentication output contract."""

from __future__ import annotations

from forge.cli import main as cli_main
from forge.cli.main import app
from typer.testing import CliRunner


def test_operator_rotate_prints_only_bootstrap_fragment(monkeypatch) -> None:
    async def fake_rotate(_settings) -> str:
        return "bootstrap-raw"

    monkeypatch.setattr(cli_main, "_rotate", fake_rotate)
    result = CliRunner().invoke(app, ["operator", "rotate"])

    assert result.exit_code == 0
    assert result.stdout == "http://127.0.0.1:3000/#bootstrap=bootstrap-raw\n"
    assert "session" not in result.stdout
    assert "csrf" not in result.stdout
    assert "challenge" not in result.stdout
