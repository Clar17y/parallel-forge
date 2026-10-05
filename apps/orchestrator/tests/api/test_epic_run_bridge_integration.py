"""Normal app registration, migrated persistence and owner launch behavior."""

from __future__ import annotations

import asyncio
import json
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from alembic import command
from forge.api.app import create_app
from forge.application.ports.projects import RepositoryInspection
from forge.application.services.runs import RunService
from forge.persistence.database import create_engine, create_session_factory
from forge.persistence.unit_of_work import PostgresUnitOfWork
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError


@pytest.fixture
def migrated_database_url(test_database_url, alembic_config_factory):
    command.upgrade(alembic_config_factory(test_database_url), "head")
    return test_database_url


@pytest_asyncio.fixture
async def session_factory(migrated_database_url):
    engine = create_engine(migrated_database_url)
    try:
        yield create_session_factory(engine)
    finally:
        await engine.dispose()


class Inspector:
    def inspect(self, **values):
        return RepositoryInspection(
            canonical_path=values["repository_path"],
            github_repository=values["github_repository"],
            default_branch=values["default_branch"],
            base_ref=f"refs/heads/{values['default_branch']}",
            base_sha="c" * 40,
        )


@pytest.mark.integration
async def test_normal_app_launches_epic_item_and_exposes_owner_override(
    task10_route_context,
    route_headers,
    session_factory,
    persisted_run,
    migrated_database_url,
    alembic_config_factory,
):
    settings = task10_route_context.app.state.settings

    def work_factory():
        return PostgresUnitOfWork(session_factory)

    run_service = RunService(work_factory, repository_inspector=Inspector(), settings=settings)
    app = create_app(
        settings,
        session_factory=session_factory,
        auth_service=task10_route_context.auth,
        run_service=run_service,
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url=settings.web_origin
    ) as client:
        client.cookies.set("forge_session", task10_route_context.auth.session_token)

        async def post(path, key, body, expected=201):
            response = await client.post(
                path, headers={**route_headers, "Idempotency-Key": key}, json=body
            )
            assert response.status_code == expected, response.text
            return response.json()

        async def get(path):
            response = await client.get(path, headers={"Host": route_headers["Host"]})
            assert response.status_code == 200, response.text
            return response.json()

        async def retained_bytes():
            async with session_factory() as session:
                return await session.scalar(
                    text("SELECT row_to_json(r)::text FROM runs r WHERE id = :id"),
                    {"id": persisted_run.id},
                )

        retained = await retained_bytes()
        requirement_id = str(uuid4())
        brief = {
            "problem": "Deliver a small saved epic increment",
            "outcomes": ["The owner can start work directly"],
            "requirements": [
                {
                    "requirement_id": requirement_id,
                    "text": "Keep the owner's actual acceptance criteria",
                    "acceptance_criteria": ["Stored source and override evidence remain readable"],
                }
            ],
        }
        epic = await post(
            "/api/epics",
            "create",
            {
                "project_id": str(persisted_run.project_id),
                "title": "Owner launch",
                "draft": brief,
            },
        )
        path = f"/api/epics/{epic['epic_id']}"
        saved = await post(
            f"{path}/brief-revisions",
            "save-brief",
            {
                "expected_epic_version": 1,
                "content": brief,
            },
        )
        await post(
            f"{path}/brief-adoptions",
            "adopt-brief",
            {
                "expected_epic_version": 2,
                "brief_revision_id": saved["brief_revision_id"],
                "brief_digest": saved["content_digest"],
            },
            200,
        )
        first_id, deferred_id = str(uuid4()), str(uuid4())

        def item(item_id, ordinal, disposition, dependencies):
            return {
                "item_id": item_id,
                "ordinal": ordinal,
                "disposition": disposition,
                "title": f"Increment {ordinal}",
                "outcome": "Observable delivery",
                "acceptance_criteria": ["Keep this whole criterion: " + "x" * 600],
                "source_requirement_ids": [requirement_id],
                "dependency_item_ids": dependencies,
            }

        graph = await post(
            f"{path}/graph-revisions",
            "save-graph",
            {
                "expected_epic_version": 3,
                "brief_revision_id": saved["brief_revision_id"],
                "brief_digest": saved["content_digest"],
                "items": [
                    item(first_id, 0, "required", []),
                    item(deferred_id, 1, "deferred", [first_id]),
                ],
            },
        )
        await post(
            f"{path}/graph-adoptions",
            "adopt-graph",
            {
                "expected_epic_version": 4,
                "graph_revision_id": graph["graph_revision_id"],
                "graph_digest": graph["graph_digest"],
            },
            200,
        )
        launch = {
            "expected_epic_version": (await get(path))["version"],
            "brief_revision_id": saved["brief_revision_id"],
            "brief_digest": saved["content_digest"],
            "graph_revision_id": graph["graph_revision_id"],
            "graph_digest": graph["graph_digest"],
            "item_id": first_id,
        }
        first = await post(f"{path}/work-item-runs", "launch", launch)
        assert not first["owner_override"]
        assert first["blocker_codes"] == []
        assert await post(f"{path}/work-item-runs", "launch", launch) == first
        assert await get(f"{path}/work-item-runs/{first['attempt_id']}") == first

        warning_body = {
            **launch,
            "expected_epic_version": (await get(path))["version"],
            "item_id": deferred_id,
        }
        blocked = await post(f"{path}/work-item-runs", "blocked", warning_body, 409)
        assert "owner" in json.dumps(blocked).lower()
        assert "item_deferred" in json.dumps(blocked)
        override_body = {**warning_body, "owner_override": True}
        overridden = await post(f"{path}/work-item-runs", "override", override_body)
        assert overridden["owner_override"]
        assert set(overridden["blocker_codes"]) >= {
            "item_deferred",
            "active_child",
            "predecessor_unverified",
        }
        assert all(value["status"] != "verified" for value in overridden["dependency_evidence"])
        assert await post(f"{path}/work-item-runs", "override", override_body) == overridden
        assert len(await get(f"{path}/work-item-runs")) == 2
        async with PostgresUnitOfWork(session_factory) as work:
            task = await work.tasks.get(UUID(first["task_id"]))
            run = await work.runs.get(UUID(first["run_id"]))
            source = json.loads(task.body)
            assert source["base_sha"] == run.base_sha == first["base_sha"]
            assert source["execution_id"] == first["execution_id"]
            assert "x" * 600 in task.body
            await work.commit()
        assert await retained_bytes() == retained
        async with session_factory() as session:
            with pytest.raises(DBAPIError):
                await session.execute(
                    text("UPDATE epic_item_attempts SET owner_override = true WHERE id = :id"),
                    {"id": UUID(first["attempt_id"])},
                )
            await session.rollback()
            with pytest.raises(DBAPIError):
                await session.execute(
                    text("UPDATE epic_executions SET brief_digest = :digest WHERE id = :id"),
                    {"id": UUID(first["execution_id"]), "digest": "e" * 64},
                )
            await session.rollback()
        config = alembic_config_factory(migrated_database_url)
        with pytest.raises(DBAPIError, match="cannot downgrade retained epic execution data"):
            await asyncio.to_thread(command.downgrade, config, "20261004_0033")
        async with session_factory() as session:
            assert (
                await session.scalar(text("SELECT version_num FROM alembic_version"))
                == "20261004_0034"
            )
        assert await post(f"{path}/work-item-runs", "launch", launch) == first
        assert await retained_bytes() == retained
