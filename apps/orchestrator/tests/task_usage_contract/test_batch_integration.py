"""Real application composition for the monitoring batch's durable boundaries."""

from uuid import uuid4

import pytest
from forge.api.app import create_app
from forge.domain.task_usage_contract import (
    CounterKind,
    Observation,
    RouteCohort,
    RouteMember,
    SourceBinding,
    SubjectKey,
    TokenCounts,
    WorkUnitBinding,
)
from forge.persistence.models import AgentExecution
from forge.persistence.unit_of_work import PostgresUnitOfWork
from forge.settings import Settings
from httpx import ASGITransport, AsyncClient


@pytest.mark.integration
@pytest.mark.asyncio
async def test_normal_app_composes_durable_usage_and_authenticated_policy_reporting(
    session_factory, persisted_run
):
    settings = Settings(web_origin="http://127.0.0.1:3000")
    app = create_app(settings, session_factory=session_factory)
    source_id = uuid4()
    async with session_factory() as session, session.begin():
        session.add(
            AgentExecution(
                id=source_id,
                run_id=persisted_run.id,
                role="developer",
                instruction_version="v1",
                provider="test",
                model="test",
                status="RUNNING",
            )
        )
    route = RouteCohort.from_members(
        (
            RouteMember(
                role="developer",
                provider="test",
                model="test",
                ordinal=0,
            ),
        )
    )
    binding = WorkUnitBinding(
        subject=SubjectKey(
            project_id=persisted_run.project_id, kind="run", subject_id=persisted_run.id
        ),
        work_unit_id=uuid4(),
        admitted_phase_id="implement",
        configured_route=route,
        effective_route=route,
        source=SourceBinding(kind="agent_execution", source_id=source_id),
    )
    await app.state.task_usage_observations.bind(binding)
    event = Observation(
        binding=binding,
        event_id="live",
        sequence=1,
        counter_kind=CounterKind.CUMULATIVE,
        tokens=TokenCounts(input_tokens=4, output_tokens=2, cached_input_tokens=1),
    )
    await app.state.task_usage_observations.observe_usage(event)
    await app.state.task_usage_observations.observe_usage(event)
    totals = await app.state.task_usage_totals.totals(binding.subject)
    assert totals.known_total_tokens == 6
    assert totals.source_count == 1
    assert totals.final_source_count == 0

    project_id = persisted_run.project_id
    path = f"/api/projects/{project_id}/monitoring-policy"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url=settings.web_origin
    ) as client:
        assert (await client.get("/api/task-usage-report")).status_code == 401
        token = await app.state.auth_service.issue_bootstrap()
        bootstrap = await client.post(
            "/api/auth/bootstrap", headers={"Origin": settings.web_origin}, json={"token": token}
        )
        assert bootstrap.status_code == 200
        actor = (await client.get("/api/auth/session")).json()["actor_id"]
        headers = {
            "Origin": settings.web_origin,
            "X-CSRF-Token": bootstrap.json()["csrf_token"],
            "Idempotency-Key": "cap",
        }
        assert (
            await client.post(
                path,
                headers={"Origin": settings.web_origin, "Idempotency-Key": "forbidden"},
                json={"expected_revision": 0, "hard_token_cap": 20},
            )
        ).status_code == 403
        created = await client.post(
            path, headers=headers, json={"expected_revision": 0, "hard_token_cap": 20}
        )
        assert created.status_code == 201, created.text
        assert created.json()["hard_token_cap"] == 20
        assert created.json()["owner_action"]["actor_id"] == actor
        reference = await app.state.task_usage_baseline_service.freeze_baseline(binding)
        assert reference.policy_revision == 1
        assert reference.status == "insufficient_history"
        cleared = await client.post(
            path,
            headers={**headers, "Idempotency-Key": "clear"},
            json={"expected_revision": 1, "hard_token_cap": None},
        )
        assert cleared.status_code == 201, cleared.text
        assert cleared.json()["hard_token_cap"] is None
        retained = await app.state.task_usage_baseline_service.freeze_baseline(binding)
        assert retained == reference
        assert retained.snapshot_digest == reference.snapshot_digest
        replay = await client.post(
            path,
            headers={**headers, "Idempotency-Key": "clear"},
            json={"expected_revision": 1, "hard_token_cap": None},
        )
        assert replay.status_code == 201
        assert replay.json()["owner_action"]["replayed"] is True
        stale = await client.post(
            path,
            headers={**headers, "Idempotency-Key": "stale"},
            json={"expected_revision": 1, "hard_token_cap": 40},
        )
        assert stale.status_code == 409
        report = await client.get("/api/task-usage-report", params={"project_id": str(uuid4())})
        assert report.status_code == 200, report.text
        assert report.json()["filters"]["project_ids"] == []
        assert report.json()["overview"]["eligible_count"] == 0

    async with PostgresUnitOfWork(session_factory) as work:
        assert (await work.task_usage_policy.get(project_id, 1)).hard_token_cap == 20
        assert (await work.task_usage_policy.get(project_id)).hard_token_cap is None
