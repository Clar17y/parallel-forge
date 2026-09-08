"""Exact terminal admission recovery for failed deliveries."""

from uuid import UUID, uuid5

import pytest
from forge.application.handlers.run_controls import ResumeRunHandler
from forge.domain.actor import AgentRole
from forge.domain.agent import AgentFinishStatus
from forge.domain.command import CommandStatus
from forge.domain.run import RunState
from forge.observability.usage import UsageRecord
from forge.persistence.unit_of_work import PostgresUnitOfWork
from test_delivery_development import _service_case
from test_failed_delivery_resume import _fail_pause_resume
from test_worker_planning_e2e import (
    workflow_session_factory as workflow_session_factory,  # noqa: PLC0414
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


async def test_exact_settled_failed_admission_resumes_next_attempt(
    tmp_path, workflow_session_factory
):
    case, source, service, gateway, _git = await _service_case(tmp_path, workflow_session_factory)
    commands, failed, _paused, resume = await _fail_pause_resume(workflow_session_factory, source)
    execution_id = uuid5(UUID("6649eb62-7e4a-421f-9861-8be14cefa22b"), str(source.id))
    step_id = uuid5(UUID("5f7bc719-a867-443c-b6a8-936c6663a983"), str(source.id))
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        await work.executions.admit(
            source.run_id,
            step_id,
            execution_id,
            "implement",
            1,
            AgentRole.DEVELOPER,
            "test-v1",
            "google",
            "fixture-model",
        )
        await work.executions.finalize(
            source.run_id,
            step_id,
            execution_id,
            AgentFinishStatus.FAILED,
            UsageRecord(
                provider="google",
                model="fixture-model",
                prompt_version="test-v1",
                pricing_version="fixture-v1",
                estimated_cost_minor=0,
                currency="USD",
            ),
            provider="google",
            model="fixture-model",
            instruction_version="test-v1",
            kind="implement",
            attempt=1,
            role=AgentRole.DEVELOPER,
        )
        await work.commit()
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        await ResumeRunHandler(artifact_store=case.artifact_store)(resume, work)
        assert (await work.runs.get(source.run_id)).state is RunState.IMPLEMENTING
    assert (await commands.get(failed.id)).status is CommandStatus.FAILED
    await commands.complete(resume.id, worker_id="resume")
    fresh = await commands.claim_next(worker_id="developer", lease_seconds=60)
    assert fresh is not None and fresh.payload["semantic_attempt"] == 2
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        await service.execute(fresh, work)
    assert len(gateway.requests) == 1
