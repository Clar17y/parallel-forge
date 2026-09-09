"""Recovery acknowledges a persisted observation before resuming its successor."""

import pytest
from forge.application.ports.commands import CommandRecoveryRequired
from forge.application.services.release_monitor import ReleaseMonitor
from forge.domain.command import CommandStatus
from forge.domain.github import CheckSnapshot, MergeProtection
from forge.domain.run import RunState
from forge.persistence.unit_of_work import PostgresUnitOfWork
from test_pr_monitoring import published
from test_release_monitor_resume import resumed_poll
from test_worker_planning_e2e import (
    workflow_session_factory as workflow_session_factory,  # noqa: PLC0414
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


@pytest.mark.parametrize("outcome", ["pending", "ready", "artifact", "renewed"])
async def test_resume_acknowledges_committed_observation_before_continuing(
    tmp_path, workflow_session_factory, monkeypatch, outcome
):
    factory = workflow_session_factory
    case, git, read, writes, validator, source, policy, _ = await published(tmp_path, factory)
    read.merge_protections[policy.github_repository.casefold(), "main"] = MergeProtection(
        True, False, False, "classic", required_check_names=("ci",)
    )
    monitor = ReleaseMonitor(case.artifact_store, validator, read, writes)
    if outcome == "ready":
        read.checks[policy.github_repository.casefold(), git.head] = (
            CheckSnapshot("ci", "completed", "success", head_sha=git.head),
        )
    async with PostgresUnitOfWork(factory) as work:
        await monitor(source, work)
    if outcome == "artifact":

        async def unverifiable(digest):
            return False

        monkeypatch.setattr(case.artifact_store, "verify", unverifiable)
        with pytest.raises(CommandRecoveryRequired, match="replay evidence"):
            await resumed_poll(case, source, factory)
        async with PostgresUnitOfWork(factory) as work:
            assert (await work.commands.get(source.id)).status is CommandStatus.LEASED
            assert (await work.runs.get(case.run_id)).state is RunState.PAUSED
        return
    if outcome == "renewed":
        assert await resumed_poll(case, source, factory, mode="renewed") is None
        return
    continued, _ = await resumed_poll(
        case, source, factory, mode="ready_observed" if outcome == "ready" else "expired"
    )
    async with PostgresUnitOfWork(factory) as work:
        assert (await work.commands.get(source.id)).status is CommandStatus.COMPLETED
        if outcome == "ready":
            assert (await work.runs.get(case.run_id)).state is RunState.AWAITING_MERGE_APPROVAL
            return
    assert continued.payload["poll"] == 2
    async with PostgresUnitOfWork(factory) as work:
        await monitor(continued, work)
        observations = [
            e
            for e in await work.events.list_after(case.run_id, 0)
            if e.event_type == "run.pr_observed"
        ]
        assert [e.payload["poll"] for e in observations] == [1, 2]
