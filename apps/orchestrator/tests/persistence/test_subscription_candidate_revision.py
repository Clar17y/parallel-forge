"""Frozen subscription candidates reopen through bounded primary authority."""

import json
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from forge.application.handlers.release import ApprovePrHandler
from forge.application.ports.worktrees import GitSnapshotFile
from forge.application.services.approved_plan import ApprovedPlanLoader
from forge.application.services.candidate_revision import CandidateRevisionService
from forge.application.services.subscription_candidate_revision import (
    SubscriptionCandidateRevisionController,
)
from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
from forge.application.services.subscription_requests import SubscriptionRequestBuilder
from forge.domain.approval import decode_pr_approval_evidence
from forge.domain.event import thaw_payload
from forge.domain.evidence import decode_evidence_manifest
from forge.domain.run import RunState
from forge.persistence.models import Approval, RunCommand
from forge.persistence.models.scheduling import SubscriptionSchedulerRun
from forge.persistence.models.subscription import SubscriptionTask
from forge.persistence.models.subscription_results import (
    SubscriptionAttemptResult,
    SubscriptionRepairDebit,
)
from forge.persistence.repositories.commands import PostgresCommandRepository
from sqlalchemy import select
from test_scheduler_acceptance import (
    _remove_disposable_subscription_rows,  # noqa: F401 - pytest fixture
)
from test_subscription_publication import publication_case, publication_validator
from test_subscription_usage import _reservation
from test_subscription_validation_repaired_candidate import accept_reopened_candidate


async def frozen_publication_case(session_factory, tmp_path):
    case = await publication_case(session_factory, tmp_path)
    factory, _, _, validation_command, controller, _, _ = case
    async with factory() as work:
        outcome = await controller.validate(validation_command, work)
    commands = PostgresCommandRepository(session_factory)
    await commands.complete(validation_command.id, worker_id=validation_command.lease_owner)
    return case, outcome


async def revision_command(
    factory,
    session_factory,
    run_id,
    *,
    approve=False,
    feedback="Clarify the failure message within the approved scope.",
):
    actor, approval_id = uuid4(), uuid4()
    async with factory() as work:
        run = await work.runs.get_for_update(run_id)
        if approve:
            work.session.add(
                Approval(
                    id=approval_id,
                    run_id=run.id,
                    gate="pr",
                    evidence_digest=run.pending_evidence_digest,
                    run_version=run.version,
                    policy_version=run.policy_version,
                    authenticated_actor_id=actor,
                )
            )
        await work.commands.enqueue(
            run_id=run.id,
            command_type="approve_pr" if approve else "request_candidate_changes",
            idempotency_key=f"{run.id}:candidate-decision",
            payload={"approval_id": str(approval_id)} if approve else {"feedback": feedback},
            expected_run_version=run.version,
            actor_id=actor,
        )
        await work.commit()
    command = await PostgresCommandRepository(session_factory).claim_next(
        worker_id="candidate-decision", lease_seconds=120
    )
    assert command is not None
    return command


@pytest.mark.integration
async def test_changed_pr_candidate_requeues_primary_for_fresh_acceptance(
    session_factory, tmp_path, monkeypatch
):
    case, _ = await frozen_publication_case(session_factory, tmp_path)
    factory, proposal, dispatch, _, _, runner, git = case
    command = await revision_command(
        factory, session_factory, proposal.decision.run_id, approve=True
    )
    snapshot = git.working_tree_snapshot

    def drift(*args, **kwargs):
        return replace(
            snapshot(*args, **kwargs),
            files=(
                GitSnapshotFile(
                    path="apps/changed.py", mode="100644", content_digest="e" * 64, byte_count=1
                ),
            ),
            changed_paths=("apps/changed.py",),
        )

    monkeypatch.setattr(git, "working_tree_snapshot", drift)
    handler = ApprovePrHandler(
        publication_validator(dispatch, proposal, git),
        ApprovedPlanLoader(dispatch._store),
        subscription_revisions=SubscriptionCandidateRevisionController(
            dispatch._store, lambda _: git
        ),
    )
    async with factory() as work:
        original = deepcopy(
            (
                await work.session.get(SubscriptionAttemptResult, proposal.attempt_id)
            ).application_payload
        )
    async with factory() as work:
        await handler(command, work)
    async with factory() as work:
        run = await work.runs.get(command.run_id)
        assert run.state is RunState.REMEDIATING and run.pending_gate is None
        assert run.version == command.expected_run_version + 1
        assert run.local_remediation_count == 1
        assert (
            await work.session.get(SubscriptionTask, proposal.decision.task_id)
        ).state == "queued"
        scheduler = await work.session.get(SubscriptionSchedulerRun, run.id)
        assert scheduler.candidate_state == "open"
        assert scheduler.candidate_epoch == proposal.review.candidate_epoch + 1
        assert await work.session.get(SubscriptionRepairDebit, proposal.attempt_id) is not None
        assert (
            await work.session.get(SubscriptionAttemptResult, proposal.attempt_id)
        ).application_payload == original
        assert not tuple(
            await work.session.scalars(
                select(RunCommand.id).where(
                    RunCommand.run_id == run.id, RunCommand.status == "pending"
                )
            )
        )
    async with factory() as work:
        await handler(command, work)
        assert (await work.runs.get(command.run_id)).version == run.version
    await PostgresCommandRepository(session_factory).complete(
        command.id, worker_id=command.lease_owner
    )
    following = await SubscriptionDecisionExecutor(factory).admit_next(
        "candidate-repair", _reservation()
    )
    assert following is not None and following.task.task_id == proposal.decision.task_id
    request = await SubscriptionRequestBuilder(factory).build(following)
    assert request.run_state is RunState.REMEDIATING
    assert request.task.owned_paths == ("apps",)
    assert runner.calls == runner.runner.calls == 1


@pytest.mark.integration
async def test_operator_revision_requires_a_new_acceptance_and_pr_gate(session_factory, tmp_path):
    case, original_gate = await frozen_publication_case(session_factory, tmp_path)
    factory, original, dispatch, _, controller, runner, git = case
    command = await revision_command(
        factory,
        session_factory,
        original.decision.run_id,
        feedback="Recheck this unchanged candidate and submit fresh acceptance.",
    )
    service = CandidateRevisionService(
        dispatch._store, ApprovedPlanLoader(dispatch._store), lambda _: git
    )
    async with factory() as work:
        await service.execute(command, work)
    commands = PostgresCommandRepository(session_factory)
    await commands.complete(command.id, worker_id=command.lease_owner)
    selection, accepting = await accept_reopened_candidate(
        factory, session_factory, original, dispatch, git
    )
    validate = await commands.claim_next(worker_id="revision-validation", lease_seconds=120)
    assert validate is not None and validate.command_type == "validate"
    assert validate.payload["acceptance_attempt_id"] == str(accepting.attempt.attempt_id)
    # This is a new command, not replay of the first command's terminal result.
    runner.runner.terminal = replace(
        runner.runner.terminal,
        result=replace(runner.runner.terminal.result, started_at=datetime.now(UTC)),
    )
    async with factory() as work:
        outcome = await controller.validate(validate, work)
    assert outcome.state is RunState.AWAITING_PR_APPROVAL
    assert outcome.pr_evidence_digest != original_gate.pr_evidence_digest
    evidence = decode_pr_approval_evidence(
        await dispatch._store.open_bytes(outcome.pr_evidence_digest)
    )
    acceptance = decode_evidence_manifest(
        await dispatch._store.open_bytes(evidence.acceptance_digest)
    )
    assert acceptance.producer_attempt_id == accepting.attempt.attempt_id
    assert acceptance.selection_attempt_id == selection.attempt.attempt_id
    assert acceptance.candidate_epoch == original.review.candidate_epoch + 2
    async with factory() as work:
        run = await work.runs.get(validate.run_id)
        assert (
            run.local_remediation_count == 0
            and run.pending_evidence_digest == outcome.pr_evidence_digest
        )
        assert (
            await work.subscription_decisions.acceptance_validation_binding(original.attempt_id)
            is not None
        )
    assert runner.calls == runner.runner.calls == 2


@pytest.mark.integration
@pytest.mark.parametrize(
    "feedback",
    [
        "Clarify the failure message within the approved scope.",
        "\n".join(
            f"Requirement {index:04d}: preserve the existing output detail." for index in range(250)
        ),
    ],
    ids=["short-feedback", "feedback-beyond-handoff-summary-limit"],
)
async def test_operator_revision_reopens_primary_and_preserves_automatic_repair_count(
    session_factory, tmp_path, feedback
):
    case, _ = await frozen_publication_case(session_factory, tmp_path)
    factory, proposal, dispatch, _, _, runner, git = case
    command = await revision_command(
        factory, session_factory, proposal.decision.run_id, feedback=feedback
    )
    service = CandidateRevisionService(
        dispatch._store, ApprovedPlanLoader(dispatch._store), lambda _: git
    )
    async with factory() as work:
        await service.execute(command, work)
    async with factory() as work:
        run = await work.runs.get(command.run_id)
        assert run.state is RunState.REMEDIATING and run.local_remediation_count == 0
        assert (
            await work.session.get(SubscriptionTask, proposal.decision.task_id)
        ).state == "queued"
        assert await work.session.get(SubscriptionRepairDebit, proposal.attempt_id) is not None
    async with factory() as work:
        await service.execute(command, work)
        assert (await work.runs.get(command.run_id)).version == run.version
    await PostgresCommandRepository(session_factory).complete(
        command.id, worker_id=command.lease_owner
    )
    following = await SubscriptionDecisionExecutor(factory).admit_next(
        "operator-revision", _reservation()
    )
    assert following is not None and following.task.task_id == proposal.decision.task_id
    request = await SubscriptionRequestBuilder(factory).build(following)
    context = json.dumps(thaw_payload(request.untrusted_context))
    assert all(instruction in context for instruction in feedback.splitlines())
    assert request.task.owned_paths == ("apps",)
    assert runner.calls == runner.runner.calls == 1
