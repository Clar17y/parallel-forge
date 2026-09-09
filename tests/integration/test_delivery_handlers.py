"""Worker-facing delivery handlers preserve review execution authority."""

import pytest
from forge.application.handlers.delivery import ReviewHandler
from forge.application.services.review import ReviewRecoveryRequired
from forge.application.services.review_decision import (
    ReviewDecisionRecoveryRequired,
    ReviewDecisionService,
)
from forge.domain.event import RunEvent
from forge.persistence.unit_of_work import PostgresUnitOfWork
from test_delivery_review import _review_case
from test_worker_planning_e2e import (
    workflow_session_factory as workflow_session_factory,  # noqa: PLC0414
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


async def _handler_case(tmp_path, workflow_session_factory):
    case, command, reviewer, gateway, git, _validation = await _review_case(
        tmp_path, workflow_session_factory
    )
    decision = ReviewDecisionService(case.artifact_store, git_factory=lambda _policy: git)
    return case, command, reviewer, decision, gateway, git


async def test_review_handler_executes_once_then_reaches_pr_gate(
    tmp_path, workflow_session_factory
):
    case, command, reviewer, decision, gateway, _git = await _handler_case(
        tmp_path, workflow_session_factory
    )
    handler = ReviewHandler(reviewer, decision)

    async with PostgresUnitOfWork(workflow_session_factory) as work:
        result = await handler(command, work)

    assert result.state.value == "AWAITING_PR_APPROVAL"
    assert len(gateway.requests) == 1
    assert result.run_id == case.run_id


async def test_review_handler_replays_decision_without_reinvoking_reviewer(
    tmp_path, workflow_session_factory
):
    _case, command, reviewer, decision, gateway, _git = await _handler_case(
        tmp_path, workflow_session_factory
    )
    handler = ReviewHandler(reviewer, decision)

    async with PostgresUnitOfWork(workflow_session_factory) as work:
        first = await handler(command, work)
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        replay = await handler(command, work)

    assert replay == first
    assert len(gateway.requests) == 1


async def test_review_handler_rejects_malformed_decision_event_without_gateway_call(
    tmp_path, workflow_session_factory
):
    case, command, reviewer, decision, gateway, _git = await _handler_case(
        tmp_path, workflow_session_factory
    )
    handler = ReviewHandler(reviewer, decision)
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        await handler(command, work)
    gateway_calls = len(gateway.requests)

    async with PostgresUnitOfWork(workflow_session_factory) as work:
        await work.events.append(
            RunEvent(
                run_id=case.run_id,
                run_version=command.expected_run_version + 1,
                event_type="run.review_decided",
                actor_class="worker",
                payload={"source_command_id": str(command.id), "target": "wrong"},
            )
        )
        await work.commit()

    async with PostgresUnitOfWork(workflow_session_factory) as work:
        with pytest.raises(ReviewDecisionRecoveryRequired):
            await handler(command, work)
    assert len(gateway.requests) == gateway_calls


async def test_review_handler_does_not_reinvoke_unknown_reviewer_result(
    tmp_path, workflow_session_factory
):
    _case, command, reviewer, decision, gateway, _git = await _handler_case(
        tmp_path, workflow_session_factory
    )

    async def unknown(request):
        gateway.requests.append(request)
        raise RuntimeError("unknown outcome")

    gateway.execute = unknown
    handler = ReviewHandler(reviewer, decision)
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        with pytest.raises(RuntimeError, match="unknown outcome"):
            await handler(command, work)
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        with pytest.raises(ReviewRecoveryRequired):
            await handler(command, work)
    assert len(gateway.requests) == 1


async def test_review_handler_replay_rejects_candidate_drift(tmp_path, workflow_session_factory):
    _case, command, reviewer, decision, gateway, git = await _handler_case(
        tmp_path, workflow_session_factory
    )
    handler = ReviewHandler(reviewer, decision)
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        await handler(command, work)
    calls = len(gateway.requests)
    git.head = "c" * 40
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        with pytest.raises(ReviewDecisionRecoveryRequired):
            await handler(command, work)
    assert len(gateway.requests) == calls
