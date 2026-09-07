"""Real delivery services hand durable commands to the next local stage."""

from dataclasses import replace

import pytest
from forge.application.services.delivery import DeliveryService
from forge.application.services.recovery import OperationExecutor
from forge.application.services.review import ReviewService
from forge.application.services.validation import ValidationService
from forge.domain.run import RunState
from forge.persistence.repositories.commands import PostgresCommandRepository
from forge.persistence.repositories.operations import PostgresOperationRepository
from forge.persistence.unit_of_work import PostgresUnitOfWork
from test_delivery_development import _service_case
from test_delivery_review import _Gateway as _ReviewGateway
from test_delivery_validation import _CheckingRunner
from test_worker_planning_e2e import (
    workflow_session_factory as workflow_session_factory,  # noqa: PLC0414
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


@pytest.mark.parametrize("checks_pass", (True, False))
async def test_approved_implementation_flows_through_controller_checks(
    tmp_path, workflow_session_factory, checks_pass
):
    case, implement, development, gateway, git = await _service_case(
        tmp_path, workflow_session_factory
    )
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        await development.execute(implement, work)
    commands = PostgresCommandRepository(workflow_session_factory)
    await commands.complete(implement.id, worker_id="developer")
    validate = await commands.claim_next(worker_id="controller", lease_seconds=60)
    assert validate.command_type == "validate"
    runner = _CheckingRunner(workflow_session_factory, case.run_id, case.artifact_store)
    if not checks_pass:
        original = runner.run_terminal

        async def failed(request):
            terminal = await original(request)
            return replace(terminal, result=replace(terminal.result, exit_code=1))

        runner.run_terminal = failed

    async def environment(run, policy, worktree):
        return {}

    validation = ValidationService(
        case.artifact_store,
        uow_factory=lambda: PostgresUnitOfWork(workflow_session_factory),
        operation_executor=OperationExecutor(PostgresOperationRepository(workflow_session_factory)),
        git_factory=lambda policy: git,
        runner_factory=runner,
        environment_resolver=environment,
    )
    delivery = DeliveryService(
        case.artifact_store, validation=validation, git_factory=lambda policy: git
    )
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        decision = await delivery.validate(validate, work)
    await commands.complete(validate.id, worker_id="controller")
    next_command = await commands.claim_next(worker_id="next-stage", lease_seconds=60)
    assert decision.state is (RunState.REVIEWING if checks_pass else RunState.REMEDIATING)
    assert next_command.command_type == ("review" if checks_pass else "remediate")
    assert next_command.payload["validation_evidence_set_id"] == str(
        decision.validation_evidence_set_id
    )
    assert len(gateway.requests) == 1
    assert runner.calls == [spec.name for spec in runner.policy.required_checks]
    if not checks_pass:
        assert next_command.payload["semantic_attempt"] == 2
        async with PostgresUnitOfWork(workflow_session_factory) as work:
            assert (await work.runs.get(case.run_id)).local_remediation_count == 1
    else:
        reviewer = _ReviewGateway(workflow_session_factory)
        review_service = ReviewService(
            reviewer,
            case.artifact_store,
            development._prompts,
            development._approved,
            development._git_factory,
            development._reader_factory,
        )
        async with PostgresUnitOfWork(workflow_session_factory) as work:
            evidence = await review_service.execute(next_command, work)
        assert evidence.validation_evidence_set_id == decision.validation_evidence_set_id
        assert reviewer.requests[0].execution_id != gateway.requests[0].execution_id
        assert reviewer.requests[0].parent_execution_id is None
