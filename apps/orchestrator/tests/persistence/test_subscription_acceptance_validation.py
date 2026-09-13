"""Actual acceptance dispatch supplies the controller candidate and required check."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from forge.application.ports.commands import CommandRecoveryRequired
from forge.application.services.recovery import OperationExecutor
from forge.application.services.validation import ValidationService
from forge.domain.evidence import decode_evidence_manifest
from forge.domain.validation import command_spec_digest
from forge.persistence.models import EvidenceSet, RunCommand, SubscriptionOperationBinding
from forge.persistence.repositories.commands import PostgresCommandRepository
from forge.persistence.repositories.operations import PostgresOperationRepository
from sqlalchemy import select
from test_scheduler_acceptance import (
    _remove_disposable_subscription_rows,  # noqa: F401 - pytest fixture
)
from test_subscription_acceptance_dispatch import dispatch_case

from apps.orchestrator.tests.application.test_controller_check_adapter import _setup_real_adapter


async def validation_case(session_factory, tmp_path, *, acceptance_factory=None):
    factory, proposal, dispatch, _ = await dispatch_case(
        session_factory, tmp_path, plan_scope=("apps",), acceptance_factory=acceptance_factory
    )
    await dispatch.apply(proposal.attempt_id)
    async with factory() as work:
        previous = await work.session.scalar(
            select(RunCommand).where(
                RunCommand.run_id == proposal.decision.run_id,
                RunCommand.command_type == "prepare_worktree",
            )
        )
        previous_id, previous_owner = previous.id, previous.lease_owner
    commands = PostgresCommandRepository(session_factory)
    await commands.complete(previous_id, worker_id=previous_owner)
    command = await commands.claim_next(worker_id="validate", lease_seconds=60)
    assert command is not None and command.command_type == "validate"
    _, _, runner, _, _ = await _setup_real_adapter(
        tmp_path / "artifacts",
        SimpleNamespace(id=proposal.decision.run_id, project_id=proposal.policy.id),
        session_factory,
    )
    if proposal.policy.required_checks:
        spec = proposal.policy.required_checks[0]
        runner.runner.terminal = replace(
            runner.runner.terminal,
            result=replace(runner.runner.terminal.result, command_digest=command_spec_digest(spec)),
        )
    snapshot = await dispatch._snapshot(proposal)

    async def environment(*args):
        return {}

    git = SimpleNamespace(
        inspect_worktree=lambda *args: proposal.worktree,
        head_sha=lambda *args: snapshot.head_sha,
        is_ancestor=lambda *args: True,
        working_tree_snapshot=lambda *args, **kwargs: snapshot,
    )
    service = ValidationService(
        dispatch._store,
        uow_factory=factory,
        operation_executor=OperationExecutor(PostgresOperationRepository(session_factory)),
        git_factory=lambda _: git,
        runner_factory=runner,
        environment_resolver=environment,
    )
    return factory, proposal, dispatch, command, service, runner, git


@pytest.mark.integration
async def test_dispatched_acceptance_validates_its_persisted_candidate(session_factory, tmp_path):
    factory, proposal, dispatch, command, service, runner, _ = await validation_case(
        session_factory, tmp_path
    )
    async with factory() as work:
        evidence = await service.execute(command, work)
    manifest = decode_evidence_manifest(await dispatch._store.open_bytes(evidence.manifest_digest))
    assert manifest.schema_version == 2
    assert manifest.candidate_tree_digest == proposal.review.candidate.tree_digest
    assert len(manifest.members) == 1 and manifest.members[0].check_name == "unit"
    assert manifest.members[0].controller_receipt_digest is not None
    async with factory() as work:
        assert await service.execute(command, work) == evidence
    assert runner.calls == runner.runner.calls == 1


@pytest.mark.integration
async def test_validation_refuses_receipt_authority_changed_during_check(
    session_factory, tmp_path, monkeypatch
):
    factory, proposal, _, command, service, runner, _ = await validation_case(
        session_factory, tmp_path
    )
    execute = runner.runner.run_terminal

    async def change_receipt(request):
        terminal = await execute(request)
        async with factory() as work:
            binding = await work.session.scalar(
                select(SubscriptionOperationBinding).where(
                    SubscriptionOperationBinding.attempt_id == proposal.attempt_id
                )
            )
            binding.receipt_payload = {**binding.receipt_payload, "accepted": False}
            await work.commit()
        return terminal

    monkeypatch.setattr(runner.runner, "run_terminal", change_receipt)
    async with factory() as work:
        with pytest.raises(CommandRecoveryRequired, match="acceptance authority differs"):
            await service.execute(command, work)
    assert runner.calls == runner.runner.calls == 1
    async with factory() as work:
        assert (
            await work.session.scalar(
                select(EvidenceSet.id).where(EvidenceSet.run_id == proposal.decision.run_id)
            )
            is None
        )
        run = await work.runs.get(proposal.decision.run_id)
        assert run.state.value == "VALIDATING" and run.pending_gate is None
