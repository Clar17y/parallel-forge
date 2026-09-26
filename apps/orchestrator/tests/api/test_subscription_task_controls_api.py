"""Authenticated task-control transport preserves versions and causal receipts."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from forge.application.services.auth import AuthenticatedActor
from forge.domain.subscription_feedback import (
    SubscriptionTaskFeedbackRequest,
    TaskFeedbackConflict,
)
from forge.domain.subscription_task_controls import (
    SubscriptionTaskControlRequest,
    TaskControlConflict,
    TaskControlReceipt,
)


class FakeTaskControls:
    def __init__(self):
        self.calls = []
        self.error = None

    async def control(self, **values):
        self.calls.append(values)
        if self.error:
            raise self.error
        body = values["request"]
        assert isinstance(body, SubscriptionTaskControlRequest)
        assert isinstance(values["actor"], AuthenticatedActor)
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


class FakeTaskFeedback:
    def __init__(self):
        self.calls = []
        self.error = None

    async def submit(self, **values):
        self.calls.append(values)
        if self.error:
            raise self.error
        body = values["request"]
        assert isinstance(body, SubscriptionTaskFeedbackRequest)
        assert isinstance(values["actor"], AuthenticatedActor)
        primary_task_id = uuid4()
        return {
            "receipt_id": uuid4(),
            "operator_id": values["actor"].actor_id,
            "run_id": values["run_id"],
            "task_id": values["task_id"],
            "primary_task_id": primary_task_id,
            "status": "pending_primary",
            "run_version": body.expected_run_version,
            "task_version": body.expected_task_version,
            "primary_task_version": 7,
            "feedback_digest": "a" * 64,
            "binding_digest": "b" * 64,
            "feedback_bytes": len(body.feedback.encode("utf-8")),
            "observed_at": datetime(2026, 9, 16, tzinfo=UTC),
        }


@pytest.mark.asyncio
async def test_worker_feedback_route_is_authenticated_versioned_and_idempotent(
    task10_client, task10_route_context, route_headers
):
    service = FakeTaskFeedback()
    task10_route_context.app.state.subscription_task_feedback_service = service
    run_id, task_id = uuid4(), uuid4()
    response = await task10_client.post(
        f"/api/runs/{run_id}/subscription-tasks/{task_id}/feedback",
        json={
            "expected_run_version": 8,
            "expected_task_version": 4,
            "feedback": "Keep the parser change; add the missing replay assertion.",
        },
        headers={**route_headers, "Idempotency-Key": "worker-feedback-1"},
    )
    assert response.status_code == 200
    assert "feedback" not in response.json()
    assert "missing replay assertion" not in response.text
    call = service.calls[0]
    assert call["actor"] == task10_route_context.auth.actor
    assert call["idempotency_key"] == "worker-feedback-1"
    assert (call["run_id"], call["task_id"]) == (run_id, task_id)
    assert call["request"].feedback == ("Keep the parser change; add the missing replay assertion.")


@pytest.mark.asyncio
async def test_anonymous_worker_feedback_never_reaches_the_service(
    task10_client, task10_route_context, route_headers
):
    service = FakeTaskFeedback()
    task10_route_context.app.state.subscription_task_feedback_service = service
    task10_client.cookies.clear()
    response = await task10_client.post(
        f"/api/runs/{uuid4()}/subscription-tasks/{uuid4()}/feedback",
        json={
            "expected_run_version": 8,
            "expected_task_version": 4,
            "feedback": "Retain the partial work.",
        },
        headers={**route_headers, "Idempotency-Key": "anonymous-worker-feedback"},
    )
    assert response.status_code == 401 and not service.calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"expected_task_version": "4"},
        {"expected_run_version": True},
        {"unexpected": "field"},
        {"feedback": " "},
        {"feedback": "é" * 2049},
        {"feedback": "Authorization: Bearer abcdefghijklmnopqrstuvwxyz"},
    ],
)
async def test_worker_feedback_body_is_closed_bounded_and_credential_safe(
    task10_client, task10_route_context, route_headers, change
):
    service = FakeTaskFeedback()
    task10_route_context.app.state.subscription_task_feedback_service = service
    response = await task10_client.post(
        f"/api/runs/{uuid4()}/subscription-tasks/{uuid4()}/feedback",
        json={
            "expected_run_version": 8,
            "expected_task_version": 4,
            "feedback": "Retain the partial work.",
            **change,
        },
        headers={**route_headers, "Idempotency-Key": "invalid-worker-feedback"},
    )
    assert response.status_code == 422 and not service.calls
    assert "Bearer" not in response.text


@pytest.mark.asyncio
async def test_worker_feedback_conflicts_do_not_expose_untrusted_text(
    task10_client, task10_route_context, route_headers
):
    service = FakeTaskFeedback()
    service.error = TaskFeedbackConflict("untrusted provider diagnostic must stay private")
    task10_route_context.app.state.subscription_task_feedback_service = service
    response = await task10_client.post(
        f"/api/runs/{uuid4()}/subscription-tasks/{uuid4()}/feedback",
        json={
            "expected_run_version": 8,
            "expected_task_version": 4,
            "feedback": "Retain the partial work.",
        },
        headers={**route_headers, "Idempotency-Key": "conflicting-worker-feedback"},
    )
    assert response.status_code == 409
    assert "untrusted provider diagnostic" not in response.text


@pytest.mark.asyncio
async def test_anonymous_task_controls_never_reach_the_service(
    task10_client, task10_route_context, route_headers
):
    service = FakeTaskControls()
    task10_route_context.app.state.subscription_task_control_service = service
    task10_client.cookies.clear()
    response = await task10_client.post(
        f"/api/runs/{uuid4()}/subscription-tasks/{uuid4()}/controls",
        json={
            "action": "pause",
            "expected_run_version": 0,
            "expected_task_version": 0,
            "reason": "Inspect task",
        },
        headers={**route_headers, "Idempotency-Key": "anonymous-task-control"},
    )
    assert response.status_code == 401 and not service.calls


@pytest.mark.asyncio
async def test_task_control_route_requires_csrf_and_preserves_causal_resume(
    task10_client, task10_route_context, route_headers
):
    service = FakeTaskControls()
    task10_route_context.app.state.subscription_task_control_service = service
    run_id, task_id, pause_id = uuid4(), uuid4(), uuid4()
    path = f"/api/runs/{run_id}/subscription-tasks/{task_id}/controls"
    body = {
        "action": "resume",
        "expected_run_version": 8,
        "expected_task_version": 4,
        "reason": "Continue the retained decision",
        "pause_receipt_id": str(pause_id),
    }
    denied = await task10_client.post(
        path,
        json=body,
        headers={**route_headers, "Idempotency-Key": "task-resume-1", "X-CSRF-Token": "wrong"},
    )
    assert denied.status_code == 403 and not service.calls
    response = await task10_client.post(
        path, json=body, headers={**route_headers, "Idempotency-Key": "task-resume-1"}
    )
    assert response.status_code == 200
    assert response.json()["status"] == "decision_pending"
    assert response.json()["pause_receipt_id"] == str(pause_id)
    call = service.calls[0]
    assert call["idempotency_key"] == "task-resume-1"
    assert (call["run_id"], call["task_id"]) == (run_id, task_id)
    assert call["request"].pause_receipt_id == pause_id
    assert isinstance(call["request"].pause_receipt_id, UUID)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"expected_task_version": "4"},
        {"expected_run_version": True},
        {"unexpected": "field"},
        {"pause_receipt_id": str(uuid4())},
        {"action": "resume"},
        {"reason": " "},
        {"reason": "é" * 257},
    ],
)
async def test_task_control_body_is_closed_and_versions_are_strict(
    task10_client, task10_route_context, route_headers, change
):
    service = FakeTaskControls()
    task10_route_context.app.state.subscription_task_control_service = service
    body = {
        "action": "pause",
        "expected_run_version": 8,
        "expected_task_version": 4,
        "reason": "Inspect work",
        **change,
    }
    response = await task10_client.post(
        f"/api/runs/{uuid4()}/subscription-tasks/{uuid4()}/controls",
        json=body,
        headers={**route_headers, "Idempotency-Key": "task-invalid-1"},
    )
    assert response.status_code == 422 and not service.calls


@pytest.mark.asyncio
async def test_task_control_conflicts_are_safe_and_do_not_hide_stale_versions(
    task10_client, task10_route_context, route_headers
):
    service = FakeTaskControls()
    service.error = TaskControlConflict("untrusted diagnostic must not be returned")
    task10_route_context.app.state.subscription_task_control_service = service
    response = await task10_client.post(
        f"/api/runs/{uuid4()}/subscription-tasks/{uuid4()}/controls",
        json={
            "action": "cancel",
            "expected_run_version": 8,
            "expected_task_version": 4,
            "reason": "Cancel task",
        },
        headers={**route_headers, "Idempotency-Key": "task-cancel-1"},
    )
    assert response.status_code == 409
    assert "untrusted diagnostic" not in response.text
