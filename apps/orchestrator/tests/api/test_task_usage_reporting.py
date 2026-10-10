from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from forge.api.dependencies import require_operator
from forge.api.routes.task_usage_reporting import router_for
from forge.application.services.auth import AuthenticatedActor
from forge.application.services.task_usage_baselines import TaskUsageBaselineService
from forge.domain.task_usage_baselines import BaselineSample


class Source:
    def __init__(self, project_id):
        self.project_id = project_id

    async def accessible_project_ids(self):
        return {self.project_id}

    async def samples(self, *, project_ids, as_of, window_days):
        return (
            BaselineSample(
                project_id=self.project_id,
                subject_kind="run",
                subject_id=uuid4(),
                completed_at=datetime.now(UTC) - timedelta(days=1),
                total_tokens=0,
                outcome="completed",
                finalized=True,
                measured=True,
                configured_route="configured",
                effective_route="effective",
                task_kind="implementation",
                route_members=(("developer", "provider", "model", "medium"),),
            ),
        )

    async def exclusions(self, *, project_ids, as_of, window_days, subjects):
        return {}


def test_authenticated_report_is_project_scoped_and_supports_route_dimensions():
    project_id = uuid4()
    app = FastAPI()
    app.include_router(router_for(), prefix="/api")
    app.state.task_usage_baseline_service = TaskUsageBaselineService(Source(project_id))
    app.dependency_overrides[require_operator] = lambda: AuthenticatedActor(
        actor_id=uuid4(), actor_class="operator", session_id=uuid4()
    )
    response = TestClient(app).get(
        "/api/task-usage-report",
        params={"project_id": str(project_id), "task_kind": "implementation", "model": "model"},
    )
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["overview"]["status"] == "insufficient_history"
    assert report["overview"]["eligible_count"] == 1
    assert report["filters"]["project_ids"] == [str(project_id)]
    assert report["by_model"]["model"]["eligible_count"] == 1


def test_report_returns_service_unavailable_when_not_wired():
    app = FastAPI()
    app.include_router(router_for(), prefix="/api")
    app.dependency_overrides[require_operator] = lambda: AuthenticatedActor(
        actor_id=uuid4(), actor_class="operator", session_id=uuid4()
    )
    response = TestClient(app).get("/api/task-usage-report")
    assert response.status_code == 503
