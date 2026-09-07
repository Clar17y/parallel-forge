"""Revalidation preserves the prior independent review across a new candidate."""

import pytest
from forge.application.services.delivery import DeliveryService
from forge.application.services.recovery import OperationExecutor
from forge.application.services.validation import ValidationService
from forge.domain.evidence import decode_evidence_manifest
from forge.domain.run import RunState
from forge.persistence.repositories.commands import PostgresCommandRepository
from forge.persistence.repositories.operations import PostgresOperationRepository
from forge.persistence.unit_of_work import PostgresUnitOfWork
from test_delivery_review import _review_case
from test_delivery_validation import _CheckingRunner
from test_worker_planning_e2e import (
    workflow_session_factory as workflow_session_factory,  # noqa: PLC0414
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
pytest_plugins = ("apps.orchestrator.tests.persistence.conftest",)


async def test_revalidation_binds_prior_review_and_enqueues_it_for_fresh_reviewer(
    tmp_path, workflow_session_factory
):
    factory = workflow_session_factory
    case, review_command, reviewer, _gateway, git, _decision = await _review_case(tmp_path, factory)
    async with PostgresUnitOfWork(factory) as work:
        prior_review = await reviewer.execute(review_command, work)
    commands = PostgresCommandRepository(factory)
    await commands.complete(review_command.id, worker_id="reviewer")
    # Simulate the Developer boundary for this focused controller test; actual
    # remediation orchestration has separate coverage.
    async with PostgresUnitOfWork(factory) as work:
        run = await work.runs.get(case.run_id)
        run = await work.runs.begin_local_remediation(
            run.id,
            run.version,
            automatic=True,
            limit=3,
            event_type="test.remediation_started",
            event_payload={},
        )
        run = await work.runs.transition(
            run.id, run.version, RunState.VALIDATING, "test.remediation_completed", {}
        )
        await work.commit()
    git.head = "c" * 40
    await commands.enqueue(
        run_id=run.id,
        command_type="validate",
        idempotency_key=f"{run.id}:validate:2",
        payload={
            "semantic_attempt": 2,
            "prior_review_evidence_set_id": str(prior_review.evidence_set_id),
        },
        expected_run_version=run.version,
        actor_id=review_command.actor_id,
    )
    command = await commands.claim_next(worker_id="validator", lease_seconds=60)
    runner = _CheckingRunner(factory, run.id, case.artifact_store)

    async def environment(*_args):
        return {}

    validation = ValidationService(
        case.artifact_store,
        uow_factory=lambda: PostgresUnitOfWork(factory),
        operation_executor=OperationExecutor(PostgresOperationRepository(factory)),
        git_factory=lambda _policy: git,
        runner_factory=runner,
        environment_resolver=environment,
    )
    delivery = DeliveryService(
        case.artifact_store, validation=validation, git_factory=lambda _policy: git
    )
    async with PostgresUnitOfWork(factory) as work:
        decision = await delivery.validate(command, work)
    async with PostgresUnitOfWork(factory) as work:
        evidence = await work.evidence.get_by_id(decision.validation_evidence_set_id, run_id=run.id)
        queued = await work.commands.get_by_idempotency_key(f"{run.id}:review:2")
    manifest = decode_evidence_manifest(
        await case.artifact_store.open_bytes(evidence.manifest_digest)
    )
    assert manifest.head_sha == git.head
    assert manifest.prior_review_evidence_set_id == prior_review.evidence_set_id
    assert evidence.prior_review_evidence_set_id == prior_review.evidence_set_id
    assert queued.payload["prior_review_evidence_set_id"] == str(prior_review.evidence_set_id)
    async with PostgresUnitOfWork(factory) as work:
        assert await delivery.validate(command, work) == decision
    assert runner.calls == ["unit", "lint"]
