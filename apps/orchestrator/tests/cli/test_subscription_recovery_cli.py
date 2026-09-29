"""Local recovery commands preserve the preview binding and idempotency key."""

from uuid import uuid4

from forge.cli import subscription_tasks
from forge.cli.main import app
from typer.testing import CliRunner


def test_recovery_cli_routes_named_actions_through_shared_service(monkeypatch):
    calls = []

    async def fake(run_id, task_id, attempt_id, action, **values):
        calls.append((run_id, task_id, attempt_id, action.value, values))
        return '{"status":"applied"}'

    monkeypatch.setattr(subscription_tasks, "_recovery_execute", fake)
    run_id, task_id, attempt_id = uuid4(), uuid4(), uuid4()
    common = [
        "--action",
        "repair_approved_plan_contract",
        "--run-id",
        str(run_id),
        "--task-id",
        str(task_id),
        "--attempt-id",
        str(attempt_id),
    ]
    preview = CliRunner().invoke(app, ["subscription-tasks", "recovery-preview", *common])
    assert preview.exit_code == 0
    applied = CliRunner().invoke(
        app,
        [
            "subscription-tasks",
            "recover",
            *common,
            "--preview-token",
            "signed-token",
            "--reason",
            "Correct objective",
            "--idempotency-key",
            "operator-1",
        ],
    )
    assert applied.exit_code == 0
    assert calls == [
        (run_id, task_id, attempt_id, "repair_approved_plan_contract", {}),
        (
            run_id,
            task_id,
            attempt_id,
            "repair_approved_plan_contract",
            {
                "preview_token": "signed-token",
                "reason": "Correct objective",
                "idempotency_key": "operator-1",
            },
        ),
    ]
