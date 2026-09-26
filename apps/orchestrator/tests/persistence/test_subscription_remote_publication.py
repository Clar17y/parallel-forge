"""A repaired remote candidate returns through fresh acceptance to the existing PR."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from forge.application.services.recovery import OperationExecutor
from forge.application.services.subscription_publication import SubscriptionPublicationController
from forge.application.services.subscription_remote_remediation import (
    SubscriptionRemoteRemediationController,
)
from forge.application.services.validation import ValidationService
from forge.domain.approval import decode_pr_approval_evidence
from forge.domain.evidence import decode_evidence_manifest
from forge.domain.run import RunState
from forge.domain.validation import command_spec_digest
from forge.persistence.repositories.operations import PostgresOperationRepository
from test_scheduler_acceptance import (
    _remove_disposable_subscription_rows,  # noqa: F401 - pytest fixture
)
from test_subscription_remote_remediation import remote_failure_case
from test_subscription_validation_repaired_candidate import accept_reopened_candidate

from apps.orchestrator.tests.application.test_controller_check_adapter import _setup_real_adapter


async def remote_acceptance_case(
    session_factory, tmp_path, *, resume_before_repair=False, fail_after_repair=False
):
    (
        factory,
        original,
        dispatch,
        validator,
        commands,
        writes,
        command,
        first,
        reads,
    ) = await remote_failure_case(session_factory, tmp_path)
    git = validator._git_factory(original.policy)
    repairs = SubscriptionRemoteRemediationController(dispatch._store, validator, lambda _: git)
    if resume_before_repair:
        from test_subscription_resume_controls import pause_and_resume

        command = await pause_and_resume(
            factory,
            session_factory,
            command.run_id,
            dispatch._store,
            source=command,
            repairs=repairs,
        )
        assert command is not None and command.command_type == "remediate_remote"
    async with factory() as work:
        await repairs.execute(command, work)
    if fail_after_repair:
        from test_subscription_resume_controls import pause_and_resume

        await commands.fail(
            command.id, worker_id=command.lease_owner, error="delivery completion unavailable"
        )
        assert (
            await pause_and_resume(
                factory, session_factory, command.run_id, dispatch._store, repairs=repairs
            )
            is None
        )
    else:
        await commands.complete(command.id, worker_id=command.lease_owner)
    selection, accepting = await accept_reopened_candidate(
        factory, session_factory, original, dispatch, git
    )
    queued = await commands.claim_next(worker_id="remote-final-validation", lease_seconds=120)
    assert queued is not None and queued.command_type == "validate"
    _, _, runner, _, _ = await _setup_real_adapter(
        tmp_path / "artifacts",
        SimpleNamespace(id=command.run_id, project_id=original.policy.id),
        session_factory,
    )
    runner.runner.terminal = replace(
        runner.runner.terminal,
        result=replace(
            runner.runner.terminal.result,
            command_digest=command_spec_digest(original.policy.required_checks[0]),
        ),
    )

    async def environment(*args):
        return {}

    validation = ValidationService(
        dispatch._store,
        uow_factory=factory,
        operation_executor=OperationExecutor(PostgresOperationRepository(session_factory)),
        git_factory=lambda _: git,
        runner_factory=runner,
        environment_resolver=environment,
    )
    controller = SubscriptionPublicationController(
        dispatch._store,
        validation=validation,
        git_factory=lambda _: git,
        remote_repairs=repairs,
    )
    return SimpleNamespace(
        factory=factory,
        original=original,
        dispatch=dispatch,
        validator=validator,
        commands=commands,
        writes=writes,
        repairs=repairs,
        repair_command=command,
        first=first,
        reads=reads,
        selection=selection,
        accepting=accepting,
        validation_command=queued,
        controller=controller,
        runner=runner,
        git=git,
    )


@pytest.mark.integration
async def test_remote_repair_requires_fresh_acceptance_before_return_to_original_pr(
    session_factory, tmp_path
):
    case = await remote_acceptance_case(session_factory, tmp_path)
    async with case.factory() as work:
        outcome = await case.controller.validate(case.validation_command, work)
    assert outcome.state is RunState.MONITORING_PR
    assert outcome.pr_evidence_digest != case.first.pr_evidence_digest
    evidence = decode_pr_approval_evidence(
        await case.dispatch._store.open_bytes(outcome.pr_evidence_digest)
    )
    accepted = decode_evidence_manifest(
        await case.dispatch._store.open_bytes(evidence.acceptance_digest)
    )
    assert accepted.producer_attempt_id == case.accepting.attempt.attempt_id
    assert accepted.selection_attempt_id == case.selection.attempt.attempt_id
    async with case.factory() as work:
        assert await case.controller.validate(case.validation_command, work) == outcome
        run = await work.runs.get(outcome.run_id)
        assert run.pending_gate is None
        assert run.remote_remediation_count == 1 and run.local_remediation_count == 0
        push = await work.commands.get_by_idempotency_key(
            f"{run.id}:push-reviewed:{outcome.version}"
        )
        assert push is not None and push.command_type == "push_reviewed_pr"
        assert push.payload["candidate_evidence_digest"] == outcome.pr_evidence_digest
        record = await work.releases.get_for_run(run.id)
        original = await work.operations.get(record.publication_intent_id)
        assert push.payload["approval_id"] == original.request_payload["approval_id"]
        assert push.payload["previous_head_sha"] == record.pull_request.head_sha
        assert not case.writes.pull_requests[case.original.policy.github_repository, 1].merged
    assert case.runner.calls == case.runner.runner.calls == 1
