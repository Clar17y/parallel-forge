import pytest
from forge.cli.main import app
from forge.settings import Settings
from typer.testing import CliRunner


def test_non_loopback_bind_requires_explicit_remote_mode() -> None:
    with pytest.raises(ValueError, match="remote exposure requires a later authentication design"):
        Settings(bind_host="0.0.0.0", allow_remote=False)


def test_cli_status_entrypoint_is_executable() -> None:
    result = CliRunner().invoke(app, ["status"])

    assert result.exit_code == 0
    assert result.stdout == "Forge CLI is ready.\n"
