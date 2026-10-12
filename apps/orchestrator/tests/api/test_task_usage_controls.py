"""Authenticated checkpoint routes translate stale policy revisions consistently."""

from uuid import uuid4

import pytest
from forge.application.services.epic_brainstorm import EpicBrainstormService
from forge.application.services.epic_decomposition import EpicDecompositionService
from forge.application.services.task_usage_controls import RunTaskUsageControls
from forge.persistence.repositories.task_usage_policy import MonitoringPolicyConflict


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("service_type", "state_name", "path"),
    (
        (RunTaskUsageControls, "run_task_usage_controls", "/api/runs/{subject}/usage-checkpoint/commands"),
        (EpicBrainstormService, "epic_brainstorm_service", "/api/epics/{epic}/brainstorm-jobs/{subject}/usage-checkpoint/commands"),
        (EpicDecompositionService, "epic_decomposition_service", "/api/epics/{epic}/decomposition-jobs/{subject}/usage-checkpoint/commands"),
    ),
)
async def test_stale_policy_conflict_is_http_409(
    task10_client, task10_route_context, route_headers, monkeypatch,
    service_type, state_name: str, path: str,
) -> None:
    project_id, epic_id, subject_id, checkpoint_id = (uuid4() for _ in range(4))
    setattr(task10_route_context.app.state, state_name, object.__new__(service_type))

    async def stale_policy(*_args, **_kwargs):
        raise MonitoringPolicyConflict("monitoring policy revision is stale")

    method = "apply" if service_type is RunTaskUsageControls else "apply_checkpoint_command"
    monkeypatch.setattr(service_type, method, stale_policy)
    response = await task10_client.post(
        path.format(epic=epic_id, subject=subject_id),
        params={"project_id": str(project_id)} if service_type is not RunTaskUsageControls else None,
        headers=route_headers,
        json={
            "checkpoint_id": str(checkpoint_id),
            "expected_version": 2,
            "idempotency_key": "stale-policy",
            "action": "set_policy",
            "requested_policy": {"project_id": str(project_id), "revision": 2, "mode": "checkpoint"},
        },
    )
    assert response.status_code == 409
    assert "stale" in response.json()["detail"]
