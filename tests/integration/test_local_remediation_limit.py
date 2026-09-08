"""Exercise the complete local loop through its approved three-cycle limit."""

from dataclasses import replace

import pytest
from forge.application.services.delivery import DeliveryService
from forge.application.services.recovery import OperationExecutor
from forge.application.services.review import ReviewService
from forge.application.services.review_decision import ReviewDecisionService
from forge.application.services.validation import ValidationService
from forge.domain.agent import ReviewDecision, ReviewOutput
from forge.domain.review import FindingSeverity, ReviewFinding
from forge.domain.run import RunState
from forge.persistence.repositories.commands import PostgresCommandRepository
from forge.persistence.repositories.operations import PostgresOperationRepository
from forge.persistence.unit_of_work import PostgresUnitOfWork
from test_delivery_development import _service_case
from test_delivery_remediation import _environment
from test_delivery_review import _Gateway as ReviewGateway
from test_delivery_validation import _CheckingRunner
from test_worker_planning_e2e import (
    workflow_session_factory as workflow_session_factory,  # noqa: PLC0414
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


@pytest.mark.parametrize("failure_source", ["checks", "review"])
@pytest.mark.parametrize("success_on_third", [True, False])
async def test_local_loop_honors_third_cycle_boundary(
    tmp_path, workflow_session_factory, failure_source, success_on_third
):
    factory = workflow_session_factory
    case, command, development, gateway, git = await _service_case(tmp_path, factory)
    commands = PostgresCommandRepository(factory)
    admitted_counts = []
    original_developer = gateway.execute

    async def develop(request):
        async with PostgresUnitOfWork(factory) as work:
            run = await work.runs.get(case.run_id)
            admitted_counts.append(run.local_remediation_count)
        assert admitted_counts[-1] == len(gateway.requests)
        result = await original_developer(request)
        git.head = f"{len(gateway.requests) + 10:040x}"
        return result.model_copy(
            update={"output": result.output.model_copy(update={"local_commit_sha": git.head})}
        )

    gateway.execute = develop
    runner = _CheckingRunner(factory, case.run_id, case.artifact_store)
    original_check = runner.run_terminal

    def repaired():
        return success_on_third and len(gateway.requests) == 4

    async def check(request):
        terminal = await original_check(request)
        if failure_source == "checks" and not repaired():
            return replace(terminal, result=replace(terminal.result, exit_code=1))
        return terminal

    runner.run_terminal = check
    validation = ValidationService(
        case.artifact_store,
        uow_factory=lambda: PostgresUnitOfWork(factory),
        operation_executor=OperationExecutor(PostgresOperationRepository(factory)),
        git_factory=lambda _policy: git,
        runner_factory=runner,
        environment_resolver=lambda *_args: _environment(),
    )
    delivery = DeliveryService(
        case.artifact_store, validation=validation, git_factory=lambda _policy: git
    )
    reviewer = ReviewGateway(factory)
    original_review = reviewer.execute

    async def review(request):
        result = await original_review(request)
        if failure_source == "review" and not repaired():
            return result.model_copy(
                update={
                    "output": ReviewOutput(
                        decision=ReviewDecision.REQUEST_CHANGES,
                        findings=(
                            ReviewFinding(
                                finding_id="remaining-defect",
                                severity=FindingSeverity.MAJOR,
                                path="README.md",
                                start_line=1,
                                summary="Required behavior remains absent",
                                evidence="Current candidate diff",
                            ),
                        ),
                        tested_claims=("named checks",),
                        missing_evidence=(),
                        summary="Repair required",
                    )
                }
            )
        return result

    reviewer.execute = review
    review_service = ReviewService(
        reviewer,
        case.artifact_store,
        development._prompts,
        development._approved,
        development._git_factory,
        development._reader_factory,
    )
    decision_service = ReviewDecisionService(case.artifact_store, git_factory=lambda _: git)
    for _ in range(12):
        assert command is not None
        async with PostgresUnitOfWork(factory) as work:
            if command.command_type in {"implement", "remediate"}:
                await development.execute(command, work)
            elif command.command_type == "validate":
                await delivery.validate(command, work)
            else:
                assert command.command_type == "review"
                await review_service.execute(command, work)
                await decision_service.decide(command, work)
        await commands.complete(command.id, worker_id=command.lease_owner)
        async with PostgresUnitOfWork(factory) as work:
            run = await work.runs.get(case.run_id)
        if run.state in {RunState.AWAITING_PR_APPROVAL, RunState.AWAITING_HUMAN_INTERVENTION}:
            break
        command = await commands.claim_next(worker_id="cycle", lease_seconds=60)
    expected = (
        RunState.AWAITING_PR_APPROVAL if success_on_third else RunState.AWAITING_HUMAN_INTERVENTION
    )
    assert run.state is expected
    assert run.local_remediation_count == 3
    assert admitted_counts == [0, 1, 2, 3]
    assert runner.calls == [spec.name for spec in runner.policy.required_checks] * 4
    expected_reviews = 4 if failure_source == "review" else int(success_on_third)
    assert len(reviewer.requests) == expected_reviews
    execution_ids = [request.execution_id for request in gateway.requests + reviewer.requests]
    assert len(set(execution_ids)) == len(execution_ids)
    assert await commands.claim_next(worker_id="after-limit", lease_seconds=60) is None
