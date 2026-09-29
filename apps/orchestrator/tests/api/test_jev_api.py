import json
from uuid import uuid4

import pytest
from forge.application.services.jev_reporting import JevReportingService
from forge.domain.policy import JevPolicy
from forge.domain.run import RunSnapshot
from forge.persistence.repositories.runs import RunNotFound
from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
async def test_jev_report_uses_policy_version_bound_to_run():
    run_id, project_id = uuid4(), uuid4()

    class Uow:
        runs = projects = jev = None
        async def __aenter__(self):
            self.runs = self
            self.projects = self
            self.jev = self
            return self
        async def __aexit__(self, *_args): pass
        async def get(self, _run_id):
            return RunSnapshot(id=run_id, project_id=project_id, task_id=uuid4(), policy_version=3)
        async def policy_for_run(self, requested_run, *, policy_version):
            assert requested_run == run_id and policy_version == 3
            return JevPolicy(mode="shadow", model="model-free")
        async def summary(self, requested_run, *, policy):
            assert requested_run == run_id
            assert policy.mode == "shadow" and policy.model == "model-free"
            return {"requested_mode": policy.mode, "effective_mode": "not_yet_observed",
                    "requested_model": policy.model, "calls": 0, "attempts": 0, "cache_hits": 0, "unknown": 0,
                    "actual_input_units": 0, "actual_output_units": 0, "reserved_input_units": 0,
                    "duration_ms": 0, "remaining_requests": 64, "remaining_input_units": 250000,
                    "by_kind": {}, "by_status": {}, "actual_model": None,
                    "review_focus_available": False, "availability": "no_samples"}

    service = JevReportingService(Uow)
    result = await service.report(run_id)
    assert result is not None and result["requested_model"] == "model-free"
    assert result["schema_version"] == 1 and result["actual_model"] is None
    assert json.loads(json.dumps(result))["run_id"] == str(run_id)


@pytest.mark.asyncio
async def test_jev_report_uses_legacy_policy_for_missing_run():
    class Uow:
        runs = projects = jev = None
        async def __aenter__(self): self.runs = self; return self
        async def __aexit__(self, *_args): pass
        async def get(self, run_id): raise RunNotFound(run_id)

    assert await JevReportingService(Uow).report(uuid4()) is None


@pytest.mark.asyncio
async def test_jev_route_requires_operator_session_and_returns_safe_report(task10_route_context, route_headers):
    class Report:
        async def report(self, run_id):
            return {"schema_version": 1, "run_id": run_id, "requested_mode": "on",
                    "effective_mode": "not_yet_observed", "requested_model": "jev-latest", "actual_model": None,
                    "calls": 0, "attempts": 0, "cache_hits": 0, "unknown": 0, "actual_input_units": 0,
                    "actual_output_units": 0, "reserved_input_units": 0, "duration_ms": 0,
                    "remaining_requests": 64, "remaining_input_units": 250000,
                    "by_kind": {}, "by_status": {}, "review_focus_available": False,
                    "availability": "no_samples"}

    app = task10_route_context.app
    app.state.jev_reporting_service = Report()
    run_id = uuid4()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:3000") as client:
        denied = await client.get(f"/api/runs/{run_id}/jev", headers=route_headers)
        client.cookies.set("forge_session", task10_route_context.auth.session_token)
        authorized = await client.get(f"/api/runs/{run_id}/jev", headers=route_headers)
    assert denied.status_code == 401
    assert authorized.status_code == 200
    body = authorized.json()
    assert body["schema_version"] == 1 and body["requested_model"] == "jev-latest"
    assert "prompt" not in body and body["actual_model"] is None


@pytest.mark.asyncio
async def test_jev_report_with_unavailable_policy_returns_safe_unavailable_status(
    task10_client, task10_route_context, route_headers,
):
    class Report:
        async def report(self, run_id):
            raise ValueError("private details from an inconsistent policy binding")

    task10_route_context.app.state.jev_reporting_service = Report()
    response = await task10_client.get(f"/api/runs/{uuid4()}/jev", headers=route_headers)
    assert response.status_code == 503
    assert "private details" not in response.text
