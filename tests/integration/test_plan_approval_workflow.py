"""Transactional planning restart contracts against real PostgreSQL."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest
from forge.application.services.planning import PlanningError, PlanningRecoveryRequired
from forge.domain.run import RunState
from forge.persistence.models import Run
from forge.persistence.repositories.runs import ConcurrencyConflict
from forge.persistence.unit_of_work import PostgresUnitOfWork
from test_planning_failed_usage import _build_case, _FailFinalCommitUnitOfWork
from test_worker_planning_e2e import (
    workflow_session_factory as workflow_session_factory,  # noqa: PLC0414 - export shared pytest fixture
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


async def test_semantic_payload_cannot_bypass_ambiguous_running_execution(
    tmp_path, workflow_session_factory
):
    factory = workflow_session_factory
    case = await _build_case(tmp_path, factory, fail_invalid=False)
    with pytest.raises(PlanningError):
        async with _FailFinalCommitUnitOfWork(factory) as work:
            await case.service.execute(case.command, work)
    async with factory() as session:
        run = await session.get(Run, case.run_id)
        version = run.version
    forged = replace(
        case.command,
        id=uuid4(),
        payload={"semantic_attempt": 2},
        expected_run_version=version,
        idempotency_key="forged-semantic-attempt",
    )
    with pytest.raises(PlanningRecoveryRequired):
        async with PostgresUnitOfWork(factory) as work:
            await case.service.execute(forged, work)
    assert len(case.gateway.requests) == 1


@pytest.mark.parametrize("fail_event", [False, True])
async def test_restart_returns_refreshed_bindings_and_rolls_back_event_failure(
    tmp_path, workflow_session_factory, fail_event
):
    factory = workflow_session_factory
    case = await _build_case(tmp_path, factory, fail_invalid=False)
    async with PostgresUnitOfWork(factory) as work:
        await case.service.execute(case.command, work)
    async with PostgresUnitOfWork(factory) as work:
        before = await work.runs.get(case.run_id)
        if fail_event:

            class FailingEvents:
                async def append(self, event):
                    raise RuntimeError("injected event failure")

            work.runs._events = FailingEvents()
        arguments = {
            "policy_version": before.policy_version,
            "base_ref": before.base_ref,
            "base_sha": "b" * 40,
            "event_type": "run.plan_revision_requested",
            "event_payload": {},
        }
        if fail_event:
            with pytest.raises(RuntimeError, match="injected event failure"):
                await work.runs.restart_planning(case.run_id, before.version, **arguments)
        else:
            returned = await work.runs.restart_planning(case.run_id, before.version, **arguments)
            assert returned.state is RunState.PLANNING
            assert returned.base_sha == "b" * 40
        # A caught repository failure must not become committable partial state.
        await work.commit()
    async with factory() as session:
        run = await session.get(Run, case.run_id)
        assert run.state == ("AWAITING_PLAN_APPROVAL" if fail_event else "PLANNING")
        assert run.version == before.version + (0 if fail_event else 1)
        assert run.base_sha == (before.base_sha if fail_event else "b" * 40)


async def test_concurrent_restarts_commit_only_one_new_version(tmp_path, workflow_session_factory):
    factory = workflow_session_factory
    case = await _build_case(tmp_path, factory, fail_invalid=False)
    async with PostgresUnitOfWork(factory) as work:
        await case.service.execute(case.command, work)
        before = await work.runs.get(case.run_id)

    async def restart():
        async with PostgresUnitOfWork(factory) as work:
            changed = await work.runs.restart_planning(
                case.run_id,
                before.version,
                policy_version=before.policy_version,
                base_ref=before.base_ref,
                base_sha="b" * 40,
                event_type="run.plan_revision_requested",
                event_payload={},
            )
            await work.commit()
            return changed

    results = await asyncio.gather(restart(), restart(), return_exceptions=True)
    assert sum(isinstance(result, ConcurrencyConflict) for result in results) == 1
    async with factory() as session:
        run = await session.get(Run, case.run_id)
        assert run.state == "PLANNING" and run.version == before.version + 1
