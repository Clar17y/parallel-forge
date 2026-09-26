"""Focused tests for the local profile command boundary."""

import asyncio
import json
from pathlib import Path
from uuid import uuid4

import pytest
from forge.cli import subscription_profiles as cli
from forge.cli.main import app
from typer.testing import CliRunner


class _Engine:
    def __init__(self) -> None:
        self.disposed = False

    async def dispose(self) -> None:
        self.disposed = True


class _Service:
    def __init__(self) -> None:
        self.actor = None

    async def create(self, **kwargs):
        self.actor = kwargs["actor"]
        return {"profile_id": uuid4(), "version": 1}


def test_profile_help_is_read_only_and_does_not_construct_database() -> None:
    result = CliRunner().invoke(app, ["profile", "--help"])
    assert result.exit_code == 0
    assert "project-select" in result.stdout


def test_create_uses_stable_local_actor_and_disposes_engine(
    tmp_path: Path, monkeypatch
) -> None:
    payload = {
        "preferences": [
            {
                "purpose": "primary",
                "preferred_route": {
                    "provider": "openai",
                    "client": "codex",
                    "model": "astra",
                },
            }
        ]
    }
    input_file = tmp_path / "profile.json"
    input_file.write_text(json.dumps(payload), encoding="utf-8")
    service = _Service()
    engine = _Engine()
    monkeypatch.setattr(cli, "_service", lambda: (service, engine))

    result = CliRunner().invoke(
        app,
        [
            "profile", "create", "--file", str(input_file),
            "--idempotency-key", "create-1",
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert service.actor.actor_id == cli.LocalOperatorProfileActor().actor_id
    assert not hasattr(service.actor, "session_id")
    assert engine.disposed


def test_malformed_duplicate_and_oversized_input_have_safe_errors(tmp_path: Path, monkeypatch) -> None:
    engine = _Engine()
    monkeypatch.setattr(cli, "_service", lambda: (_Service(), engine))
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"preferences": [], "preferences": []}', encoding="utf-8")
    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b"{" + b"x" * (256 * 1024) + b"}")

    runner = CliRunner()
    base = ["profile", "create", "--idempotency-key", "bad"]
    duplicate_result = runner.invoke(app, [*base, "--file", str(duplicate)])
    oversized_result = runner.invoke(app, [*base, "--file", str(oversized)])

    assert duplicate_result.exit_code != 0
    assert "invalid profile input" in duplicate_result.output
    assert "preferences" not in duplicate_result.output
    assert oversized_result.exit_code != 0
    assert "invalid profile input" in oversized_result.output
    assert str(oversized) not in oversized_result.output


def test_nonfinite_unknown_and_invalid_fields_have_safe_errors(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(cli, "_service", lambda: (_Service(), _Engine()))
    cases = {
        "nan.json": '{"preferences": [], "value": NaN}',
        "infinity.json": '{"preferences": [], "value": Infinity}',
        "unknown.json": '{"preferences": [], "unexpected": true}',
        "invalid.json": '{"preferences": "not-a-list"}',
    }
    runner = CliRunner()
    for name, content in cases.items():
        path = tmp_path / name
        path.write_text(content, encoding="utf-8")
        result = runner.invoke(
            app, ["profile", "create", "--file", str(path), "--idempotency-key", name]
        )
        assert result.exit_code != 0
        assert "invalid profile input" in result.output or "profile input failed schema validation" in result.output
        assert "NaN" not in result.output and "not-a-list" not in result.output


def test_all_profile_commands_have_help() -> None:
    runner = CliRunner()
    for command in ("list", "show", "create", "append", "project-show", "project-select"):
        result = runner.invoke(app, ["profile", command, "--help"])
        assert result.exit_code == 0, result.output


def test_engine_disposes_on_setup_failure_action_failure_and_cancellation(monkeypatch) -> None:
    setup_engine = _Engine()
    monkeypatch.setattr(cli, "create_engine", lambda _: setup_engine)
    monkeypatch.setattr(cli, "create_session_factory", lambda _: (_ for _ in ()).throw(RuntimeError("secret")))
    with pytest.raises(cli._SetupError):
        asyncio.run(cli._run(lambda service: service.list()))
    assert setup_engine.disposed

    action_engine = _Engine()
    monkeypatch.setattr(cli, "_service", lambda: (_Service(), action_engine))
    async def fail(service):
        raise RuntimeError("injected secret")
    with pytest.raises(RuntimeError, match="injected secret"):
        asyncio.run(cli._run(fail))
    assert action_engine.disposed

    cancel_engine = _Engine()
    monkeypatch.setattr(cli, "_service", lambda: (_Service(), cancel_engine))
    async def cancel(service):
        raise asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(cli._run(cancel))
    assert cancel_engine.disposed


def test_injected_secret_is_not_echoed(monkeypatch) -> None:
    class SecretService:
        async def list(self):
            raise RuntimeError("database password SUPER_SECRET")
    monkeypatch.setattr(cli, "_service", lambda: (SecretService(), _Engine()))
    result = CliRunner().invoke(app, ["profile", "list"])
    assert result.exit_code != 0
    assert "SUPER_SECRET" not in result.output
    assert "profile operation failed" in result.output


def test_engine_disposes_when_service_construction_fails(monkeypatch) -> None:
    engine = _Engine()
    monkeypatch.setattr(cli, "create_engine", lambda _: engine)
    monkeypatch.setattr(cli, "create_session_factory", lambda _: object())

    def fail_construction(factory):
        raise RuntimeError("private configuration")

    monkeypatch.setattr(cli, "SubscriptionProfileService", fail_construction)
    with pytest.raises(cli._SetupError):
        asyncio.run(cli._run(lambda service: service.list()))
    assert engine.disposed
