"""Authenticated epic work-item graph HTTP boundary tests."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from forge.api.routes.epic_items import router_for
from forge.api.schemas.epic_brief import BriefContent, EpicResponse
from forge.api.schemas.epic_items import (
    AcceptedGraphResponse,
    GraphAdoption,
    GraphAdoptionRequest,
    GraphRevisionCreate,
    GraphRevisionCreateRequest,
    GraphRevisionResponse,
    ItemInput,
    ItemReadiness,
    ItemSnapshot,
)
from forge.application.services.auth import (
    AuthenticatedActor,
    AuthenticationError,
    CsrfError,
)
from forge.domain.epic_brief import (
    BriefBindingConflict,
    BriefNotAccepted,
    EpicNotFound,
    EpicRecord,
    EpicVersionConflict,
)
from forge.domain.epic_items import (
    AcceptedGraph,
    GraphBindingConflict,
    GraphNotAccepted,
    GraphRevisionNotFound,
    GraphRevisionRecord,
    GraphValidationError,
    make_snapshot,
)
from forge.persistence.repositories.mutations import MutationConflict
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


class FakeEpicItemsService:
    """In-memory service double recording all route calls and parameters."""

    def __init__(
        self,
        revision: GraphRevisionRecord,
        accepted: AcceptedGraph,
        epic: EpicRecord,
    ) -> None:
        self.revision = revision
        self.accepted_record = accepted
        self.epic = epic
        self.save_revision_calls: list[
            tuple[AuthenticatedActor, UUID, str, GraphRevisionCreateRequest]
        ] = []
        self.adopt_revision_calls: list[
            tuple[AuthenticatedActor, UUID, str, GraphAdoptionRequest]
        ] = []
        self.list_revisions_calls: list[UUID] = []
        self.get_revision_calls: list[tuple[UUID, UUID]] = []
        self.accepted_calls: list[UUID] = []
        self.error: Exception | None = None

    async def save_revision(
        self,
        *,
        actor: AuthenticatedActor,
        epic_id: UUID,
        idempotency_key: str,
        request: GraphRevisionCreateRequest,
    ) -> GraphRevisionRecord:
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
        request: GraphAdoptionRequest,
    ) -> EpicRecord:
        if self.error:
            raise self.error
        self.adopt_revision_calls.append((actor, epic_id, idempotency_key, request))
        return self.epic

    async def list_revisions(self, epic_id: UUID) -> Sequence[GraphRevisionRecord]:
        if self.error:
            raise self.error
        self.list_revisions_calls.append(epic_id)
        return [self.revision]

    async def get_revision(
        self, epic_id: UUID, graph_revision_id: UUID
    ) -> GraphRevisionRecord:
        if self.error:
            raise self.error
        self.get_revision_calls.append((epic_id, graph_revision_id))
        return self.revision

    async def accepted(self, epic_id: UUID) -> AcceptedGraph:
        if self.error:
            raise self.error
        self.accepted_calls.append(epic_id)
        return self.accepted_record


@pytest.fixture
def epic_items_route_context() -> SimpleNamespace:
    project_id = uuid4()
    epic_id = uuid4()
    brief_revision_id = uuid4()
    brief_digest = "b" * 64
    graph_revision_id = uuid4()
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

    req_id_1 = uuid4()
    req_id_2 = uuid4()
    item_id_1 = uuid4()
    item_id_2 = uuid4()
    item_id_3 = uuid4()

    # Three items demonstrating the three readiness states:
    # item 1: required, no dependencies -> ready
    # item 2: required, depends on item 1 -> blocked
    # item 3: deferred, depends on item 2 -> deferred
    item_inputs = [
        ItemInput(
            item_id=item_id_1,
            disposition="required",
            ordinal=1,
            title="Foundational work item",
            outcome="Foundation is built",
            acceptance_criteria=["Foundation passes checks"],
            source_requirement_ids=[req_id_1],
            dependency_item_ids=[],
        ),
        ItemInput(
            item_id=item_id_2,
            disposition="required",
            ordinal=2,
            title="Dependent work item",
            outcome="Feature depending on foundation",
            acceptance_criteria=["Feature functions correctly"],
            source_requirement_ids=[req_id_2],
            dependency_item_ids=[item_id_1],
        ),
        ItemInput(
            item_id=item_id_3,
            disposition="deferred",
            ordinal=3,
            title="Deferred work item",
            outcome="Future enhancement",
            acceptance_criteria=["Enhancement is specified"],
            source_requirement_ids=[req_id_2],
            dependency_item_ids=[item_id_2],
        ),
    ]

    snapshots, graph_digest = make_snapshot(
        graph_revision_id, item_inputs, brief_revision_id, brief_digest
    )

    revision = GraphRevisionRecord(
        schema_version=1,
        graph_revision_id=graph_revision_id,
        epic_id=epic_id,
        brief_revision_id=brief_revision_id,
        brief_digest=brief_digest,
        revision_number=1,
        epic_version=2,
        graph_digest=graph_digest,
        items=snapshots,
        created_at=now,
    )

    accepted = AcceptedGraph(
        schema_version=1,
        epic_id=epic_id,
        brief_revision_id=brief_revision_id,
        brief_digest=brief_digest,
        graph_revision_id=graph_revision_id,
        graph_digest=graph_digest,
        items=snapshots,
    )

    epic = EpicRecord(
        schema_version=1,
        epic_id=epic_id,
        project_id=project_id,
        version=3,
        title="Epic Title",
        draft=BriefContent(problem="P", outcomes=["O"], requirements=[]),
        accepted_brief_revision_id=brief_revision_id,
        accepted_brief_digest=brief_digest,
        accepted_graph_revision_id=graph_revision_id,
        accepted_graph_digest=graph_digest,
        created_at=now,
        updated_at=now,
    )

    auth = FakeRouteAuthService()
    service = FakeEpicItemsService(revision=revision, accepted=accepted, epic=epic)
    settings = Settings(web_origin="http://127.0.0.1:3000")

    app = FastAPI()
    app.state.settings = settings
    app.state.auth_service = auth
    app.state.epic_items_service = service
    app.include_router(router_for(), prefix="/api")

    @app.exception_handler(SQLAlchemyError)
    async def persistence_error(_request: Request, _error: SQLAlchemyError) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={"detail": "persistence unavailable"},
        )

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
        brief_revision_id=brief_revision_id,
        brief_digest=brief_digest,
        graph_revision_id=graph_revision_id,
        graph_digest=graph_digest,
        item_id_1=item_id_1,
        item_id_2=item_id_2,
        item_id_3=item_id_3,
        req_id_1=req_id_1,
        req_id_2=req_id_2,
        item_inputs=item_inputs,
        snapshots=snapshots,
        revision=revision,
        accepted=accepted,
        epic=epic,
        settings=settings,
    )


@pytest.fixture
async def epic_items_client(epic_items_route_context: SimpleNamespace):
    settings = epic_items_route_context.settings
    async with AsyncClient(
        transport=ASGITransport(app=epic_items_route_context.app),
        base_url=settings.web_origin,
    ) as client:
        client.cookies.set("forge_session", epic_items_route_context.auth.session_token)
        yield client


def _graph_create_payload(
    ctx: SimpleNamespace, items: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    if items is None:
        items = [
            {
                "item_id": str(ctx.item_id_1),
                "disposition": "required",
                "ordinal": 1,
                "title": "Foundational work item",
                "outcome": "Foundation is built",
                "acceptance_criteria": ["Foundation passes checks"],
                "source_requirement_ids": [str(ctx.req_id_1)],
                "dependency_item_ids": [],
            },
            {
                "item_id": str(ctx.item_id_2),
                "disposition": "required",
                "ordinal": 2,
                "title": "Dependent work item",
                "outcome": "Feature depending on foundation",
                "acceptance_criteria": ["Feature functions correctly"],
                "source_requirement_ids": [str(ctx.req_id_2)],
                "dependency_item_ids": [str(ctx.item_id_1)],
            },
            {
                "item_id": str(ctx.item_id_3),
                "disposition": "deferred",
                "ordinal": 3,
                "title": "Deferred work item",
                "outcome": "Future enhancement",
                "acceptance_criteria": ["Enhancement is specified"],
                "source_requirement_ids": [str(ctx.req_id_2)],
                "dependency_item_ids": [str(ctx.item_id_2)],
            },
        ]
    return {
        "schema_version": 1,
        "expected_epic_version": 2,
        "brief_revision_id": str(ctx.brief_revision_id),
        "brief_digest": ctx.brief_digest,
        "items": items,
    }


def _graph_adoption_payload(ctx: SimpleNamespace) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "expected_epic_version": 2,
        "graph_revision_id": str(ctx.graph_revision_id),
        "graph_digest": ctx.graph_digest,
    }


# =========================================================================
# Schema export and alias tests
# =========================================================================


def test_schema_exports_and_aliases() -> None:
    assert GraphRevisionCreate is GraphRevisionCreateRequest
    assert GraphAdoption is GraphAdoptionRequest
    assert issubclass(GraphRevisionResponse, GraphRevisionRecord)
    assert issubclass(AcceptedGraphResponse, AcceptedGraph)
    assert issubclass(EpicResponse, EpicRecord)
    assert issubclass(ItemSnapshot, ItemInput)
    assert ItemReadiness is not None


# =========================================================================
# Route 1: POST /api/epics/{epic_id}/graph-revisions
# =========================================================================


@pytest.mark.asyncio
async def test_create_graph_revision_success_and_readiness(
    epic_items_client: AsyncClient,
    epic_items_route_context: SimpleNamespace,
    route_headers: dict[str, str],
) -> None:
    ctx = epic_items_route_context
    headers = {**route_headers, "Idempotency-Key": "graph-save-key-1"}
    payload = _graph_create_payload(ctx)

    response = await epic_items_client.post(
        f"/api/epics/{ctx.epic_id}/graph-revisions",
        headers=headers,
        json=payload,
    )
    assert response.status_code == 201

    data = response.json()
    assert data["schema_version"] == 1
    assert data["graph_revision_id"] == str(ctx.graph_revision_id)
    assert data["epic_id"] == str(ctx.epic_id)
    assert data["brief_revision_id"] == str(ctx.brief_revision_id)
    assert data["brief_digest"] == ctx.brief_digest
    assert data["revision_number"] == 1
    assert data["epic_version"] == 2
    assert data["graph_digest"] == ctx.graph_digest
    assert len(data["items"]) == 3

    # Verify deterministic readiness projection
    readiness = data["readiness"]
    assert len(readiness) == 3
    # item 1: required, no dependencies -> ready
    assert readiness[0]["item_id"] == str(ctx.item_id_1)
    assert readiness[0]["status"] == "ready"
    assert "execution eligibility is checked separately" in readiness[0]["reason"]
    assert readiness[0]["dependency_item_ids"] == []

    # item 2: required, has dependencies -> blocked
    assert readiness[1]["item_id"] == str(ctx.item_id_2)
    assert readiness[1]["status"] == "blocked"
    assert "Pending verified prerequisite integration" in readiness[1]["reason"]
    assert readiness[1]["dependency_item_ids"] == [str(ctx.item_id_1)]

    # item 3: deferred -> deferred
    assert readiness[2]["item_id"] == str(ctx.item_id_3)
    assert readiness[2]["status"] == "deferred"
    assert "Explicitly deferred" in readiness[2]["reason"]

    # Verify service call parameters
    assert len(ctx.service.save_revision_calls) == 1
    actor, epic_id, key, req = ctx.service.save_revision_calls[0]
    assert actor.actor_id == ctx.auth.actor.actor_id
    assert epic_id == ctx.epic_id
    assert key == "graph-save-key-1"
    assert req.expected_epic_version == 2
    assert req.brief_revision_id == ctx.brief_revision_id
    assert req.brief_digest == ctx.brief_digest
    assert len(req.items) == 3


@pytest.mark.asyncio
async def test_create_graph_revision_mutation_categories(
    epic_items_client: AsyncClient,
    epic_items_route_context: SimpleNamespace,
    route_headers: dict[str, str],
) -> None:
    """Verify save snapshot handles manual create, edit, split, combine, reorder, defer, and edge changes."""
    ctx = epic_items_route_context
    headers = {**route_headers, "Idempotency-Key": "graph-save-edit-1"}

    # 1. Edit: modify title, outcome, and criteria
    edited_items = [
        {
            "item_id": str(ctx.item_id_1),
            "disposition": "required",
            "ordinal": 1,
            "title": "Edited title",
            "outcome": "Edited outcome",
            "acceptance_criteria": ["New criterion 1", "New criterion 2"],
            "source_requirement_ids": [str(ctx.req_id_1)],
            "dependency_item_ids": [],
        }
    ]
    resp = await epic_items_client.post(
        f"/api/epics/{ctx.epic_id}/graph-revisions",
        headers=headers,
        json=_graph_create_payload(ctx, edited_items),
    )
    assert resp.status_code == 201

    # 2. Split: one item replaced with two items
    split_id_a = str(uuid4())
    split_id_b = str(uuid4())
    split_items = [
        {
            "item_id": split_id_a,
            "disposition": "required",
            "ordinal": 1,
            "title": "Part A",
            "outcome": "First part",
            "acceptance_criteria": ["Crit A"],
            "source_requirement_ids": [str(ctx.req_id_1)],
            "dependency_item_ids": [],
        },
        {
            "item_id": split_id_b,
            "disposition": "required",
            "ordinal": 2,
            "title": "Part B",
            "outcome": "Second part",
            "acceptance_criteria": ["Crit B"],
            "source_requirement_ids": [str(ctx.req_id_1)],
            "dependency_item_ids": [split_id_a],
        },
    ]
    resp = await epic_items_client.post(
        f"/api/epics/{ctx.epic_id}/graph-revisions",
        headers={**route_headers, "Idempotency-Key": "graph-save-split-1"},
        json=_graph_create_payload(ctx, split_items),
    )
    assert resp.status_code == 201

    # 3. Combine: multiple items combined into a single item
    combined_items = [
        {
            "item_id": str(ctx.item_id_1),
            "disposition": "required",
            "ordinal": 1,
            "title": "Combined item",
            "outcome": "Combined outcome",
            "acceptance_criteria": ["Crit A", "Crit B"],
            "source_requirement_ids": [str(ctx.req_id_1), str(ctx.req_id_2)],
            "dependency_item_ids": [],
        }
    ]
    resp = await epic_items_client.post(
        f"/api/epics/{ctx.epic_id}/graph-revisions",
        headers={**route_headers, "Idempotency-Key": "graph-save-combine-1"},
        json=_graph_create_payload(ctx, combined_items),
    )
    assert resp.status_code == 201

    # 4. Reorder: ordinals changed
    reordered_items = [
        {
            "item_id": str(ctx.item_id_1),
            "disposition": "required",
            "ordinal": 10,
            "title": "First",
            "outcome": "O1",
            "acceptance_criteria": ["C1"],
            "source_requirement_ids": [str(ctx.req_id_1)],
            "dependency_item_ids": [],
        },
        {
            "item_id": str(ctx.item_id_2),
            "disposition": "required",
            "ordinal": 5,
            "title": "Second",
            "outcome": "O2",
            "acceptance_criteria": ["C2"],
            "source_requirement_ids": [str(ctx.req_id_2)],
            "dependency_item_ids": [],
        },
    ]
    resp = await epic_items_client.post(
        f"/api/epics/{ctx.epic_id}/graph-revisions",
        headers={**route_headers, "Idempotency-Key": "graph-save-reorder-1"},
        json=_graph_create_payload(ctx, reordered_items),
    )
    assert resp.status_code == 201

    # 5. Defer: item disposition switched to deferred
    deferred_switch = [
        {
            "item_id": str(ctx.item_id_1),
            "disposition": "deferred",
            "ordinal": 1,
            "title": "Deferred item",
            "outcome": "O1",
            "acceptance_criteria": ["C1"],
            "source_requirement_ids": [str(ctx.req_id_1)],
            "dependency_item_ids": [],
        }
    ]
    resp = await epic_items_client.post(
        f"/api/epics/{ctx.epic_id}/graph-revisions",
        headers={**route_headers, "Idempotency-Key": "graph-save-defer-1"},
        json=_graph_create_payload(ctx, deferred_switch),
    )
    assert resp.status_code == 201

    # 6. Edge changes: dependencies removed or added
    edge_change = [
        {
            "item_id": str(ctx.item_id_1),
            "disposition": "required",
            "ordinal": 1,
            "title": "First",
            "outcome": "O1",
            "acceptance_criteria": ["C1"],
            "source_requirement_ids": [str(ctx.req_id_1)],
            "dependency_item_ids": [],
        },
        {
            "item_id": str(ctx.item_id_2),
            "disposition": "required",
            "ordinal": 2,
            "title": "Second",
            "outcome": "O2",
            "acceptance_criteria": ["C2"],
            "source_requirement_ids": [str(ctx.req_id_2)],
            "dependency_item_ids": [str(ctx.item_id_1)],
        },
    ]
    resp = await epic_items_client.post(
        f"/api/epics/{ctx.epic_id}/graph-revisions",
        headers={**route_headers, "Idempotency-Key": "graph-save-edge-1"},
        json=_graph_create_payload(ctx, edge_change),
    )
    assert resp.status_code == 201

    # 7. Empty draft: empty items list allowed on save
    resp = await epic_items_client.post(
        f"/api/epics/{ctx.epic_id}/graph-revisions",
        headers={**route_headers, "Idempotency-Key": "graph-save-empty-1"},
        json=_graph_create_payload(ctx, []),
    )
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_create_graph_revision_rejects_closed_schema_and_bounds(
    epic_items_client: AsyncClient,
    epic_items_route_context: SimpleNamespace,
    route_headers: dict[str, str],
) -> None:
    ctx = epic_items_route_context
    headers = {**route_headers, "Idempotency-Key": "graph-save-bounds-1"}

    # Extra client-supplied digest on top-level request
    payload = _graph_create_payload(ctx)
    payload["graph_digest"] = "c" * 64
    resp = await epic_items_client.post(
        f"/api/epics/{ctx.epic_id}/graph-revisions", headers=headers, json=payload
    )
    assert resp.status_code == 422

    # Extra client-supplied status on item
    payload = _graph_create_payload(ctx)
    payload["items"][0]["status"] = "ready"
    resp = await epic_items_client.post(
        f"/api/epics/{ctx.epic_id}/graph-revisions", headers=headers, json=payload
    )
    assert resp.status_code == 422

    # Extra client-supplied attempt/completion field on item
    payload = _graph_create_payload(ctx)
    payload["items"][0]["attempt_id"] = str(uuid4())
    resp = await epic_items_client.post(
        f"/api/epics/{ctx.epic_id}/graph-revisions", headers=headers, json=payload
    )
    assert resp.status_code == 422

    # Invalid brief digest format (not 64 hex chars)
    payload = _graph_create_payload(ctx)
    payload["brief_digest"] = "not-a-valid-hex-digest"
    resp = await epic_items_client.post(
        f"/api/epics/{ctx.epic_id}/graph-revisions", headers=headers, json=payload
    )
    assert resp.status_code == 422

    # Blank title
    payload = _graph_create_payload(ctx)
    payload["items"][0]["title"] = "   "
    resp = await epic_items_client.post(
        f"/api/epics/{ctx.epic_id}/graph-revisions", headers=headers, json=payload
    )
    assert resp.status_code == 422

    # Title exceeding 256 UTF-8 bytes
    payload = _graph_create_payload(ctx)
    payload["items"][0]["title"] = "é" * 129
    resp = await epic_items_client.post(
        f"/api/epics/{ctx.epic_id}/graph-revisions", headers=headers, json=payload
    )
    assert resp.status_code == 422

    # Outcome exceeding 5000 characters
    payload = _graph_create_payload(ctx)
    payload["items"][0]["outcome"] = "x" * 5001
    resp = await epic_items_client.post(
        f"/api/epics/{ctx.epic_id}/graph-revisions", headers=headers, json=payload
    )
    assert resp.status_code == 422

    # Empty acceptance criteria
    payload = _graph_create_payload(ctx)
    payload["items"][0]["acceptance_criteria"] = []
    resp = await epic_items_client.post(
        f"/api/epics/{ctx.epic_id}/graph-revisions", headers=headers, json=payload
    )
    assert resp.status_code == 422

    # More than 64 acceptance criteria
    payload = _graph_create_payload(ctx)
    payload["items"][0]["acceptance_criteria"] = [f"Crit {i}" for i in range(65)]
    resp = await epic_items_client.post(
        f"/api/epics/{ctx.epic_id}/graph-revisions", headers=headers, json=payload
    )
    assert resp.status_code == 422

    # Empty source requirements
    payload = _graph_create_payload(ctx)
    payload["items"][0]["source_requirement_ids"] = []
    resp = await epic_items_client.post(
        f"/api/epics/{ctx.epic_id}/graph-revisions", headers=headers, json=payload
    )
    assert resp.status_code == 422

    # Duplicate source requirement references
    payload = _graph_create_payload(ctx)
    payload["items"][0]["source_requirement_ids"] = [str(ctx.req_id_1), str(ctx.req_id_1)]
    resp = await epic_items_client.post(
        f"/api/epics/{ctx.epic_id}/graph-revisions", headers=headers, json=payload
    )
    assert resp.status_code == 422

    # Duplicate dependency references
    payload = _graph_create_payload(ctx)
    payload["items"][1]["dependency_item_ids"] = [str(ctx.item_id_1), str(ctx.item_id_1)]
    resp = await epic_items_client.post(
        f"/api/epics/{ctx.epic_id}/graph-revisions", headers=headers, json=payload
    )
    assert resp.status_code == 422

    # Invalid disposition (not required or deferred)
    payload = _graph_create_payload(ctx)
    payload["items"][0]["disposition"] = "optional"
    resp = await epic_items_client.post(
        f"/api/epics/{ctx.epic_id}/graph-revisions", headers=headers, json=payload
    )
    assert resp.status_code == 422

    # Negative ordinal
    payload = _graph_create_payload(ctx)
    payload["items"][0]["ordinal"] = -1
    resp = await epic_items_client.post(
        f"/api/epics/{ctx.epic_id}/graph-revisions", headers=headers, json=payload
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_graph_revision_error_translations(
    epic_items_client: AsyncClient,
    epic_items_route_context: SimpleNamespace,
    route_headers: dict[str, str],
) -> None:
    ctx = epic_items_route_context
    headers = {**route_headers, "Idempotency-Key": "graph-save-err-1"}
    payload = _graph_create_payload(ctx)

    # 404: EpicNotFound
    ctx.service.error = EpicNotFound("missing epic")
    resp = await epic_items_client.post(
        f"/api/epics/{ctx.epic_id}/graph-revisions", headers=headers, json=payload
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "resource not found"

    # 409: EpicVersionConflict
    ctx.service.error = EpicVersionConflict("stale epic version")
    resp = await epic_items_client.post(
        f"/api/epics/{ctx.epic_id}/graph-revisions", headers=headers, json=payload
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "request conflicts with current state"

    # 409: BriefNotAccepted
    ctx.service.error = BriefNotAccepted("no brief accepted")
    resp = await epic_items_client.post(
        f"/api/epics/{ctx.epic_id}/graph-revisions", headers=headers, json=payload
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "request conflicts with current state"

    # 409: BriefBindingConflict
    ctx.service.error = BriefBindingConflict("mismatched brief binding")
    resp = await epic_items_client.post(
        f"/api/epics/{ctx.epic_id}/graph-revisions", headers=headers, json=payload
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "request conflicts with current state"

    # 409: MutationConflict
    ctx.service.error = MutationConflict("key already used with different payload")
    resp = await epic_items_client.post(
        f"/api/epics/{ctx.epic_id}/graph-revisions", headers=headers, json=payload
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "request conflicts with current state"

    # 422: GraphValidationError
    ctx.service.error = GraphValidationError("graph contains cycle")
    resp = await epic_items_client.post(
        f"/api/epics/{ctx.epic_id}/graph-revisions", headers=headers, json=payload
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == "request cannot be processed"

    # 503: SQLAlchemyError
    ctx.service.error = SQLAlchemyError("database down")
    resp = await epic_items_client.post(
        f"/api/epics/{ctx.epic_id}/graph-revisions", headers=headers, json=payload
    )
    assert resp.status_code == 503
    assert resp.json()["detail"] == "persistence unavailable"

    # 503: Unknown service error
    ctx.service.error = RuntimeError("unexpected failure")
    resp = await epic_items_client.post(
        f"/api/epics/{ctx.epic_id}/graph-revisions", headers=headers, json=payload
    )
    assert resp.status_code == 503
    assert resp.json()["detail"] == "service unavailable"


# =========================================================================
# Route 2: GET /api/epics/{epic_id}/graph-revisions
# =========================================================================


@pytest.mark.asyncio
async def test_list_graph_revisions_success_and_errors(
    epic_items_client: AsyncClient,
    epic_items_route_context: SimpleNamespace,
    route_headers: dict[str, str],
) -> None:
    ctx = epic_items_route_context
    host_headers = {"Host": route_headers["Host"]}

    # Success
    resp = await epic_items_client.get(
        f"/api/epics/{ctx.epic_id}/graph-revisions",
        headers=host_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["graph_revision_id"] == str(ctx.graph_revision_id)
    assert "readiness" in data[0]
    assert ctx.service.list_revisions_calls == [ctx.epic_id]

    # Error translation 404
    ctx.service.error = EpicNotFound("missing epic")
    resp = await epic_items_client.get(
        f"/api/epics/{ctx.epic_id}/graph-revisions",
        headers=host_headers,
    )
    assert resp.status_code == 404

    # Error translation 503
    ctx.service.error = SQLAlchemyError("db fail")
    resp = await epic_items_client.get(
        f"/api/epics/{ctx.epic_id}/graph-revisions",
        headers=host_headers,
    )
    assert resp.status_code == 503


# =========================================================================
# Route 3: GET /api/epics/{epic_id}/graph-revisions/{graph_revision_id}
# =========================================================================


@pytest.mark.asyncio
async def test_get_graph_revision_success_and_errors(
    epic_items_client: AsyncClient,
    epic_items_route_context: SimpleNamespace,
    route_headers: dict[str, str],
) -> None:
    ctx = epic_items_route_context
    host_headers = {"Host": route_headers["Host"]}

    # Success
    resp = await epic_items_client.get(
        f"/api/epics/{ctx.epic_id}/graph-revisions/{ctx.graph_revision_id}",
        headers=host_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["graph_revision_id"] == str(ctx.graph_revision_id)
    assert len(data["readiness"]) == 3
    assert ctx.service.get_revision_calls == [(ctx.epic_id, ctx.graph_revision_id)]

    # Error translation 404 (GraphRevisionNotFound)
    ctx.service.error = GraphRevisionNotFound("not found")
    resp = await epic_items_client.get(
        f"/api/epics/{ctx.epic_id}/graph-revisions/{ctx.graph_revision_id}",
        headers=host_headers,
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "resource not found"

    # Error translation 404 (EpicNotFound)
    ctx.service.error = EpicNotFound("epic not found")
    resp = await epic_items_client.get(
        f"/api/epics/{ctx.epic_id}/graph-revisions/{ctx.graph_revision_id}",
        headers=host_headers,
    )
    assert resp.status_code == 404

    # Error translation 503
    ctx.service.error = RuntimeError("unexpected")
    resp = await epic_items_client.get(
        f"/api/epics/{ctx.epic_id}/graph-revisions/{ctx.graph_revision_id}",
        headers=host_headers,
    )
    assert resp.status_code == 503


# =========================================================================
# Route 4: POST /api/epics/{epic_id}/graph-adoptions
# =========================================================================


@pytest.mark.asyncio
async def test_adopt_graph_revision_success(
    epic_items_client: AsyncClient,
    epic_items_route_context: SimpleNamespace,
    route_headers: dict[str, str],
) -> None:
    ctx = epic_items_route_context
    headers = {**route_headers, "Idempotency-Key": "graph-adopt-key-1"}
    payload = _graph_adoption_payload(ctx)

    response = await epic_items_client.post(
        f"/api/epics/{ctx.epic_id}/graph-adoptions",
        headers=headers,
        json=payload,
    )
    assert response.status_code == 200

    data = response.json()
    assert data["schema_version"] == 1
    assert data["epic_id"] == str(ctx.epic.epic_id)
    assert data["accepted_graph_revision_id"] == str(ctx.graph_revision_id)
    assert data["accepted_graph_digest"] == ctx.graph_digest

    assert len(ctx.service.adopt_revision_calls) == 1
    actor, epic_id, key, req = ctx.service.adopt_revision_calls[0]
    assert actor.actor_id == ctx.auth.actor.actor_id
    assert epic_id == ctx.epic_id
    assert key == "graph-adopt-key-1"
    assert req.expected_epic_version == 2
    assert req.graph_revision_id == ctx.graph_revision_id
    assert req.graph_digest == ctx.graph_digest


@pytest.mark.asyncio
async def test_adopt_graph_revision_validation_and_errors(
    epic_items_client: AsyncClient,
    epic_items_route_context: SimpleNamespace,
    route_headers: dict[str, str],
) -> None:
    ctx = epic_items_route_context
    headers = {**route_headers, "Idempotency-Key": "graph-adopt-err-1"}
    payload = _graph_adoption_payload(ctx)

    # Extra field rejected
    bad_payload = {**payload, "extra_field": "disallowed"}
    resp = await epic_items_client.post(
        f"/api/epics/{ctx.epic_id}/graph-adoptions", headers=headers, json=bad_payload
    )
    assert resp.status_code == 422

    # Invalid graph digest
    bad_payload = {**payload, "graph_digest": "not-valid"}
    resp = await epic_items_client.post(
        f"/api/epics/{ctx.epic_id}/graph-adoptions", headers=headers, json=bad_payload
    )
    assert resp.status_code == 422

    # 404: GraphRevisionNotFound
    ctx.service.error = GraphRevisionNotFound("not found")
    resp = await epic_items_client.post(
        f"/api/epics/{ctx.epic_id}/graph-adoptions", headers=headers, json=payload
    )
    assert resp.status_code == 404

    # 409: GraphBindingConflict
    ctx.service.error = GraphBindingConflict("digest mismatch")
    resp = await epic_items_client.post(
        f"/api/epics/{ctx.epic_id}/graph-adoptions", headers=headers, json=payload
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "request conflicts with current state"

    # 409: EpicVersionConflict
    ctx.service.error = EpicVersionConflict("stale")
    resp = await epic_items_client.post(
        f"/api/epics/{ctx.epic_id}/graph-adoptions", headers=headers, json=payload
    )
    assert resp.status_code == 409

    # 409: BriefBindingConflict
    ctx.service.error = BriefBindingConflict("brief mismatch")
    resp = await epic_items_client.post(
        f"/api/epics/{ctx.epic_id}/graph-adoptions", headers=headers, json=payload
    )
    assert resp.status_code == 409

    # 409: BriefNotAccepted
    ctx.service.error = BriefNotAccepted("no brief accepted")
    resp = await epic_items_client.post(
        f"/api/epics/{ctx.epic_id}/graph-adoptions", headers=headers, json=payload
    )
    assert resp.status_code == 409

    # 422: GraphValidationError (e.g. required depends on deferred)
    ctx.service.error = GraphValidationError("required item depends on deferred item")
    resp = await epic_items_client.post(
        f"/api/epics/{ctx.epic_id}/graph-adoptions", headers=headers, json=payload
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == "request cannot be processed"

    # 503: SQLAlchemyError
    ctx.service.error = SQLAlchemyError("db fail")
    resp = await epic_items_client.post(
        f"/api/epics/{ctx.epic_id}/graph-adoptions", headers=headers, json=payload
    )
    assert resp.status_code == 503


# =========================================================================
# Route 5: GET /api/epics/{epic_id}/accepted-graph
# =========================================================================


@pytest.mark.asyncio
async def test_get_accepted_graph_success_and_readiness(
    epic_items_client: AsyncClient,
    epic_items_route_context: SimpleNamespace,
    route_headers: dict[str, str],
) -> None:
    ctx = epic_items_route_context
    host_headers = {"Host": route_headers["Host"]}

    resp = await epic_items_client.get(
        f"/api/epics/{ctx.epic_id}/accepted-graph",
        headers=host_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["schema_version"] == 1
    assert data["epic_id"] == str(ctx.epic_id)
    assert data["brief_revision_id"] == str(ctx.brief_revision_id)
    assert data["brief_digest"] == ctx.brief_digest
    assert data["graph_revision_id"] == str(ctx.graph_revision_id)
    assert data["graph_digest"] == ctx.graph_digest
    assert len(data["items"]) == 3
    assert len(data["readiness"]) == 3
    assert data["readiness"][0]["status"] == "ready"
    assert data["readiness"][1]["status"] == "blocked"
    assert data["readiness"][2]["status"] == "deferred"
    assert ctx.service.accepted_calls == [ctx.epic_id]


@pytest.mark.asyncio
async def test_get_accepted_graph_error_translations(
    epic_items_client: AsyncClient,
    epic_items_route_context: SimpleNamespace,
    route_headers: dict[str, str],
) -> None:
    ctx = epic_items_route_context
    host_headers = {"Host": route_headers["Host"]}

    # 409: GraphNotAccepted
    ctx.service.error = GraphNotAccepted("no graph accepted")
    resp = await epic_items_client.get(
        f"/api/epics/{ctx.epic_id}/accepted-graph",
        headers=host_headers,
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "request conflicts with current state"

    # 409: GraphBindingConflict
    ctx.service.error = GraphBindingConflict("digest mismatch")
    resp = await epic_items_client.get(
        f"/api/epics/{ctx.epic_id}/accepted-graph",
        headers=host_headers,
    )
    assert resp.status_code == 409

    # 409: BriefNotAccepted
    ctx.service.error = BriefNotAccepted("no brief accepted")
    resp = await epic_items_client.get(
        f"/api/epics/{ctx.epic_id}/accepted-graph",
        headers=host_headers,
    )
    assert resp.status_code == 409

    # 409: BriefBindingConflict
    ctx.service.error = BriefBindingConflict("brief mismatch")
    resp = await epic_items_client.get(
        f"/api/epics/{ctx.epic_id}/accepted-graph",
        headers=host_headers,
    )
    assert resp.status_code == 409

    # 404: EpicNotFound
    ctx.service.error = EpicNotFound("not found")
    resp = await epic_items_client.get(
        f"/api/epics/{ctx.epic_id}/accepted-graph",
        headers=host_headers,
    )
    assert resp.status_code == 404

    # 503: SQLAlchemyError
    ctx.service.error = SQLAlchemyError("db fail")
    resp = await epic_items_client.get(
        f"/api/epics/{ctx.epic_id}/accepted-graph",
        headers=host_headers,
    )
    assert resp.status_code == 503


# =========================================================================
# Auth, CSRF, and Idempotency failures
# =========================================================================


@pytest.mark.asyncio
async def test_auth_and_csrf_and_idempotency_failures(
    epic_items_route_context: SimpleNamespace,
    route_headers: dict[str, str],
) -> None:
    ctx = epic_items_route_context
    payload = _graph_create_payload(ctx)

    async with AsyncClient(
        transport=ASGITransport(app=ctx.app), base_url=ctx.settings.web_origin
    ) as unauthed_client:
        # 1. Missing session cookie on mutation -> 401
        headers = {**route_headers, "Idempotency-Key": "key-1"}
        resp = await unauthed_client.post(
            f"/api/epics/{ctx.epic_id}/graph-revisions", headers=headers, json=payload
        )
        assert resp.status_code == 401

        # 2. Missing session cookie on read -> 401
        resp = await unauthed_client.get(
            f"/api/epics/{ctx.epic_id}/graph-revisions", headers={"Host": route_headers["Host"]}
        )
        assert resp.status_code == 401

        # Set session cookie
        unauthed_client.cookies.set("forge_session", ctx.auth.session_token)

        # 3. Missing CSRF header on mutation -> 403
        no_csrf_headers = {
            "Host": route_headers["Host"],
            "Origin": route_headers["Origin"],
            "Idempotency-Key": "key-2",
        }
        resp = await unauthed_client.post(
            f"/api/epics/{ctx.epic_id}/graph-revisions", headers=no_csrf_headers, json=payload
        )
        assert resp.status_code == 403

        # 4. Invalid CSRF header on mutation -> 403
        bad_csrf_headers = {
            "Host": route_headers["Host"],
            "Origin": route_headers["Origin"],
            "X-CSRF-Token": "wrong-csrf-token",
            "Idempotency-Key": "key-3",
        }
        resp = await unauthed_client.post(
            f"/api/epics/{ctx.epic_id}/graph-revisions", headers=bad_csrf_headers, json=payload
        )
        assert resp.status_code == 403

        # 5. Missing Idempotency-Key on mutation -> 422
        resp = await unauthed_client.post(
            f"/api/epics/{ctx.epic_id}/graph-revisions", headers=route_headers, json=payload
        )
        assert resp.status_code == 422
        assert resp.json()["detail"] == "invalid idempotency key"

        # 6. Blank Idempotency-Key -> 422
        blank_key_headers = {**route_headers, "Idempotency-Key": "   "}
        resp = await unauthed_client.post(
            f"/api/epics/{ctx.epic_id}/graph-revisions", headers=blank_key_headers, json=payload
        )
        assert resp.status_code == 422

        # 7. Non-operator actor -> 403
        ctx.auth.actor = SimpleNamespace(
            actor_id=uuid4(), actor_class="worker", session_id=uuid4()
        )
        valid_headers = {**route_headers, "Idempotency-Key": "key-4"}
        resp = await unauthed_client.post(
            f"/api/epics/{ctx.epic_id}/graph-revisions", headers=valid_headers, json=payload
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == "operator authorization required"


# =========================================================================
# Unconfigured service (500)
# =========================================================================


@pytest.mark.asyncio
async def test_unconfigured_service_returns_500(
    epic_items_route_context: SimpleNamespace,
    route_headers: dict[str, str],
) -> None:
    ctx = epic_items_route_context
    unconfigured_app = FastAPI()
    unconfigured_app.state.settings = ctx.settings
    unconfigured_app.state.auth_service = ctx.auth
    unconfigured_app.state.epic_items_service = None
    unconfigured_app.include_router(router_for(), prefix="/api")

    async with AsyncClient(
        transport=ASGITransport(app=unconfigured_app), base_url=ctx.settings.web_origin
    ) as client:
        client.cookies.set("forge_session", ctx.auth.session_token)
        headers = {**route_headers, "Idempotency-Key": "unconfig-key-1"}

        resp1 = await client.post(
            f"/api/epics/{ctx.epic_id}/graph-revisions",
            headers=headers,
            json=_graph_create_payload(ctx),
        )
        assert resp1.status_code == 500
        assert resp1.json()["detail"] == "API service is not configured"

        resp2 = await client.get(
            f"/api/epics/{ctx.epic_id}/graph-revisions",
            headers={"Host": route_headers["Host"]},
        )
        assert resp2.status_code == 500

        resp3 = await client.get(
            f"/api/epics/{ctx.epic_id}/graph-revisions/{ctx.graph_revision_id}",
            headers={"Host": route_headers["Host"]},
        )
        assert resp3.status_code == 500

        resp4 = await client.post(
            f"/api/epics/{ctx.epic_id}/graph-adoptions",
            headers=headers,
            json=_graph_adoption_payload(ctx),
        )
        assert resp4.status_code == 500

        resp5 = await client.get(
            f"/api/epics/{ctx.epic_id}/accepted-graph",
            headers={"Host": route_headers["Host"]},
        )
        assert resp5.status_code == 500


# =========================================================================
# Credentials, cycles, dangling edges, and extra completion fields
# =========================================================================


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
    epic_items_client: AsyncClient,
    epic_items_route_context: SimpleNamespace,
    route_headers: dict[str, str],
    secret_value: str,
) -> None:
    ctx = epic_items_route_context
    headers = {**route_headers, "Idempotency-Key": "secret-key-1"}

    # Secret in title
    title_payload = _graph_create_payload(ctx)
    title_payload["items"][0]["title"] = f"Item {secret_value}"
    res1 = await epic_items_client.post(
        f"/api/epics/{ctx.epic_id}/graph-revisions", headers=headers, json=title_payload
    )
    assert res1.status_code == 422
    assert "supersecrettoken" not in res1.text
    assert "secretpassword" not in res1.text

    # Secret in outcome
    outcome_payload = _graph_create_payload(ctx)
    outcome_payload["items"][0]["outcome"] = f"Outcome with {secret_value}"
    res2 = await epic_items_client.post(
        f"/api/epics/{ctx.epic_id}/graph-revisions", headers=headers, json=outcome_payload
    )
    assert res2.status_code == 422
    assert "supersecrettoken" not in res2.text

    # Secret in criteria
    crit_payload = _graph_create_payload(ctx)
    crit_payload["items"][0]["acceptance_criteria"] = [f"Crit with {secret_value}"]
    res3 = await epic_items_client.post(
        f"/api/epics/{ctx.epic_id}/graph-revisions", headers=headers, json=crit_payload
    )
    assert res3.status_code == 422
    assert "supersecrettoken" not in res3.text


@pytest.mark.asyncio
async def test_create_graph_revision_rejects_cycle_self_and_dangling_dependencies(
    epic_items_client: AsyncClient,
    epic_items_route_context: SimpleNamespace,
    route_headers: dict[str, str],
) -> None:
    ctx = epic_items_route_context
    headers = {**route_headers, "Idempotency-Key": "graph-cycle-key"}

    # Cycle: item 1 depends on item 2, item 2 depends on item 1
    cycle_items = [
        {
            "item_id": str(ctx.item_id_1),
            "disposition": "required",
            "ordinal": 1,
            "title": "Item 1",
            "outcome": "Outcome 1",
            "acceptance_criteria": ["Crit 1"],
            "source_requirement_ids": [str(ctx.req_id_1)],
            "dependency_item_ids": [str(ctx.item_id_2)],
        },
        {
            "item_id": str(ctx.item_id_2),
            "disposition": "required",
            "ordinal": 2,
            "title": "Item 2",
            "outcome": "Outcome 2",
            "acceptance_criteria": ["Crit 2"],
            "source_requirement_ids": [str(ctx.req_id_2)],
            "dependency_item_ids": [str(ctx.item_id_1)],
        },
    ]
    ctx.service.error = GraphValidationError("graph contains cycle")
    res1 = await epic_items_client.post(
        f"/api/epics/{ctx.epic_id}/graph-revisions",
        headers=headers,
        json=_graph_create_payload(ctx, cycle_items),
    )
    assert res1.status_code == 422
    assert res1.json()["detail"] == "request cannot be processed"

    # Self-edge: item 1 depends on item 1
    self_items = [
        {
            "item_id": str(ctx.item_id_1),
            "disposition": "required",
            "ordinal": 1,
            "title": "Item 1",
            "outcome": "Outcome 1",
            "acceptance_criteria": ["Crit 1"],
            "source_requirement_ids": [str(ctx.req_id_1)],
            "dependency_item_ids": [str(ctx.item_id_1)],
        }
    ]
    ctx.service.error = GraphValidationError("graph edge is invalid")
    res2 = await epic_items_client.post(
        f"/api/epics/{ctx.epic_id}/graph-revisions",
        headers={**route_headers, "Idempotency-Key": "graph-self-key"},
        json=_graph_create_payload(ctx, self_items),
    )
    assert res2.status_code == 422
    assert res2.json()["detail"] == "request cannot be processed"

    # Dangling edge: item 1 depends on an unknown node
    dangling_items = [
        {
            "item_id": str(ctx.item_id_1),
            "disposition": "required",
            "ordinal": 1,
            "title": "Item 1",
            "outcome": "Outcome 1",
            "acceptance_criteria": ["Crit 1"],
            "source_requirement_ids": [str(ctx.req_id_1)],
            "dependency_item_ids": [str(uuid4())],
        }
    ]
    ctx.service.error = GraphValidationError("graph edge is invalid")
    res3 = await epic_items_client.post(
        f"/api/epics/{ctx.epic_id}/graph-revisions",
        headers={**route_headers, "Idempotency-Key": "graph-dangling-key"},
        json=_graph_create_payload(ctx, dangling_items),
    )
    assert res3.status_code == 422
    assert res3.json()["detail"] == "request cannot be processed"


@pytest.mark.asyncio
async def test_create_graph_revision_rejects_extra_completion_and_digest_fields(
    epic_items_client: AsyncClient,
    epic_items_route_context: SimpleNamespace,
    route_headers: dict[str, str],
) -> None:
    ctx = epic_items_route_context
    headers = {**route_headers, "Idempotency-Key": "extra-comp-key"}

    # Extra completion field
    payload = _graph_create_payload(ctx)
    payload["items"][0]["completion_proof"] = {"status": "verified"}
    res = await epic_items_client.post(
        f"/api/epics/{ctx.epic_id}/graph-revisions", headers=headers, json=payload
    )
    assert res.status_code == 422

    # Extra item_digest field
    payload = _graph_create_payload(ctx)
    payload["items"][0]["item_digest"] = "d" * 64
    res2 = await epic_items_client.post(
        f"/api/epics/{ctx.epic_id}/graph-revisions", headers=headers, json=payload
    )
    assert res2.status_code == 422

    # Extra completed boolean
    payload = _graph_create_payload(ctx)
    payload["items"][0]["completed"] = True
    res3 = await epic_items_client.post(
        f"/api/epics/{ctx.epic_id}/graph-revisions", headers=headers, json=payload
    )
    assert res3.status_code == 422


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
    epic_items_client: AsyncClient,
    epic_items_route_context: SimpleNamespace,
    route_headers: dict[str, str],
    security_headers: dict[str, str],
) -> None:
    ctx = epic_items_route_context
    headers = {
        **security_headers,
        "X-CSRF-Token": route_headers["X-CSRF-Token"],
        "Idempotency-Key": "security-headers-key",
    }
    # POST graph-revisions
    res1 = await epic_items_client.post(
        f"/api/epics/{ctx.epic_id}/graph-revisions",
        headers=headers,
        json=_graph_create_payload(ctx),
    )
    assert res1.status_code == 403

    # POST graph-adoptions
    res2 = await epic_items_client.post(
        f"/api/epics/{ctx.epic_id}/graph-adoptions",
        headers=headers,
        json=_graph_adoption_payload(ctx),
    )
    assert res2.status_code == 403
