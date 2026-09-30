"""The local run commands keep stable operator identity and bounded JSON output."""

import json
from uuid import uuid4

from forge.cli import runs as cli
from forge.cli.main import app
from typer.testing import CliRunner


class _Engine:
    disposed = False

    async def dispose(self):
        self.disposed = True


class _Service:
    def __init__(self, *_args, **_kwargs):
        self.actor = None
        self.selection = {"profile_id": "profile", "profile_version": 2, "selection_source": "run_override"}
        self.run = None

    async def create_run(self, **kwargs):
        self.actor = kwargs["actor"]
        self.kwargs = kwargs
        self.run = type("Run", (), {
            "id": uuid4(), "project_id": uuid4(), "task_id": kwargs["task_id"],
            "state": type("State", (), {"value": "CREATED"})(), "version": 0,
        })()
        return self.run

    async def get(self, _run_id):
        return self.run

    async def profile_selection(self, _run_id):
        return self.selection


def _setup(monkeypatch):
    engine = _Engine()
    service = _Service()
    monkeypatch.setattr(cli, "Settings", lambda **_kwargs: type("Settings", (), {"database_url": "postgresql://test", "data_root": "."})())
    monkeypatch.setattr(cli, "create_engine", lambda _url: engine)
    monkeypatch.setattr(cli, "create_session_factory", lambda _engine: object())
    monkeypatch.setattr(cli, "PostgresUnitOfWork", lambda _sessions: None)
    monkeypatch.setattr(cli, "RunService", lambda *_args, **_kwargs: service)
    return engine, service


def test_create_accepts_atomic_profile_pair_and_disposes_engine(monkeypatch):
    engine, service = _setup(monkeypatch)
    task_id, profile_id = uuid4(), uuid4()
    result = CliRunner().invoke(app, [
        "run", "create", "--task-id", str(task_id), "--idempotency-key", "cli-run",
        "--profile-id", str(profile_id), "--profile-version", "2",
    ])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["subscription_profile"]["selection_source"] == "run_override"
    assert service.kwargs["task_id"] == task_id
    assert service.kwargs["profile_id"] == profile_id
    assert service.kwargs["profile_version"] == 2
    assert service.actor.actor_id == cli.LocalOperatorProfileActor().actor_id
    assert not hasattr(service.actor, "session_id")
    assert engine.disposed


def test_create_rejects_incomplete_profile_pair(monkeypatch):
    _engine, _service = _setup(monkeypatch)
    result = CliRunner().invoke(app, [
        "run", "create", "--task-id", str(uuid4()), "--idempotency-key", "cli-run",
        "--profile-id", str(uuid4()),
    ])
    assert result.exit_code != 0
    assert "profile identity and version" in result.output
    assert not _engine.disposed


def test_profileless_create_and_show_emit_no_selection(monkeypatch):
    _engine, service = _setup(monkeypatch)
    service.selection = None
    runner = CliRunner()
    created = runner.invoke(app, [
        "run", "create", "--task-id", str(uuid4()), "--idempotency-key", "legacy-run",
    ])
    assert created.exit_code == 0, created.output
    assert json.loads(created.stdout)["subscription_profile"] is None
    shown = runner.invoke(app, ["run", "show", "--run-id", str(service.run.id)])
    assert shown.exit_code == 0, shown.output
    assert json.loads(shown.stdout)["subscription_profile"] is None
