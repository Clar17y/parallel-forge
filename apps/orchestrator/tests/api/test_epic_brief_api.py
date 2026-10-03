"""Authenticated epic and brief HTTP boundary tests."""

from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import forge.api.errors as api_errors
import pytest
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from forge.api.routes.epic_brief import router_for
from forge.api.schemas.epic_brief import (
    AcceptedBriefResponse,
    BriefAdoption,
    BriefAdoptionRequest,
    BriefContent,
    BriefRequirement,
    BriefRevisionCreate,
    BriefRevisionCreateRequest,
    BriefRevisionResponse,
    EpicCreate,
    EpicCreateRequest,
    EpicDraftUpdate,
    EpicDraftUpdateRequest,
    EpicResponse,
)
from forge.application.services.auth import (
    AuthenticatedActor,
    AuthenticationError,
    CsrfError,
)
from forge.domain.epic_brief import (
    AcceptedBrief,
    BriefBindingConflict,
    BriefNotAccepted,
    BriefRevisionNotFound,
    BriefRevisionRecord,
    EpicNotFound,
    EpicRecord,
    EpicVersionConflict,
)
from forge.persistence.repositories.mutations import MutationConflict
from forge.persistence.repositories.projects import ProjectNotFound
from forge.settings import Settings
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import SQLAlchemyError


class FakeRouteAuthService:
    """Minimal server-side session boundary used by route tests."""

    def __init__(self) -> None:
        self.session_token = "route-session-token"
        self.csrf_token = "route-csrf-token"
        self.actor = AuthenticatedActor(
            actor_id=uuid4(), actor_class="operator", session_id=uuid4()
        )
        self.error: Exception | None = None
        self.calls = 0

    async def require_session(
        self,
        token: str,
        *,
        csrf_token: str | None = None,
        require_csrf: bool = False,
    ) -> AuthenticatedActor:
        self.calls += 1
        if self.error is not None:
            raise self.error
        if token != self.session_token:
            raise AuthenticationError("invalid or expired session")
        if require_csrf and csrf_token != self.csrf_token:
            raise CsrfError("invalid csrf token")
        return self.actor


class FakeEpicBriefService:
    """In-memory service double recording all route calls and parameters."""

    def __init__(
        self,
        epic: EpicRecord,
        revision: BriefRevisionRecord,
        accepted: AcceptedBrief,
    ) -> None:
        self.epic = epic
        self.revision = revision
        self.accepted_record = accepted
        self.create_calls: list[tuple[AuthenticatedActor, str, EpicCreateRequest]] = []
        self.update_draft_calls: list[tuple[AuthenticatedActor, UUID, str, EpicDraftUpdateRequest]] = []
        self.save_revision_calls: list[
            tuple[AuthenticatedActor, UUID, str, BriefRevisionCreateRequest]
        ] = []
        self.adopt_revision_calls: list[
            tuple[AuthenticatedActor, UUID, str, BriefAdoptionRequest]
        ] = []
        self.list_calls: list[UUID] = []
        self.get_calls: list[UUID] = []
        self.list_revisions_calls: list[UUID] = []
        self.get_revision_calls: list[tuple[UUID, UUID]] = []
        self.accepted_calls: list[UUID] = []
        self.error: Exception | None = None

    async def create(
        self,
        *,
        actor: AuthenticatedActor,
        idempotency_key: str,
        request: EpicCreateRequest,
    ) -> EpicRecord:
        if self.error:
            raise self.error
        self.create_calls.append((actor, idempotency_key, request))
        return self.epic

    async def update_draft(
        self,
        *,
        actor: AuthenticatedActor,
        epic_id: UUID,
        idempotency_key: str,
        request: EpicDraftUpdateRequest,
    ) -> EpicRecord:
        if self.error:
            raise self.error
        self.update_draft_calls.append((actor, epic_id, idempotency_key, request))
        return self.epic

    async def save_revision(
        self,
        *,
        actor: AuthenticatedActor,
        epic_id: UUID,
        idempotency_key: str,
        request: BriefRevisionCreateRequest,
    ) -> BriefRevisionRecord:
        if self.error:
            raise self.error
        self.save_revision_calls.append((actor, epic_id, idempotency_key, request))
        return self.revision

    async def adopt_revision(
        self,
        *,
        actor: AuthenticatedActor,
        epic_id: UUID,
        idempotency_key: str,
        request: BriefAdoptionRequest,
    ) -> EpicRecord:
        if self.error:
            raise self.error
        self.adopt_revision_calls.append((actor, epic_id, idempotency_key, request))
        return self.epic

    async def list(self, project_id: UUID) -> Sequence[EpicRecord]:
        if self.error:
            raise self.error
        self.list_calls.append(project_id)
        return [self.epic]

    async def get(self, epic_id: UUID) -> EpicRecord:
        if self.error:
            raise self.error
        self.get_calls.append(epic_id)
        return self.epic

    async def list_revisions(self, epic_id: UUID) -> Sequence[BriefRevisionRecord]:
        if self.error:
            raise self.error
        self.list_revisions_calls.append(epic_id)
        return [self.revision]

    async def get_revision(
        self, epic_id: UUID, brief_revision_id: UUID
    ) -> BriefRevisionRecord:
        if self.error:
            raise self.error
        self.get_revision_calls.append((epic_id, brief_revision_id))
        return self.revision

    async def accepted(self, epic_id: UUID) -> AcceptedBrief:
        if self.error:
            raise self.error
        self.accepted_calls.append(epic_id)
        return self.accepted_record


@pytest.fixture(autouse=True)
def _patch_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure safe category translation mappings exist for epic exceptions."""
    if EpicNotFound not in api_errors._NOT_FOUND:
        monkeypatch.setattr(
            api_errors,
            "_NOT_FOUND",
            api_errors._NOT_FOUND + (EpicNotFound, BriefRevisionNotFound),
        )
    if EpicVersionConflict not in api_errors._CONFLICT:
        monkeypatch.setattr(
            api_errors,
            "_CONFLICT",
            api_errors._CONFLICT
            + (EpicVersionConflict, BriefBindingConflict, BriefNotAccepted),
        )


@pytest.fixture
def epic_route_context() -> SimpleNamespace:
    project_id = uuid4()
    epic_id = uuid4()
    revision_id = uuid4()
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

    draft = BriefContent(
        problem="Draft problem",
        outcomes=["Draft outcome"],
        requirements=[
            BriefRequirement(
                requirement_id=uuid4(),
                text="Requirement text",
                acceptance_criteria=["Criterion 1"],
            )
        ],
    )

    epic = EpicRecord(
        schema_version=1,
        epic_id=epic_id,
        project_id=project_id,
        version=1,
        title="Epic Title",
        draft=draft,
        accepted_brief_revision_id=None,
        accepted_brief_digest=None,
        accepted_graph_revision_id=None,
        accepted_graph_digest=None,
        created_at=now,
        updated_at=now,
    )

    revision = BriefRevisionRecord(
        schema_version=1,
        brief_revision_id=revision_id,
        epic_id=epic_id,
        revision_number=1,
        epic_version=1,
        content_digest="a" * 64,
        source_job_id=None,
        content=draft,
        created_at=now,
    )

    accepted = AcceptedBrief(
        schema_version=1,
        problem="Draft problem",
        outcomes=["Draft outcome"],
        requirements=[
            BriefRequirement(
                requirement_id=uuid4(),
                text="Requirement text",
                acceptance_criteria=["Criterion 1"],
            )
        ],
        epic_id=epic_id,
        project_id=project_id,
        epic_version=1,
        brief_revision_id=revision_id,
        brief_digest="a" * 64,
    )

    auth = FakeRouteAuthService()
    service = FakeEpicBriefService(epic=epic, revision=revision, accepted=accepted)
    settings = Settings(web_origin="http://127.0.0.1:3000")

    try:
        from forge.api.app import create_app

        app = create_app(
            settings,
            unit_of_work_factory=lambda: object(),
            auth_service=auth,
            approval_challenge_service=object(),
            approval_authorization_service=object(),
            project_service=object(),
            task_service=object(),
            epic_brief_service=service,
            run_service=object(),
            run_command_service=object(),
        )
    except Exception:  # noqa: BLE001
        app = FastAPI()
        app.state.settings = settings
        app.state.auth_service = auth
        app.state.epic_brief_service = service
        app.include_router(router_for(), prefix="/api")

        @app.exception_handler(RequestValidationError)
        async def request_validation_error(
            _request: Request, _error: RequestValidationError
        ) -> JSONResponse:
            return JSONResponse(
                status_code=422,
                content={"detail": "invalid request"},
            )


    return SimpleNamespace(
        app=app,
        auth=auth,
        service=service,
        project_id=project_id,
        epic_id=epic_id,
        revision_id=revision_id,
        epic=epic,
        revision=revision,
        accepted=accepted,
        settings=settings,
    )


@pytest.fixture
async def epic_client(epic_route_context: SimpleNamespace):
    settings = epic_route_context.settings
    async with AsyncClient(
        transport=ASGITransport(app=epic_route_context.app), base_url=settings.web_origin
    ) as client:
        client.cookies.set("forge_session", epic_route_context.auth.session_token)
        yield client


def _epic_create_payload(project_id: UUID) -> dict[str, object]:
    return {
        "project_id": str(project_id),
        "title": "A Bounded Epic Title",
        "draft": {
            "problem": "Clear problem statement",
            "outcomes": ["Outcome 1"],
            "requirements": [
                {
                    "requirement_id": str(uuid4()),
                    "text": "Requirement 1 text",
                    "acceptance_criteria": ["Criterion 1"],
                }
            ],
        },
    }


# =========================================================================
# Schema export and alias tests
# =========================================================================


def test_schema_exports_and_aliases() -> None:
    assert EpicCreate is EpicCreateRequest
    assert EpicDraftUpdate is EpicDraftUpdateRequest
    assert BriefRevisionCreate is BriefRevisionCreateRequest
    assert BriefAdoption is BriefAdoptionRequest
    assert issubclass(EpicResponse, EpicRecord)
    assert issubclass(BriefRevisionResponse, BriefRevisionRecord)
    assert issubclass(AcceptedBriefResponse, AcceptedBrief)


# =========================================================================
# Read and write route success and parameter forwarding tests
# =========================================================================


@pytest.mark.asyncio
async def test_create_epic_success_and_forwarding(
    epic_client: AsyncClient,
    epic_route_context: SimpleNamespace,
    route_headers: dict[str, str],
) -> None:
    payload = _epic_create_payload(epic_route_context.project_id)
    headers = {**route_headers, "Idempotency-Key": "epic-create-key-1"}

    response = await epic_client.post("/api/epics", headers=headers, json=payload)
    assert response.status_code == 201

    data = response.json()
    assert data["schema_version"] == 1
    assert data["epic_id"] == str(epic_route_context.epic.epic_id)
    assert data["project_id"] == str(epic_route_context.project_id)
    assert data["version"] == 1
    assert data["title"] == epic_route_context.epic.title
    assert data["accepted_brief_revision_id"] is None
    assert data["accepted_brief_digest"] is None
    assert data["accepted_graph_revision_id"] is None
    assert data["accepted_graph_digest"] is None

    assert len(epic_route_context.service.create_calls) == 1
    actor, key, req = epic_route_context.service.create_calls[0]
    assert actor.actor_id == epic_route_context.auth.actor.actor_id
    assert key == "epic-create-key-1"
    assert req.title == "A Bounded Epic Title"
    assert req.project_id == epic_route_context.project_id


@pytest.mark.asyncio
async def test_list_and_get_epics_success(
    epic_client: AsyncClient,
    epic_route_context: SimpleNamespace,
    route_headers: dict[str, str],
) -> None:
    host_headers = {"Host": route_headers["Host"]}

    listed = await epic_client.get(
        f"/api/epics?project_id={epic_route_context.project_id}",
        headers=host_headers,
    )
    assert listed.status_code == 200
    assert len(listed.json()) == 1
    assert listed.json()[0]["epic_id"] == str(epic_route_context.epic.epic_id)
    assert epic_route_context.service.list_calls == [epic_route_context.project_id]

    fetched = await epic_client.get(
        f"/api/epics/{epic_route_context.epic_id}",
        headers=host_headers,
    )
    assert fetched.status_code == 200
    assert fetched.json()["epic_id"] == str(epic_route_context.epic.epic_id)
    assert epic_route_context.service.get_calls == [epic_route_context.epic_id]


@pytest.mark.asyncio
async def test_patch_epic_draft_success_and_forwarding(
    epic_client: AsyncClient,
    epic_route_context: SimpleNamespace,
    route_headers: dict[str, str],
) -> None:
    headers = {**route_headers, "Idempotency-Key": "epic-patch-key-1"}
    payload = {
        "expected_epic_version": 1,
        "title": "Updated Title",
        "draft": {
            "problem": "Updated problem",
            "outcomes": ["New outcome"],
            "requirements": [],
        },
    }

    response = await epic_client.patch(
        f"/api/epics/{epic_route_context.epic_id}",
        headers=headers,
        json=payload,
    )
    assert response.status_code == 200
    assert response.json()["epic_id"] == str(epic_route_context.epic.epic_id)

    assert len(epic_route_context.service.update_draft_calls) == 1
    actor, epic_id, key, req = epic_route_context.service.update_draft_calls[0]
    assert actor.actor_id == epic_route_context.auth.actor.actor_id
    assert epic_id == epic_route_context.epic_id
    assert key == "epic-patch-key-1"
    assert req.expected_epic_version == 1
    assert req.title == "Updated Title"
    assert req.draft.problem == "Updated problem"


@pytest.mark.asyncio
async def test_create_and_list_and_get_brief_revisions_success(
    epic_client: AsyncClient,
    epic_route_context: SimpleNamespace,
    route_headers: dict[str, str],
) -> None:
    headers = {**route_headers, "Idempotency-Key": "brief-rev-create-1"}
    host_headers = {"Host": route_headers["Host"]}
    payload = {
        "expected_epic_version": 1,
        "content": {
            "problem": "Revision problem",
            "outcomes": ["Rev outcome"],
            "requirements": [],
        },
    }

    created = await epic_client.post(
        f"/api/epics/{epic_route_context.epic_id}/brief-revisions",
        headers=headers,
        json=payload,
    )
    assert created.status_code == 201
    rev_data = created.json()
    assert rev_data["brief_revision_id"] == str(epic_route_context.revision.brief_revision_id)
    assert rev_data["revision_number"] == 1
    assert rev_data["source_job_id"] is None
    assert rev_data["content_digest"] == "a" * 64

    assert len(epic_route_context.service.save_revision_calls) == 1
    actor, epic_id, key, req = epic_route_context.service.save_revision_calls[0]
    assert actor.actor_id == epic_route_context.auth.actor.actor_id
    assert epic_id == epic_route_context.epic_id
    assert key == "brief-rev-create-1"
    assert req.expected_epic_version == 1

    listed = await epic_client.get(
        f"/api/epics/{epic_route_context.epic_id}/brief-revisions",
        headers=host_headers,
    )
    assert listed.status_code == 200
    assert len(listed.json()) == 1
    assert listed.json()[0]["source_job_id"] is None

    fetched = await epic_client.get(
        f"/api/epics/{epic_route_context.epic_id}/brief-revisions/{epic_route_context.revision_id}",
        headers=host_headers,
    )
    assert fetched.status_code == 200
    assert fetched.json()["brief_revision_id"] == str(epic_route_context.revision_id)
    assert fetched.json()["source_job_id"] is None


@pytest.mark.asyncio
async def test_adopt_brief_revision_success(
    epic_client: AsyncClient,
    epic_route_context: SimpleNamespace,
    route_headers: dict[str, str],
) -> None:
    headers = {**route_headers, "Idempotency-Key": "brief-adopt-key-1"}
    payload = {
        "expected_epic_version": 1,
        "brief_revision_id": str(epic_route_context.revision_id),
        "brief_digest": "a" * 64,
    }

    response = await epic_client.post(
        f"/api/epics/{epic_route_context.epic_id}/brief-adoptions",
        headers=headers,
        json=payload,
    )
    assert response.status_code == 200
    assert response.json()["epic_id"] == str(epic_route_context.epic.epic_id)

    assert len(epic_route_context.service.adopt_revision_calls) == 1
    actor, epic_id, key, req = epic_route_context.service.adopt_revision_calls[0]
    assert actor.actor_id == epic_route_context.auth.actor.actor_id
    assert epic_id == epic_route_context.epic_id
    assert key == "brief-adopt-key-1"
    assert req.expected_epic_version == 1
    assert req.brief_revision_id == epic_route_context.revision_id
    assert req.brief_digest == "a" * 64


@pytest.mark.asyncio
async def test_get_accepted_brief_success(
    epic_client: AsyncClient,
    epic_route_context: SimpleNamespace,
    route_headers: dict[str, str],
) -> None:
    host_headers = {"Host": route_headers["Host"]}
    response = await epic_client.get(
        f"/api/epics/{epic_route_context.epic_id}/accepted-brief",
        headers=host_headers,
    )
    assert response.status_code == 200
    data = response.json()
    assert data["epic_id"] == str(epic_route_context.epic_id)
    assert data["project_id"] == str(epic_route_context.project_id)
    assert data["epic_version"] == 1
    assert data["brief_revision_id"] == str(epic_route_context.revision_id)
    assert data["brief_digest"] == "a" * 64
    assert data["problem"] == "Draft problem"
    assert data["outcomes"] == ["Draft outcome"]
    assert len(data["requirements"]) == 1
    assert epic_route_context.service.accepted_calls == [epic_route_context.epic_id]


# =========================================================================
# Explicit null keys in responses
# =========================================================================


@pytest.mark.asyncio
async def test_explicit_null_keys_in_epic_and_revision_responses(
    epic_client: AsyncClient,
    epic_route_context: SimpleNamespace,
    route_headers: dict[str, str],
) -> None:
    host_headers = {"Host": route_headers["Host"]}

    epic_res = await epic_client.get(
        f"/api/epics/{epic_route_context.epic_id}",
        headers=host_headers,
    )
    epic_data = epic_res.json()
    assert "accepted_brief_revision_id" in epic_data
    assert epic_data["accepted_brief_revision_id"] is None
    assert "accepted_brief_digest" in epic_data
    assert epic_data["accepted_brief_digest"] is None
    assert "accepted_graph_revision_id" in epic_data
    assert epic_data["accepted_graph_revision_id"] is None
    assert "accepted_graph_digest" in epic_data
    assert epic_data["accepted_graph_digest"] is None

    rev_res = await epic_client.get(
        f"/api/epics/{epic_route_context.epic_id}/brief-revisions/{epic_route_context.revision_id}",
        headers=host_headers,
    )
    rev_data = rev_res.json()
    assert "source_job_id" in rev_data
    assert rev_data["source_job_id"] is None


# =========================================================================
# Authentication and CSRF tests across all route categories
# =========================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,path,needs_body",
    [
        ("POST", "/api/epics", True),
        ("PATCH", "/api/epics/{epic_id}", True),
        ("POST", "/api/epics/{epic_id}/brief-revisions", True),
        ("POST", "/api/epics/{epic_id}/brief-adoptions", True),
    ],
)
async def test_mutating_routes_reject_missing_or_expired_session(
    epic_client: AsyncClient,
    epic_route_context: SimpleNamespace,
    route_headers: dict[str, str],
    method: str,
    path: str,
    needs_body: bool,
) -> None:
    epic_client.cookies.clear()
    headers = {**route_headers, "Idempotency-Key": "auth-test-key"}
    formatted_path = path.format(epic_id=epic_route_context.epic_id)

    res = await epic_client.request(method, formatted_path, headers=headers, json={})
    assert res.status_code == 401
    assert "authentication required" in res.text

    epic_client.cookies.set("forge_session", "expired-or-invalid-token")
    res2 = await epic_client.request(method, formatted_path, headers=headers, json={})
    assert res2.status_code == 401
    assert "authentication required" in res2.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/api/epics?project_id={project_id}",
        "/api/epics/{epic_id}",
        "/api/epics/{epic_id}/brief-revisions",
        "/api/epics/{epic_id}/brief-revisions/{revision_id}",
        "/api/epics/{epic_id}/accepted-brief",
    ],
)
async def test_read_routes_reject_missing_or_expired_session(
    epic_client: AsyncClient,
    epic_route_context: SimpleNamespace,
    route_headers: dict[str, str],
    path: str,
) -> None:
    epic_client.cookies.clear()
    host_headers = {"Host": route_headers["Host"]}
    formatted_path = path.format(
        project_id=epic_route_context.project_id,
        epic_id=epic_route_context.epic_id,
        revision_id=epic_route_context.revision_id,
    )

    res = await epic_client.get(formatted_path, headers=host_headers)
    assert res.status_code == 401

    epic_client.cookies.set("forge_session", "expired-token")
    res2 = await epic_client.get(formatted_path, headers=host_headers)
    assert res2.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/api/epics"),
        ("PATCH", "/api/epics/{epic_id}"),
        ("POST", "/api/epics/{epic_id}/brief-revisions"),
        ("POST", "/api/epics/{epic_id}/brief-adoptions"),
    ],
)
async def test_mutating_routes_reject_missing_or_invalid_csrf(
    epic_client: AsyncClient,
    epic_route_context: SimpleNamespace,
    route_headers: dict[str, str],
    method: str,
    path: str,
) -> None:
    formatted_path = path.format(epic_id=epic_route_context.epic_id)

    headers_without_csrf = {
        "Host": route_headers["Host"],
        "Origin": route_headers["Origin"],
        "Idempotency-Key": "csrf-test-key",
    }
    res = await epic_client.request(method, formatted_path, headers=headers_without_csrf, json={})
    assert res.status_code == 403

    headers_wrong_csrf = {**headers_without_csrf, "X-CSRF-Token": "invalid-csrf-token"}
    res2 = await epic_client.request(method, formatted_path, headers=headers_wrong_csrf, json={})
    assert res2.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "security_headers",
    [
        {"Host": "127.0.0.1:3001", "Origin": "http://127.0.0.1:3000"},
        {"Host": "127.0.0.1:3000", "Origin": "http://localhost:3000"},
        {"Host": "127.0.0.1:3000", "Origin": "http://evil.com"},
    ],
)
async def test_mutating_routes_reject_mismatched_origin_or_host(
    epic_client: AsyncClient,
    epic_route_context: SimpleNamespace,
    route_headers: dict[str, str],
    security_headers: dict[str, str],
) -> None:
    headers = {
        **security_headers,
        "X-CSRF-Token": route_headers["X-CSRF-Token"],
        "Idempotency-Key": "security-headers-key",
    }
    payload = _epic_create_payload(epic_route_context.project_id)
    response = await epic_client.post("/api/epics", headers=headers, json=payload)
    assert response.status_code == 403
    assert epic_route_context.service.create_calls == []


# =========================================================================
# Idempotency header tests
# =========================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,path,payload_fn",
    [
        ("POST", "/api/epics", lambda ctx: _epic_create_payload(ctx.project_id)),
        (
            "PATCH",
            "/api/epics/{epic_id}",
            lambda ctx: {"expected_epic_version": 1, "title": "T", "draft": {}},
        ),
        (
            "POST",
            "/api/epics/{epic_id}/brief-revisions",
            lambda ctx: {"expected_epic_version": 1, "content": {}},
        ),
        (
            "POST",
            "/api/epics/{epic_id}/brief-adoptions",
            lambda ctx: {
                "expected_epic_version": 1,
                "brief_revision_id": str(ctx.revision_id),
                "brief_digest": "a" * 64,
            },
        ),
    ],
)
async def test_mutating_routes_require_nonblank_idempotency_key(
    epic_client: AsyncClient,
    epic_route_context: SimpleNamespace,
    route_headers: dict[str, str],
    method: str,
    path: str,
    payload_fn,
) -> None:
    formatted_path = path.format(epic_id=epic_route_context.epic_id)
    payload = payload_fn(epic_route_context)

    missing = await epic_client.request(method, formatted_path, headers=route_headers, json=payload)
    assert missing.status_code == 422

    blank = await epic_client.request(
        method,
        formatted_path,
        headers={**route_headers, "Idempotency-Key": "    "},
        json=deepcopy(payload),
    )
    assert blank.status_code == 422

    oversized = await epic_client.request(
        method,
        formatted_path,
        headers={**route_headers, "Idempotency-Key": "k" * 256},
        json=deepcopy(payload),
    )
    assert oversized.status_code == 422


# =========================================================================
# Schema, extra fields, bounds, and raw credential rejection tests (422)
# =========================================================================


@pytest.mark.asyncio
async def test_create_epic_rejects_extra_fields_with_422(
    epic_client: AsyncClient,
    epic_route_context: SimpleNamespace,
    route_headers: dict[str, str],
) -> None:
    headers = {**route_headers, "Idempotency-Key": "extra-field-key"}
    payload = _epic_create_payload(epic_route_context.project_id)
    payload["unexpected_extra"] = "forbidden"

    response = await epic_client.post("/api/epics", headers=headers, json=payload)
    assert response.status_code == 422
    assert epic_route_context.service.create_calls == []


@pytest.mark.asyncio
async def test_create_epic_rejects_oversized_and_blank_title_with_422(
    epic_client: AsyncClient,
    epic_route_context: SimpleNamespace,
    route_headers: dict[str, str],
) -> None:
    headers = {**route_headers, "Idempotency-Key": "oversized-title-key"}
    payload = _epic_create_payload(epic_route_context.project_id)

    payload["title"] = "   "
    blank_res = await epic_client.post("/api/epics", headers=headers, json=payload)
    assert blank_res.status_code == 422

    payload["title"] = "a" * 257
    oversized_res = await epic_client.post("/api/epics", headers=headers, json=payload)
    assert oversized_res.status_code == 422


@pytest.mark.asyncio
async def test_create_epic_rejects_duplicate_requirement_ids_with_422(
    epic_client: AsyncClient,
    epic_route_context: SimpleNamespace,
    route_headers: dict[str, str],
) -> None:
    headers = {**route_headers, "Idempotency-Key": "dup-req-key"}
    shared_id = str(uuid4())
    payload = _epic_create_payload(epic_route_context.project_id)
    payload["draft"] = {
        "problem": "Problem",
        "requirements": [
            {"requirement_id": shared_id, "text": "Req 1", "acceptance_criteria": []},
            {"requirement_id": shared_id, "text": "Req 2", "acceptance_criteria": []},
        ],
    }

    response = await epic_client.post("/api/epics", headers=headers, json=payload)
    assert response.status_code == 422
    assert epic_route_context.service.create_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "secret_value",
    [
        "bearer supersecrettoken123456",
        "ghp_123456789012345678901234567890",
        "postgres://user:secretpassword@localhost:5432/db",
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA0\n-----END RSA PRIVATE KEY-----",
    ],
)
async def test_payload_rejects_raw_credentials_safely_with_422(
    epic_client: AsyncClient,
    epic_route_context: SimpleNamespace,
    route_headers: dict[str, str],
    secret_value: str,
) -> None:
    headers = {**route_headers, "Idempotency-Key": "secret-key-1"}

    title_payload = _epic_create_payload(epic_route_context.project_id)
    title_payload["title"] = f"Epic {secret_value}"
    res1 = await epic_client.post("/api/epics", headers=headers, json=title_payload)
    assert res1.status_code == 422
    assert "supersecrettoken" not in res1.text
    assert "secretpassword" not in res1.text

    problem_payload = _epic_create_payload(epic_route_context.project_id)
    problem_payload["draft"] = {"problem": f"Problem with {secret_value}"}
    res2 = await epic_client.post("/api/epics", headers=headers, json=problem_payload)
    assert res2.status_code == 422
    assert "supersecrettoken" not in res2.text


@pytest.mark.asyncio
async def test_adopt_revision_rejects_invalid_digest_format_with_422(
    epic_client: AsyncClient,
    epic_route_context: SimpleNamespace,
    route_headers: dict[str, str],
) -> None:
    headers = {**route_headers, "Idempotency-Key": "bad-digest-key"}
    payload = {
        "expected_epic_version": 1,
        "brief_revision_id": str(epic_route_context.revision_id),
        "brief_digest": "not-a-valid-hex-digest",
    }
    response = await epic_client.post(
        f"/api/epics/{epic_route_context.epic_id}/brief-adoptions",
        headers=headers,
        json=payload,
    )
    assert response.status_code == 422


# =========================================================================
# Safe error translation tests (404, 409, 422, 503)
# =========================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error,expected_status,expected_detail",
    [
        (EpicNotFound("epic not found"), 404, "resource not found"),
        (BriefRevisionNotFound("revision not found"), 404, "resource not found"),
        (ProjectNotFound("project not found"), 404, "resource not found"),
        (EpicVersionConflict("version stale"), 409, "request conflicts with current state"),
        (BriefBindingConflict("digest mismatch"), 409, "request conflicts with current state"),
        (BriefNotAccepted("no brief accepted"), 409, "request conflicts with current state"),
        (MutationConflict("key conflict"), 409, "request conflicts with current state"),
        (ValueError("invalid field value"), 422, "request cannot be processed"),
        (SQLAlchemyError("database connection broken"), 503, "persistence unavailable"),
        (RuntimeError("unexpected failure"), 503, "service unavailable"),
    ],
)
async def test_service_boundary_errors_safely_translated(
    epic_client: AsyncClient,
    epic_route_context: SimpleNamespace,
    route_headers: dict[str, str],
    error: Exception,
    expected_status: int,
    expected_detail: str,
) -> None:
    host_headers = {"Host": route_headers["Host"]}
    epic_route_context.service.error = error

    response = await epic_client.get(
        f"/api/epics/{epic_route_context.epic_id}",
        headers=host_headers,
    )
    assert response.status_code == expected_status
    assert response.json()["detail"] == expected_detail
    assert "database connection broken" not in response.text
    assert "unexpected failure" not in response.text


@pytest.mark.asyncio
async def test_unconfigured_service_returns_safe_500(
    epic_route_context: SimpleNamespace,
    route_headers: dict[str, str],
) -> None:
    epic_route_context.app.state.epic_brief_service = None
    settings = epic_route_context.settings

    async with AsyncClient(
        transport=ASGITransport(app=epic_route_context.app), base_url=settings.web_origin
    ) as client:
        client.cookies.set("forge_session", epic_route_context.auth.session_token)
        response = await client.get(
            f"/api/epics/{epic_route_context.epic_id}",
            headers={"Host": route_headers["Host"]},
        )
        assert response.status_code == 500
        assert response.json()["detail"] == "API service is not configured"
