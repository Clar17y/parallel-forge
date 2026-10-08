"""Owner selection must preserve the exact accepted source binding."""

from __future__ import annotations

from uuid import uuid4

import pytest
from forge.application.services.epic_brief import EpicBriefService
from forge.application.services.epic_items import EpicItemsService
from forge.persistence.unit_of_work import PostgresUnitOfWork

pytest_plugins = (
    "apps.orchestrator.tests.persistence.conftest",
    "apps.orchestrator.tests.api.conftest",
)


@pytest.mark.integration
async def test_reselecting_identical_brief_retains_graph_but_new_brief_clears_it(
    task10_client, task10_route_context, route_headers, session_factory, persisted_run
) -> None:
    app = task10_route_context.app
    app.state.epic_brief_service = EpicBriefService(lambda: PostgresUnitOfWork(session_factory))
    app.state.epic_items_service = EpicItemsService(lambda: PostgresUnitOfWork(session_factory))

    async def post(path: str, key: str, body: dict, expected: int = 201) -> dict:
        response = await task10_client.post(
            path, headers={**route_headers, "Idempotency-Key": key}, json=body
        )
        assert response.status_code == expected, response.text
        return response.json()

    requirement_id, item_id = str(uuid4()), str(uuid4())
    brief = {
        "problem": "An owner needs a stable accepted source",
        "outcomes": ["The selected graph survives a repeated brief selection"],
        "requirements": [{
            "requirement_id": requirement_id,
            "text": "Retain the accepted graph",
            "acceptance_criteria": ["A repeated selection retains the graph identity"],
        }],
    }
    epic = await post(
        "/api/epics", "create", {
            "project_id": str(persisted_run.project_id), "title": "Selection boundary", "draft": brief
        }
    )
    path = f"/api/epics/{epic['epic_id']}"
    first = await post(
        f"{path}/brief-revisions", "save-first",
        {"expected_epic_version": 1, "content": brief},
    )
    await post(
        f"{path}/brief-adoptions", "adopt-first", {
            "expected_epic_version": 2,
            "brief_revision_id": first["brief_revision_id"],
            "brief_digest": first["content_digest"],
        }, 200,
    )
    graph = await post(
        f"{path}/graph-revisions", "save-graph", {
            "expected_epic_version": 3,
            "brief_revision_id": first["brief_revision_id"],
            "brief_digest": first["content_digest"],
            "items": [{
                "item_id": item_id, "disposition": "required", "ordinal": 1,
                "title": "Retain graph", "outcome": "Graph identity is stable",
                "acceptance_criteria": ["Selection remains exact"],
                "source_requirement_ids": [requirement_id],
                "dependency_item_ids": [],
            }],
        },
    )
    selected = await post(
        f"{path}/graph-adoptions", "adopt-graph", {
            "expected_epic_version": 4,
            "graph_revision_id": graph["graph_revision_id"],
            "graph_digest": graph["graph_digest"],
        }, 200,
    )
    assert selected["accepted_graph_revision_id"] == graph["graph_revision_id"]

    repeated = await post(
        f"{path}/brief-adoptions", "reselect-first", {
            "expected_epic_version": 5,
            "brief_revision_id": first["brief_revision_id"],
            "brief_digest": first["content_digest"],
        }, 200,
    )
    assert repeated["accepted_graph_revision_id"] == graph["graph_revision_id"]
    assert repeated["accepted_graph_digest"] == graph["graph_digest"]
    assert (await task10_client.get(
        f"{path}/accepted-graph", headers={"Host": route_headers["Host"]}
    )).json()["graph_revision_id"] == graph["graph_revision_id"]

    changed = {**brief, "problem": "A revised accepted source"}
    second = await post(
        f"{path}/brief-revisions", "save-second", {
            "expected_epic_version": repeated["version"], "content": changed
        },
    )
    replaced = await post(
        f"{path}/brief-adoptions", "adopt-second", {
            "expected_epic_version": second["epic_version"],
            "brief_revision_id": second["brief_revision_id"],
            "brief_digest": second["content_digest"],
        }, 200,
    )
    assert replaced["accepted_graph_revision_id"] is None
    assert replaced["accepted_graph_digest"] is None
