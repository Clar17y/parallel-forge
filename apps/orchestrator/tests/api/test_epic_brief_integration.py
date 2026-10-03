"""Real HTTP-to-PostgreSQL brief history and receipt projection contract."""

from __future__ import annotations

import json
from uuid import UUID, uuid4

import pytest
from alembic import command
from forge.application.services.epic_brief import EpicBriefService
from forge.domain.operation import canonical_digest
from forge.persistence.unit_of_work import PostgresUnitOfWork
from sqlalchemy import text


@pytest.fixture
def migrated_database_url(test_database_url, alembic_config_factory):
    # The outer fixture drops only its guarded disposable DB. A production
    # downgrade deliberately refuses to discard newly saved epic records.
    command.upgrade(alembic_config_factory(test_database_url), "head")
    return test_database_url


@pytest.mark.integration
async def test_http_brief_history_selection_and_original_replays_survive_reopen(
    task10_client,
    task10_route_context,
    route_headers,
    session_factory,
    persisted_run,
) -> None:
    task10_route_context.app.state.epic_brief_service = EpicBriefService(
        lambda: PostgresUnitOfWork(session_factory)
    )
    content = {
        "schema_version": 1,
        "problem": "Large ideas need smaller plans",
        "outcomes": ["Saved requirements can be reopened"],
        "scope": ["Project epics"],
        "exclusions": ["Automatic run approval"],
        "requirements": [
            {
                "requirement_id": str(uuid4()),
                "text": "Preserve selected requirements",
                "acceptance_criteria": ["Reopening returns the accepted revision"],
            }
        ],
        "decisions": ["Each delivery item uses an ordinary run"],
        "assumptions": ["The operator reviews the requirements"],
        "open_questions": ["Which items can be developed in parallel?"],
    }

    async def post(path, key, body):
        response = await task10_client.post(
            path, headers={**route_headers, "Idempotency-Key": key}, json=body
        )
        assert response.status_code in (200, 201), response.text
        return response.json()

    async def old_run_bytes():
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

    before = await old_run_bytes()
    create_body = {
        "schema_version": 1,
        "project_id": str(persisted_run.project_id),
        "title": "Break large ideas into plans",
        "draft": content,
    }
    created = await post("/api/epics", "http-create", create_body)
    epic_path = f"/api/epics/{created['epic_id']}"
    assert created["version"] == 1
    assert created["accepted_brief_revision_id"] is None
    assert created["accepted_graph_revision_id"] is None
    assert created["accepted_graph_digest"] is None

    save_body = {"schema_version": 1, "expected_epic_version": 1, "content": content}
    saved = await post(f"{epic_path}/brief-revisions", "http-save", save_body)
    assert saved["epic_version"] == 2 and saved["revision_number"] == 1
    assert saved["content_digest"] == canonical_digest(content)
    assert saved["source_job_id"] is None
    adopt_body = {
        "schema_version": 1,
        "expected_epic_version": 2,
        "brief_revision_id": saved["brief_revision_id"],
        "brief_digest": saved["content_digest"],
    }
    adopted = await post(f"{epic_path}/brief-adoptions", "http-adopt", adopt_body)
    assert adopted["version"] == 3
    changed = {**content, "problem": "The next saved draft is different"}
    edit_body = {
        "schema_version": 1,
        "expected_epic_version": 3,
        "title": "Revised draft title",
        "draft": changed,
    }
    edited = await task10_client.patch(
        epic_path, headers={**route_headers, "Idempotency-Key": "http-edit"}, json=edit_body
    )
    assert edited.status_code == 200
    assert edited.json()["version"] == 4

    # Reconstruct the service to prove projections come from persisted state.
    task10_route_context.app.state.epic_brief_service = EpicBriefService(
        lambda: PostgresUnitOfWork(session_factory)
    )
    accepted = await task10_client.get(
        f"{epic_path}/accepted-brief", headers={"Host": route_headers["Host"]}
    )
    assert accepted.status_code == 200
    assert accepted.json()["problem"] == content["problem"]
    assert accepted.json()["brief_revision_id"] == saved["brief_revision_id"]
    assert accepted.json()["requirements"] == content["requirements"]
    history = await task10_client.get(
        f"{epic_path}/brief-revisions", headers={"Host": route_headers["Host"]}
    )
    assert history.status_code == 200 and history.json() == [saved]

    saved_next = await post(
        f"{epic_path}/brief-revisions",
        "http-save-next",
        {"schema_version": 1, "expected_epic_version": 4, "content": changed},
    )
    adopted_next = await post(
        f"{epic_path}/brief-adoptions",
        "http-adopt-next",
        {
            "schema_version": 1,
            "expected_epic_version": 5,
            "brief_revision_id": saved_next["brief_revision_id"],
            "brief_digest": saved_next["content_digest"],
        },
    )
    assert adopted_next["version"] == 6
    assert await post("/api/epics", "http-create", create_body) == created
    assert await post(f"{epic_path}/brief-revisions", "http-save", save_body) == saved
    assert await post(f"{epic_path}/brief-adoptions", "http-adopt", adopt_body) == adopted
    replayed_edit = await task10_client.patch(
        epic_path, headers={**route_headers, "Idempotency-Key": "http-edit"}, json=edit_body
    )
    assert replayed_edit.json() == edited.json()

    stale = await task10_client.patch(
        epic_path, headers={**route_headers, "Idempotency-Key": "http-stale"}, json=edit_body
    )
    assert stale.status_code == 409
    key_conflict = await task10_client.post(
        "/api/epics",
        headers={**route_headers, "Idempotency-Key": "http-create"},
        json={**create_body, "title": "A different request"},
    )
    assert key_conflict.status_code == 409
    current = await task10_client.get(epic_path, headers={"Host": route_headers["Host"]})
    assert current.status_code == 200 and current.json() == adopted_next
    assert await old_run_bytes() == before
    async with PostgresUnitOfWork(session_factory) as work:
        audit = await work.audit.list_for_subject(
            subject_type="epic", subject_id=UUID(created["epic_id"])
        )
        assert len(audit) == 6
        assert all("problem" not in json.dumps(event.payload) for event in audit)
