"""Historical policy reads retain the exact evidence bound to earlier runs."""

from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from forge.persistence.repositories.projects import PolicyNotFound


@pytest.mark.asyncio
async def test_reads_requested_policy_version_without_returning_current_policy(
    task10_client, task10_route_context, route_headers, monkeypatch
) -> None:
    project = task10_route_context.project
    historical = replace(project.policy, version=2, policy_digest="c" * 64)
    read = AsyncMock(return_value=historical)
    monkeypatch.setattr(task10_route_context.projects, "get_policy", read, raising=False)
    response = await task10_client.get(
        f"/api/projects/{project.id}/policy-versions/2", headers={"Host": route_headers["Host"]}
    )
    assert response.status_code == 200
    assert response.json()["version"] == 2
    assert response.json()["policy_digest"] == "c" * 64
    read.assert_awaited_once_with(project.id, 2)


@pytest.mark.asyncio
async def test_policy_history_requires_auth_and_valid_version_and_bounds_missing_errors(
    task10_client, task10_route_context, route_headers, monkeypatch
) -> None:
    project = task10_route_context.project
    read = AsyncMock(side_effect=PolicyNotFound("private database details"))
    monkeypatch.setattr(task10_route_context.projects, "get_policy", read, raising=False)
    path = f"/api/projects/{project.id}/policy-versions"
    headers = {"Host": route_headers["Host"]}
    invalid = await task10_client.get(f"{path}/0", headers=headers)
    assert invalid.status_code == 422
    read.assert_not_awaited()
    missing = await task10_client.get(f"{path}/99", headers=headers)
    assert missing.status_code == 404
    assert "private database" not in missing.text
    read.reset_mock()
    task10_client.cookies.clear()
    denied = await task10_client.get(f"{path}/1", headers=headers)
    assert denied.status_code == 401
    read.assert_not_awaited()
