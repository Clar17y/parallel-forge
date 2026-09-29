"""Recovery HTTP transport enforces operator session, CSRF and idempotency."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from forge.application.services.auth import AuthenticatedActor
from forge.domain.subscription_recovery import (
    RecoveryApplyRequest,
    RecoveryBudgetImpact,
    RecoveryPreview,
    RecoveryPreviewRequest,
    RecoveryReceipt,
)


class FakeRecovery:
    def __init__(self):
        self.previews = []
        self.applies = []

    async def preview(self, **values):
        self.previews.append(values)
        assert isinstance(values["actor"], AuthenticatedActor)
        assert isinstance(values["request"], RecoveryPreviewRequest)
        return RecoveryPreview(
            run_id=values["run_id"],
            task_id=values["task_id"],
            attempt_id=values["attempt_id"],
            action=values["request"].action,
            eligible=True,
            reason_code="eligible",
            message="Ready",
            changes=("Queue one attempt",),
            retained_evidence=("Retain result",),
            budget_impact=RecoveryBudgetImpact(provider_attempts=1, repair_units=1),
            preview_token="signed-preview",
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )

    async def apply(self, **values):
        self.applies.append(values)
        assert isinstance(values["actor"], AuthenticatedActor)
        assert isinstance(values["request"], RecoveryApplyRequest)
        return RecoveryReceipt(
            receipt_id=uuid4(),
            run_id=values["run_id"],
            task_id=values["task_id"],
            attempt_id=values["attempt_id"],
            action=values["request"].action,
            status="applied",
            observed_at=datetime.now(UTC),
            reason_code="applied",
        )


@pytest.mark.asyncio
async def test_recovery_routes_bind_identity_auth_and_csrf(
    task10_client,
    task10_route_context,
    route_headers,
):
    fake = FakeRecovery()
    task10_route_context.app.state.subscription_recovery_service = fake
    run_id, task_id, attempt_id = uuid4(), uuid4(), uuid4()
    url = f"/api/runs/{run_id}/subscription-tasks/{task_id}/attempts/{attempt_id}/recovery"
    preview = await task10_client.post(
        url + "/preview",
        json={"action": "repair_approved_plan_contract"},
        headers=route_headers,
    )
    assert preview.status_code == 200
    assert preview.json()["run_id"] == str(run_id)
    assert fake.previews[0]["actor"] == task10_route_context.auth.actor
    missing_key = await task10_client.post(
        url,
        json={
            "action": "repair_approved_plan_contract",
            "preview_token": "signed-preview",
            "reason": "Correct objective",
        },
        headers=route_headers,
    )
    assert missing_key.status_code in {400, 422} and not fake.applies
    applied = await task10_client.post(
        url,
        json={
            "action": "repair_approved_plan_contract",
            "preview_token": "signed-preview",
            "reason": "Correct objective",
        },
        headers={**route_headers, "Idempotency-Key": "recovery-key"},
    )
    assert applied.status_code == 200
    assert fake.applies[0]["idempotency_key"] == "recovery-key"
    assert fake.applies[0]["actor"] == task10_route_context.auth.actor
    task10_client.cookies.clear()
    anonymous = await task10_client.post(
        url + "/preview",
        json={"action": "retry_application"},
    )
    assert anonymous.status_code == 401 and len(fake.previews) == 1
