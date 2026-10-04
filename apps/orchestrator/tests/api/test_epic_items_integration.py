"""HTTP/PostgreSQL graph authoring, brief changes and retained run evidence."""

from __future__ import annotations

import json
from uuid import UUID, uuid4

import pytest
from alembic import command
from forge.application.services.epic_brief import EpicBriefService
from forge.application.services.epic_items import EpicItemsService
from forge.persistence.unit_of_work import PostgresUnitOfWork
from sqlalchemy import text


@pytest.fixture
def migrated_database_url(test_database_url, alembic_config_factory):
    command.upgrade(alembic_config_factory(test_database_url), "head")
    return test_database_url


@pytest.mark.integration
async def test_http_graph_history_and_brief_binding_survive_reopen_without_run_mutation(
    task10_client, task10_route_context, route_headers, session_factory, persisted_run
) -> None:
    app = task10_route_context.app
    app.state.epic_brief_service = EpicBriefService(lambda: PostgresUnitOfWork(session_factory))
    app.state.epic_items_service = EpicItemsService(lambda: PostgresUnitOfWork(session_factory))

    async def retained_run_bytes():
        async with session_factory() as session:
            return await session.scalar(
                text(
                    "SELECT json_build_object("
                    "'task', (SELECT row_to_json(t) FROM tasks t WHERE t.id = :task_id), "
                    "'run', (SELECT row_to_json(r) FROM runs r WHERE r.id = :run_id), "
                    "'approvals', (SELECT coalesce(json_agg(a ORDER BY a.id), '[]'::json) "
                    "FROM approvals a WHERE a.run_id = :run_id))::text"
                ),
                {"task_id": persisted_run.task_id, "run_id": persisted_run.id},
            )

    async def post(path, key, body, expected=201):
        response = await task10_client.post(
            path, headers={**route_headers, "Idempotency-Key": key}, json=body
        )
        assert response.status_code == expected, response.text
        return response.json()

    async def get(path, expected=200):
        response = await task10_client.get(path, headers={"Host": route_headers["Host"]})
        assert response.status_code == expected, response.text
        return response.json()

    before = await retained_run_bytes()
    requirement_id = str(uuid4())
    brief = {
        "problem": "An operator needs a durable manual breakdown",
        "outcomes": ["A graph can be saved and reopened"],
        "requirements": [
            {
                "requirement_id": requirement_id,
                "text": "Retain the work-item graph",
                "acceptance_criteria": ["Reopening preserves the selected graph"],
            }
        ],
    }
    epic = await post(
        "/api/epics",
        "create",
        {"project_id": str(persisted_run.project_id), "title": "Manual graph", "draft": brief},
    )
    path = f"/api/epics/{epic['epic_id']}"
    saved_brief = await post(
        f"{path}/brief-revisions", "save-brief", {"expected_epic_version": 1, "content": brief}
    )
    await post(
        f"{path}/brief-adoptions",
        "adopt-brief",
        {
            "expected_epic_version": 2,
            "brief_revision_id": saved_brief["brief_revision_id"],
            "brief_digest": saved_brief["content_digest"],
        },
        200,
    )
    first_id, second_id, deferred_id = (str(uuid4()) for _ in range(3))

    def item(item_id, ordinal, title, dependencies, disposition="required"):
        return {
            "item_id": item_id,
            "disposition": disposition,
            "ordinal": ordinal,
            "title": title,
            "outcome": f"{title} is observable",
            "acceptance_criteria": [f"{title} can be checked"],
            "source_requirement_ids": [requirement_id],
            "dependency_item_ids": dependencies,
        }

    graph_body = {
        "expected_epic_version": 3,
        "brief_revision_id": saved_brief["brief_revision_id"],
        "brief_digest": saved_brief["content_digest"],
        "items": [
            item(deferred_id, 3, "Future preview", [second_id], "deferred"),
            item(second_id, 2, "Read the saved graph", [first_id]),
            item(first_id, 1, "Save the graph", []),
        ],
    }
    graph = await post(f"{path}/graph-revisions", "save-graph", graph_body)
    assert graph["epic_version"] == 4
    assert [entry["item_id"] for entry in graph["items"]] == [first_id, second_id, deferred_id]
    adoption = {
        "expected_epic_version": 4,
        "graph_revision_id": graph["graph_revision_id"],
        "graph_digest": graph["graph_digest"],
    }
    adopted = await post(f"{path}/graph-adoptions", "adopt-graph", adoption, 200)
    assert adopted["version"] == 5
    assert adopted["accepted_graph_revision_id"] == graph["graph_revision_id"]
    frozen = await get(f"{path}/accepted-graph")
    assert [entry["status"] for entry in frozen["readiness"]] == ["ready", "blocked", "deferred"]
    assert all(entry["reason"] for entry in frozen["readiness"])

    revised_items = json.loads(json.dumps(graph_body["items"]))
    revised_items[-1]["acceptance_criteria"].append("Saving also preserves a new requirement")
    next_body = {**graph_body, "expected_epic_version": 5, "items": revised_items}
    next_graph = await post(f"{path}/graph-revisions", "save-next-graph", next_body)
    assert next_graph["graph_digest"] != graph["graph_digest"]
    assert next_graph["items"][0]["item_digest"] != graph["items"][0]["item_digest"]
    assert await get(f"{path}/accepted-graph") == frozen
    assert await get(f"{path}/graph-revisions/{graph['graph_revision_id']}") == graph

    app.state.epic_items_service = EpicItemsService(lambda: PostgresUnitOfWork(session_factory))
    assert await get(f"{path}/accepted-graph") == frozen
    assert await post(f"{path}/graph-revisions", "save-graph", graph_body) == graph
    assert await post(f"{path}/graph-adoptions", "adopt-graph", adoption, 200) == adopted
    await post(f"{path}/graph-revisions", "stale-save", graph_body, 409)
    invalid = {**next_body, "expected_epic_version": 6}
    invalid["items"] = [{**revised_items[-1], "source_requirement_ids": [str(uuid4())]}]
    await post(f"{path}/graph-revisions", "invalid-source", invalid, 422)
    invalid["items"] = [{**revised_items[-1], "status": "succeeded"}]
    await post(f"{path}/graph-revisions", "untrusted-success", invalid, 422)
    current = await get(path)
    assert current["version"] == 6
    assert current["accepted_graph_revision_id"] == graph["graph_revision_id"]

    await post(
        f"{path}/graph-adoptions",
        "adopt-next-graph",
        {
            "expected_epic_version": 6,
            "graph_revision_id": next_graph["graph_revision_id"],
            "graph_digest": next_graph["graph_digest"],
        },
        200,
    )
    changed_brief = {**brief, "problem": "The confirmed requirements have changed"}
    next_brief = await post(
        f"{path}/brief-revisions",
        "save-next-brief",
        {"expected_epic_version": 7, "content": changed_brief},
    )
    changed = await post(
        f"{path}/brief-adoptions",
        "adopt-next-brief",
        {
            "expected_epic_version": 8,
            "brief_revision_id": next_brief["brief_revision_id"],
            "brief_digest": next_brief["content_digest"],
        },
        200,
    )
    assert changed["accepted_graph_revision_id"] is None
    assert changed["accepted_graph_digest"] is None
    await get(f"{path}/accepted-graph", 409)
    await post(
        f"{path}/graph-adoptions", "obsolete-adoption", {**adoption, "expected_epic_version": 9}, 409
    )
    assert await get(f"{path}/graph-revisions") == [graph, next_graph]
    assert await get(f"{path}/graph-revisions/{graph['graph_revision_id']}") == graph
    assert await retained_run_bytes() == before
    async with PostgresUnitOfWork(session_factory) as work:
        audits = await work.audit.list_for_subject(
            subject_type="epic", subject_id=UUID(epic["epic_id"])
        )
        assert len(audits) == 9
        assert all("acceptance_criteria" not in json.dumps(event.payload) for event in audits)
