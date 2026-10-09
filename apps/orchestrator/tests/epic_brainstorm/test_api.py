from uuid import uuid4

import pytest
from fastapi import FastAPI
from forge.api.routes.epic_brainstorm import router_for
from forge.api.routes.epic_decomposition import router_for as decomposition_router_for
from forge.application.ports.epic_brainstorm import BriefInput
from forge.application.services.epic_brainstorm import EpicBrainstormService
from forge.application.services.epic_decomposition import EpicDecompositionService
from forge.domain.subscription import RouteBinding, RouteSpec, TaskBudget
from forge.persistence.models.project import Project
from forge.settings import Settings
from httpx import ASGITransport, AsyncClient

from apps.orchestrator.tests.api.conftest import FakeRouteAuthService


class BriefFixture:
    def __init__(self, epic_id, project_id):
        self.value = BriefInput(
            epic_id=epic_id,
            project_id=project_id,
            epic_version=1,
            draft_digest="a" * 64,
            accepted_revision_id=None,
            accepted_digest=None,
        )

    async def input(self, epic_id, *, for_update=False):
        assert epic_id == self.value.epic_id
        return self.value


@pytest.mark.asyncio
async def test_session_csrf_and_cross_subject_boundaries(brainstorm_session_factory):
    epic_id, project_id = uuid4(), uuid4()
    async with brainstorm_session_factory() as session, session.begin():
        session.add(
            Project(
                id=project_id,
                canonical_path=f"/tmp/{project_id}",
                github_repository="example/repo",
                default_branch="main",
            )
        )
    route = RouteSpec(provider="fake", client="fake", model="fixture")
    app = FastAPI()
    app.state.settings = Settings(web_origin="http://127.0.0.1:3000")
    app.state.auth_service = FakeRouteAuthService()
    app.state.epic_brainstorm_service = EpicBrainstormService(
        brainstorm_session_factory,
        lambda _: BriefFixture(epic_id, project_id),
        route=RouteBinding(requested=route, effective=route),
        budget=TaskBudget(max_provider_attempts=1),
    )
    app.state.epic_decomposition_service = EpicDecompositionService(
        None, authoring_service=app.state.epic_brainstorm_service
    )
    app.include_router(router_for(), prefix="/api")
    app.include_router(decomposition_router_for(), prefix="/api")
    headers = {
        "Host": "127.0.0.1:3000",
        "Origin": "http://127.0.0.1:3000",
        "X-CSRF-Token": "route-csrf-token",
        "Idempotency-Key": "create",
    }
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://127.0.0.1:3000"
    ) as client:
        path = f"/api/epics/{epic_id}/brainstorm-conversations"
        payload = {"project_id": str(project_id), "text": "Discover the workflow"}
        assert (await client.post(path, json=payload, headers=headers)).status_code == 401
        client.cookies.set("forge_session", app.state.auth_service.session_token)
        assert (
            await client.post(path, json=payload, headers={**headers, "X-CSRF-Token": "bad"})
        ).status_code == 403
        created = await client.post(path, json=payload, headers=headers)
        assert created.status_code == 200
        conversation_id = created.json()["conversation_id"]
        conversation_version = created.json()["version"]
        wrong = await client.get(
            f"{path}/{conversation_id}/turns?project_id={uuid4()}", headers=headers
        )
        assert wrong.status_code == 404
        assert (
            await client.get(
                f"{path}/{conversation_id}/turns?project_id={project_id}", headers=headers
            )
        ).status_code == 200
        brainstorm_threads = await client.get(f"{path}?project_id={project_id}", headers=headers)
        decomposition_threads = await client.get(
            f"/api/epics/{epic_id}/decomposition-conversations?project_id={project_id}",
            headers=headers,
        )
        assert brainstorm_threads.json() == [
            {
                "conversation_id": conversation_id,
                "conversation_version": conversation_version,
                "job_ids": [],
            }
        ]
        assert decomposition_threads.json() == brainstorm_threads.json()
