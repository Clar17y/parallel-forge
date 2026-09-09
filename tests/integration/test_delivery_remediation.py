"""Closed authority checks for automatic Developer remediation commands."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from forge.application.services.delivery import DeliveryService
from forge.application.services.development import DevelopmentRecoveryRequired, DevelopmentService
from forge.application.services.recovery import OperationExecutor
from forge.application.services.review import ReviewService
from forge.application.services.review_decision import ReviewDecisionService
from forge.application.services.validation import ValidationService
from forge.domain.agent import DeveloperOutput, ReviewDecision, ReviewOutput
from forge.domain.command import CommandEnvelope, CommandStatus
from forge.domain.review import FindingSeverity, ReviewFinding
from forge.domain.run import RunState
from forge.persistence.repositories.commands import PostgresCommandRepository
from forge.persistence.repositories.operations import PostgresOperationRepository
from forge.persistence.unit_of_work import PostgresUnitOfWork
from test_delivery_development import _service_case
from test_delivery_review import _Gateway as ReviewGateway
from test_delivery_validation import _CheckingRunner
from test_worker_planning_e2e import (
    workflow_session_factory as workflow_session_factory,  # noqa: PLC0414
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


def _command(payload: dict[str, object]) -> CommandEnvelope:
    run_id = uuid4()
    return CommandEnvelope(
        id=uuid4(),
        run_id=run_id,
        command_type="remediate",
        idempotency_key=f"{run_id}:remediate:2",
        payload=payload,
        status=CommandStatus.LEASED,
        expected_run_version=4,
        actor_id=uuid4(),
        payload_schema_version=1,
        attempt=1,
        available_at=datetime.now(UTC),
        lease_owner="worker",
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
    )


async def test_automatic_remediation_requires_closed_evidence_authority():
    evidence_id = uuid4()
    command = _command(
        {
            "semantic_attempt": 2,
            "validation_evidence_set_id": str(evidence_id),
            "automatic": True,
        }
    )
    assert DevelopmentService._validate(command) == (2, evidence_id, None)

    with pytest.raises(DevelopmentRecoveryRequired):
        DevelopmentService._validate(
            _command({"semantic_attempt": 2, "validation_evidence_set_id": str(evidence_id)})
        )
    with pytest.raises(DevelopmentRecoveryRequired):
        DevelopmentService._validate(
            _command(
                {
                    "semantic_attempt": True,
                    "validation_evidence_set_id": str(evidence_id),
                    "automatic": True,
                }
            )
        )


@pytest.mark.parametrize("failure_source", ["checks", "review"])
@pytest.mark.parametrize("tamper", [None, "head", "count", "settled_payload", "resume"])
async def test_failed_validation_runs_fresh_developer_remediation_and_revalidates(
    tmp_path, workflow_session_factory, failure_source, tamper
):
    case, implement, development, gateway, git = await _service_case(
        tmp_path, workflow_session_factory
    )
    commands = PostgresCommandRepository(workflow_session_factory)
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        await development.execute(implement, work)
    await commands.complete(implement.id, worker_id="developer")
    validate = await commands.claim_next(worker_id="controller", lease_seconds=60)
    runner = _CheckingRunner(workflow_session_factory, case.run_id, case.artifact_store)
    original = runner.run_terminal

    async def failed(request):
        terminal = await original(request)
        return replace(terminal, result=replace(terminal.result, exit_code=1))

    if failure_source == "checks":
        runner.run_terminal = failed
    validation = ValidationService(
        case.artifact_store,
        uow_factory=lambda: PostgresUnitOfWork(workflow_session_factory),
        operation_executor=OperationExecutor(PostgresOperationRepository(workflow_session_factory)),
        git_factory=lambda _policy: git,
        runner_factory=runner,
        environment_resolver=lambda *_args: _environment(),
    )
    delivery = DeliveryService(
        case.artifact_store, validation=validation, git_factory=lambda _policy: git
    )
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        decision = await delivery.validate(validate, work)
    await commands.complete(validate.id, worker_id="controller")
    reviewer = ReviewGateway(workflow_session_factory)
    review_service = ReviewService(
        reviewer,
        case.artifact_store,
        development._prompts,
        development._approved,
        development._git_factory,
        development._reader_factory,
    )
    review_decisions = ReviewDecisionService(
        case.artifact_store, git_factory=development._git_factory
    )
    prior_review = None
    if failure_source == "review":
        review_command = await commands.claim_next(worker_id="reviewer", lease_seconds=60)
        original_review = reviewer.execute

        async def blocking(request):
            result = await original_review(request)
            return result.model_copy(
                update={
                    "output": ReviewOutput(
                        decision=ReviewDecision.REQUEST_CHANGES,
                        findings=(
                            ReviewFinding(
                                finding_id="R1",
                                severity=FindingSeverity.MAJOR,
                                path="README.md",
                                start_line=1,
                                summary="Missing behavior",
                                evidence="Observed diff",
                            ),
                        ),
                        tested_claims=("checks",),
                        missing_evidence=(),
                        summary="Repair needed",
                    )
                }
            )

        reviewer.execute = blocking
        async with PostgresUnitOfWork(workflow_session_factory) as work:
            prior_review = await review_service.execute(review_command, work)
            decision = await review_decisions.decide(review_command, work)
        await commands.complete(review_command.id, worker_id="reviewer")
        reviewer.execute = original_review
    assert decision.state is RunState.REMEDIATING
    remediate = await commands.claim_next(worker_id="developer", lease_seconds=60)
    assert remediate is not None and remediate.command_type == "remediate"
    if tamper in {"head", "count"}:
        if tamper == "head":
            git.head = "d" * 40
        else:
            from forge.persistence.models import Run

            async with workflow_session_factory() as session, session.begin():
                (await session.get(Run, case.run_id)).local_remediation_count = 2
        async with PostgresUnitOfWork(workflow_session_factory) as work:
            with pytest.raises(DevelopmentRecoveryRequired):
                await development.execute(remediate, work)
        assert len(gateway.requests) == 1
        return
    original_gateway = gateway.execute

    async def remediated(request):
        result = await original_gateway(request)
        output = result.output
        assert isinstance(output, DeveloperOutput)
        git.head = "c" * 40
        return result.model_copy(
            update={
                "output": output.model_copy(
                    update={"summary": "remediated", "local_commit_sha": git.head}
                )
            }
        )

    if tamper == "resume":
        from forge.application.ports.commands import CommandSuspended

        from tests.integration.test_resumed_delivery import _pause_stage, _resume_stage

        async def stopped_before_change(request):
            result = await original_gateway(request)
            await _pause_stage(workflow_session_factory, remediate)
            return result

        gateway.execute = stopped_before_change
        async with PostgresUnitOfWork(workflow_session_factory) as work:
            with pytest.raises(CommandSuspended):
                await development.execute(remediate, work)
        remediate = await _resume_stage(workflow_session_factory, remediate)
    gateway.execute = remediated
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        outcome = await development.execute(remediate, work)
    if tamper == "settled_payload":
        from forge.persistence.models import RunCommand

        changed = dict(remediate.payload, validation_evidence_set_id=str(uuid4()))
        async with workflow_session_factory() as session, session.begin():
            (await session.get(RunCommand, remediate.id)).payload = changed
        async with PostgresUnitOfWork(workflow_session_factory) as work:
            with pytest.raises(DevelopmentRecoveryRequired, match="replay evidence"):
                await development.execute(replace(remediate, payload=changed), work)
        assert len(gateway.requests) == (3 if tamper == "resume" else 2)
        return
    assert len(gateway.requests) == (3 if tamper == "resume" else 2)
    assert gateway.requests[-1].context.check_evidence
    assert [f.finding_id for f in gateway.requests[-1].context.remediation_findings] == (
        ["R1"] if prior_review else []
    )
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        assert await development.execute(remediate, work) == replace(outcome, changed=False)
        assert (await work.runs.get(case.run_id)).local_remediation_count == 1
    assert len(gateway.requests) == (3 if tamper == "resume" else 2)
    await commands.complete(remediate.id, worker_id=remediate.lease_owner)
    queued = await commands.get_by_idempotency_key(f"{case.run_id}:validate:2")
    expected = {"semantic_attempt": 2}
    if prior_review:
        expected["prior_review_evidence_set_id"] = str(prior_review.evidence_set_id)
    assert queued is not None and queued.payload == expected
    runner.run_terminal = original
    revalidate = await commands.claim_next(worker_id="controller", lease_seconds=60)
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        assert (await delivery.validate(revalidate, work)).state is RunState.REVIEWING
    await commands.complete(revalidate.id, worker_id="controller")
    fresh_review_command = await commands.claim_next(worker_id="reviewer", lease_seconds=60)
    async with PostgresUnitOfWork(workflow_session_factory) as work:
        fresh_review = await review_service.execute(fresh_review_command, work)
        final = await review_decisions.decide(fresh_review_command, work)
    assert fresh_review.head_sha == "c" * 40
    assert final.state is RunState.AWAITING_PR_APPROVAL


async def _environment():
    return {}
