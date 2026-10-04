"""Operator authentication and CSRF around a settled decomposition proposal."""

from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from forge.api.routes.epic_decomposition import router_for
from forge.settings import Settings
from httpx import ASGITransport, AsyncClient

from apps.orchestrator.tests.api.conftest import FakeRouteAuthService

from .test_persistence import prepared, settle, submit


@pytest.mark.asyncio
async def test_decomposition_api_auth_csrf_and_adopt(decomposition_session_factory) -> None:
    factory = decomposition_session_factory
    service, actor, epic_id, project_id = await prepared(factory)
    _, _, _, receipt = await submit(service, actor, epic_id, project_id)
    await settle(factory, receipt.job_id)
    outcome = await service.observe(epic_id=epic_id, project_id=project_id, job_id=receipt.job_id)

    app = FastAPI()
    app.state.settings = Settings(web_origin="http://127.0.0.1:3000")
    app.state.auth_service = FakeRouteAuthService()
    app.state.epic_decomposition_service = service
    app.include_router(router_for(), prefix="/api")
    headers = {
        "Host": "127.0.0.1:3000", "Origin": "http://127.0.0.1:3000",
        "X-CSRF-Token": "route-csrf-token", "Idempotency-Key": "api-adopt",
    }
    path = f"/api/epics/{epic_id}/decomposition-jobs/{receipt.job_id}/adopt"
    payload = {
        "project_id": str(project_id), "expected_job_version": outcome.job_version,
        "expected_epic_version": 1, "proposal_digest": outcome.proposal_digest,
    }
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://127.0.0.1:3000"
    ) as client:
        assert (await client.post(path, json=payload, headers=headers)).status_code == 401
        client.cookies.set("forge_session", app.state.auth_service.session_token)
        bad_csrf = await client.post(path, json=payload, headers={**headers, "X-CSRF-Token": "bad"})
        assert bad_csrf.status_code == 403
        success = await client.post(path, json=payload, headers=headers)
        assert success.status_code == 200
        assert success.json()["epic_version"] == 3
        stale = await client.post(
            path, json=payload, headers={**headers, "Idempotency-Key": "other-key"}
        )
        assert stale.status_code == 409


@pytest.mark.asyncio
async def test_adoption_api_replay_changed_key_and_missing_epic(decomposition_session_factory) -> None:
    factory = decomposition_session_factory
    service, actor, epic_id, project_id = await prepared(factory)
    _, _, _, receipt = await submit(service, actor, epic_id, project_id)
    await settle(factory, receipt.job_id)
    outcome = await service.observe(epic_id=epic_id, project_id=project_id, job_id=receipt.job_id)
    app = FastAPI()
    app.state.settings = Settings(web_origin="http://127.0.0.1:3000")
    app.state.auth_service = FakeRouteAuthService()
    app.state.epic_decomposition_service = service
    app.include_router(router_for(), prefix="/api")
    headers = {
        "Host": "127.0.0.1:3000", "Origin": "http://127.0.0.1:3000",
        "X-CSRF-Token": "route-csrf-token", "Idempotency-Key": "api-replay",
    }
    path = f"/api/epics/{epic_id}/decomposition-jobs/{receipt.job_id}/adopt"
    payload = {
        "project_id": str(project_id), "expected_job_version": outcome.job_version,
        "expected_epic_version": 1, "proposal_digest": outcome.proposal_digest,
    }
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://127.0.0.1:3000",
    ) as client:
        client.cookies.set("forge_session", app.state.auth_service.session_token)
        first = await client.post(path, json=payload, headers=headers)
        assert first.status_code == 200
        replay = await client.post(path, json=payload, headers=headers)
        assert replay.status_code == 200 and replay.json() == first.json()
        edited = outcome.proposal.items[0].model_copy(update={"title": "Edited after adoption"})
        conflict = await client.post(
            path, json={**payload, "items": [edited.model_dump(mode="json")]}, headers=headers,
        )
        assert conflict.status_code == 409
        missing = await client.post(
            f"/api/epics/{uuid4()}/decomposition-jobs/{receipt.job_id}/adopt",
            json=payload, headers={**headers, "Idempotency-Key": "unknown-epic"},
        )
        assert missing.status_code == 404
    observed = await service.observe(epic_id=epic_id, project_id=project_id, job_id=receipt.job_id)
    assert observed.adopted_revision_id == UUID(first.json()["graph_revision_id"])
