"""Resume allocates a fresh stage identity while preserving its approved input."""

import pytest
from forge.application.services.resume_continuation import enqueue_resumed_stage
from forge.application.services.resume_reconciliation import ResumeReconciler
from forge.domain.command import CommandStatus
from forge.persistence.unit_of_work import PostgresUnitOfWork

from tests.integration.test_resume_settlement import _stopped_delivery

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


@pytest.mark.parametrize("phase", ["initial_plan", "implement", "restarted_plan"])
async def test_resume_allocates_new_stage_without_changing_scope_or_counters(
    persisted_run, session_factory, phase
):
    _commands, source, resume = await _stopped_delivery(session_factory, persisted_run, phase)
    async with PostgresUnitOfWork(session_factory) as work:
        sources = await ResumeReconciler().reconcile(work, resume)
        paused = await work.runs.get_for_update(resume.run_id)
        queued = await enqueue_resumed_stage(work, resume, paused, sources)
        assert queued.id != source.id
        assert queued.command_type == source.command_type
        assert queued.status is CommandStatus.PENDING
        assert queued.actor_id == source.actor_id
        assert queued.expected_run_version == paused.version + 1
        assert queued.payload["semantic_attempt"] == source.payload.get("semantic_attempt", 1) + 1
        assert queued.payload["resume_command_id"] == str(resume.id)
        assert queued.payload["source_command_id"] == str(source.id)
        assert await work.runs.get(resume.run_id) == paused
        assert await enqueue_resumed_stage(work, resume, paused, sources) == queued
        await work.commit()
