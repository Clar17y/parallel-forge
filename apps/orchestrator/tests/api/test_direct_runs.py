"""Direct-run HTTP boundary tests with an injected fake service."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from uuid import uuid4

import pytest
from forge.api.app import create_app
from forge.settings import Settings
from httpx import ASGITransport, AsyncClient

from apps.orchestrator.tests.api.conftest import FakeRouteAuthService


@dataclass
class FakeRun:
    id: object = field(default_factory=uuid4)
    project_id: object = field(default_factory=uuid4)
    task_text: str = "do it"
    client: str = "muse_code"
    model: str = "model-1"
    effort: str | None = "high"
    state: str = "QUEUED"
    branch: str | None = None
    worktree_path: str | None = None
    base_sha: str | None = None
    head_sha: str | None = None
    summary: str | None = None
    question: str | None = None
    options: tuple[str, ...] = ()
    failure_reason: str | None = None
    result_degraded: bool = False
    cancel_requested: bool = False
    pr_number: int | None = None
    pr_url: str | None = None
    pr_rounds: int = 0
    shape: str = "quick"
    review_plan_first: bool = False
    plan_text: str | None = None
    plan_approved: bool = False
    created_at: object = None
    updated_at: object = None


class FakeDirectService:
    def __init__(self) -> None:
        self.run = FakeRun()
        self.created: list[dict] = []

    async def create(self, **kwargs):
        if kwargs["model"] == "nope":
            raise ValueError("unknown direct route")
        self.created.append(kwargs)
        return self.run

    async def list(self, limit=50):
        return (self.run,)

    async def get(self, run_id):
        return self.run

    async def sessions(self, run_id):
        return ()

    async def checks(self, run_id):
        return (
            SimpleNamespace(
                id=uuid4(), run_id=run_id, round=0, command_name="smoke",
                kind="test", tree_sha="a" * 40, command_digest="b" * 64,
                policy_version=1, status="FAILED", exit_code=1, duration_ms=12,
                failure_excerpt="FAILED tests/test_smoke.py::test_it",
                created_at=None,
            ),
        )

    async def reviews(self, run_id):
        return (
            SimpleNamespace(
                id=uuid4(), run_id=run_id, session_id=uuid4(), round=0,
                client="codex_app_server", model="review-model", verdict="blocking",
                findings=({"severity": "blocking", "file": "a.py", "detail": "fix it"},),
                summary="bad", degraded=False, reviewer_modified_worktree=True,
                created_at=None,
            ),
        )

    async def cancel(self, run_id):
        self.run.state = "CANCELLED"
        return self.run

    async def approve_plan(self, run_id):
        if self.run.state != "AWAITING_PLAN_APPROVAL":
            raise ValueError("direct run is not awaiting plan approval")
        self.run.plan_approved = True
        self.run.state = "QUEUED"
        return self.run

    async def transcript(self, run_id, session_id=None, tail_bytes=32768):
        return "hello transcript"

    async def diff(self, run_id):
        return "diff --git a/x b/x"

    def routes(self):
        return ({"client": "muse_code", "model": "model-1", "effort": "high"},)


@pytest.fixture
def direct_context():
    auth = FakeRouteAuthService()
    service = FakeDirectService()
    settings = Settings(web_origin="http://127.0.0.1:3000")
    app = create_app(
        settings,
        unit_of_work_factory=lambda: object(),
        auth_service=auth,
        approval_challenge_service=object(),
        approval_authorization_service=object(),
        direct_run_service=service,
    )
    return SimpleNamespace(app=app, auth=auth, service=service)


@pytest.fixture
async def direct_client(direct_context):
    async with AsyncClient(
        transport=ASGITransport(app=direct_context.app), base_url="http://127.0.0.1:3000"
    ) as client:
        client.cookies.set("forge_session", direct_context.auth.session_token)
        yield client


HEADERS = {"Host": "127.0.0.1:3000", "Origin": "http://127.0.0.1:3000", "X-CSRF-Token": "route-csrf-token"}


async def test_direct_routes_and_create(direct_client, direct_context):
    routes = await direct_client.get("/api/direct-runs/routes", headers=HEADERS)
    assert routes.status_code == 200
    body = routes.json()
    assert body == [{"client": "muse_code", "model": "model-1", "effort": "high"}]
    assert "executable" not in str(body)

    created = await direct_client.post(
        "/api/direct-runs",
        headers=HEADERS,
        json={
            "project_id": str(direct_context.service.run.project_id),
            "task": "do it",
            "client": "muse_code",
            "model": "model-1",
        },
    )
    assert created.status_code == 201
    assert created.json()["state"] == "QUEUED"

    bad = await direct_client.post(
        "/api/direct-runs",
        headers=HEADERS,
        json={
            "project_id": str(direct_context.service.run.project_id),
            "task": "do it",
            "client": "muse_code",
            "model": "nope",
        },
    )
    assert bad.status_code == 422

    detail = await direct_client.get(
        f"/api/direct-runs/{direct_context.service.run.id}", headers=HEADERS
    )
    assert detail.status_code == 200
    assert detail.json()["sessions"] == []
    assert detail.json()["checks"][0]["status"] == "FAILED"
    assert detail.json()["checks"][0]["command_name"] == "smoke"
    assert "test_it" in detail.json()["checks"][0]["failure_excerpt"]
    assert detail.json()["reviews"][0]["verdict"] == "blocking"
    assert detail.json()["reviews"][0]["client"] == "codex_app_server"
    assert detail.json()["reviews"][0]["findings"][0]["detail"] == "fix it"
    assert detail.json()["reviews"][0]["reviewer_modified_worktree"] is True

    transcript = await direct_client.get(
        f"/api/direct-runs/{direct_context.service.run.id}/transcript", headers=HEADERS
    )
    assert transcript.json()["text"] == "hello transcript"
    diff = await direct_client.get(
        f"/api/direct-runs/{direct_context.service.run.id}/diff", headers=HEADERS
    )
    assert "diff --git" in diff.json()["diff"]

    cancelled = await direct_client.post(
        f"/api/direct-runs/{direct_context.service.run.id}/cancel", headers=HEADERS, json={}
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["state"] == "CANCELLED"


async def test_direct_create_planned_shape_and_approve_plan(direct_client, direct_context):
    created = await direct_client.post(
        "/api/direct-runs",
        headers=HEADERS,
        json={
            "project_id": str(direct_context.service.run.project_id),
            "task": "plan it",
            "client": "muse_code",
            "model": "model-1",
            "shape": "planned",
            "review_plan_first": True,
        },
    )
    assert created.status_code == 201
    sent = direct_context.service.created[-1]
    assert sent["shape"] == "planned"
    assert sent["review_plan_first"] is True

    bad_shape = await direct_client.post(
        "/api/direct-runs",
        headers=HEADERS,
        json={
            "project_id": str(direct_context.service.run.project_id),
            "task": "epic it",
            "client": "muse_code",
            "model": "model-1",
            "shape": "epic",
        },
    )
    assert bad_shape.status_code == 422

    early = await direct_client.post(
        f"/api/direct-runs/{direct_context.service.run.id}/approve-plan",
        headers=HEADERS, json={},
    )
    assert early.status_code == 422

    direct_context.service.run.state = "AWAITING_PLAN_APPROVAL"
    direct_context.service.run.plan_text = "the plan"
    approved = await direct_client.post(
        f"/api/direct-runs/{direct_context.service.run.id}/approve-plan",
        headers=HEADERS, json={},
    )
    assert approved.status_code == 200
    body = approved.json()
    assert body["state"] == "QUEUED"
    assert body["plan_approved"] is True
    assert body["plan_text"] == "the plan"
