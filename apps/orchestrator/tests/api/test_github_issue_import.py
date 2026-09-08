"""Issue import accepts only authenticated typed operator intent."""

from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_import_route_uses_closed_intent_and_bounded_errors(
    task10_client, task10_route_context, route_headers
):
    service = AsyncMock()
    service.import_issue.return_value = task10_route_context.task
    task10_route_context.app.state.github_issue_import_service = service
    body = {"project_id": str(task10_route_context.project.id), "issue_number": 42}
    headers = {**route_headers, "Idempotency-Key": "import-one"}
    response = await task10_client.post("/api/tasks/import-github", json=body, headers=headers)
    assert response.status_code == 201
    call = service.import_issue.call_args.kwargs
    assert call["request"].issue_number == 42
    assert call["request"].project_id == task10_route_context.project.id
    assert call["idempotency_key"] == "import-one"
    for invalid in (
        {**body, "repository": "attacker/repo"},
        {**body, "source_url": "https://evil.test"},
        {**body, "issue_number": True},
        {**body, "issue_number": 0},
    ):
        assert (
            await task10_client.post("/api/tasks/import-github", json=invalid, headers=headers)
        ).status_code == 422
    assert service.import_issue.await_count == 1
    service.import_issue.side_effect = RuntimeError("private-token-detail")
    failed = await task10_client.post("/api/tasks/import-github", json=body, headers=headers)
    assert failed.status_code == 503
    assert "private-token-detail" not in failed.text


@pytest.mark.asyncio
async def test_import_requires_session_csrf_and_key(
    task10_client, task10_route_context, route_headers
):
    service = AsyncMock()
    task10_route_context.app.state.github_issue_import_service = service
    body = {"project_id": str(task10_route_context.project.id), "issue_number": 42}
    missing_key = await task10_client.post(
        "/api/tasks/import-github", json=body, headers=route_headers
    )
    assert missing_key.status_code == 422
    missing_csrf = await task10_client.post(
        "/api/tasks/import-github",
        json=body,
        headers={"Idempotency-Key": "x", "Origin": route_headers["Origin"]},
    )
    assert missing_csrf.status_code == 403
    task10_client.cookies.clear()
    missing_session = await task10_client.post(
        "/api/tasks/import-github", json=body, headers={**route_headers, "Idempotency-Key": "x"}
    )
    assert missing_session.status_code == 401
    service.import_issue.assert_not_awaited()
