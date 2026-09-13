"""Fake local clients prove typed, attributed controls and safe cleanup/output."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from forge.application.services.subscription_profiles import LocalOperatorProfileActor
from forge.cli import subscription_tasks
from forge.cli.main import app
from forge.domain.subscription_task_controls import TaskControlConflict, TaskControlReceipt
from typer.testing import CliRunner


class Engine:
    def __init__(self):
        self.disposed = 0

    async def dispose(self):
        self.disposed += 1


def setup_cli(monkeypatch, *, error=None):
    engine, calls = Engine(), []

    async def control(**values):
        calls.append(values)
        if error:
            raise error
        body = values["request"]
        return TaskControlReceipt(
            receipt_id=uuid4(),
            run_id=values["run_id"],
            task_id=values["task_id"],
            action=body.action,
            status={
                "pause": "pause_requested",
                "cancel": "cancel_requested",
                "resume": "decision_pending",
            }[body.action],
            run_version=body.expected_run_version,
            task_version=body.expected_task_version + 1,
            observed_at=datetime(2026, 9, 12, tzinfo=UTC),
            reason=body.reason,
            pause_receipt_id=body.pause_receipt_id,
        )

    monkeypatch.setattr(
        subscription_tasks, "Settings", lambda **_: SimpleNamespace(database_url="unused")
    )
    monkeypatch.setattr(subscription_tasks, "create_engine", lambda _: engine)
    monkeypatch.setattr(subscription_tasks, "_service", lambda *_: SimpleNamespace(control=control))
    return engine, calls


def arguments(action="pause"):
    return [
        "subscription-tasks",
        "control",
        "--action",
        action,
        "--run-id",
        str(uuid4()),
        "--task-id",
        str(uuid4()),
        "--expected-run-version",
        "8",
        "--expected-task-version",
        "4",
        "--reason",
        "Inspect work",
        "--idempotency-key",
        "operator-1",
    ]


@pytest.mark.parametrize("action", ["pause", "cancel", "resume"])
def test_local_operator_calls_same_control_service_and_keeps_receipt(monkeypatch, action):
    engine, calls = setup_cli(monkeypatch)
    argv = arguments(action)
    pause_id = uuid4()
    if action == "resume":
        argv += ["--pause-receipt-id", str(pause_id)]
    result = CliRunner().invoke(app, argv)
    assert result.exit_code == 0, result.output
    value = json.loads(result.stdout)
    assert value["action"] == action and value["task_version"] == 5
    assert isinstance(calls[0]["actor"], LocalOperatorProfileActor)
    assert calls[0]["idempotency_key"] == "operator-1"
    assert calls[0]["request"].expected_run_version == 8
    if action == "resume":
        assert value["pause_receipt_id"] == str(pause_id)
    assert engine.disposed == 1


def test_missing_pause_receipt_is_rejected_before_opening_resources(monkeypatch):
    engine, calls = setup_cli(monkeypatch)
    result = CliRunner().invoke(app, arguments("resume"))
    assert result.exit_code == 1 and "request rejected" in result.output
    assert not calls and engine.disposed == 0


@pytest.mark.parametrize(
    "error", [TaskControlConflict("secret diagnostic"), RuntimeError("secret diagnostic")]
)
def test_failure_disposes_engine_without_echoing_untrusted_details(monkeypatch, error):
    engine, calls = setup_cli(monkeypatch, error=error)
    result = CliRunner().invoke(app, arguments())
    assert result.exit_code == 1 and len(calls) == 1 and engine.disposed == 1
    assert "secret diagnostic" not in result.output
