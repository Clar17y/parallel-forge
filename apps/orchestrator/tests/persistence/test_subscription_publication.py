"""A real accepted subscription candidate reaches the human PR gate."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from forge.application.handlers.release import ApprovePrHandler
from forge.application.ports.worktrees import GitCandidateDiff, GitDiff
from forge.application.services.approved_plan import ApprovedPlanLoader
from forge.application.services.delivery import DeliveryService
from forge.application.services.pr_evidence import PrEvidenceValidator
from forge.application.services.subscription_publication import SubscriptionPublicationController
from forge.domain.approval import (
    ApprovalGate,
    SubscriptionPrApprovalEvidence,
    decode_pr_approval_evidence,
)
from forge.domain.evidence import SubscriptionAcceptanceEvidenceManifest, decode_evidence_manifest
from forge.domain.run import RunState
from forge.persistence.models import AgentExecution, Approval, RunCommand
from forge.persistence.repositories.commands import PostgresCommandRepository
from sqlalchemy import select
from test_scheduler_acceptance import (
    _remove_disposable_subscription_rows,  # noqa: F401 - pytest fixture
)
from test_subscription_acceptance_evidence_persistence import reviewed_acceptance_case
from test_subscription_acceptance_validation import validation_case


async def publication_case(session_factory, tmp_path, *, reviewed=False):
    factory, proposal, dispatch, command, validation, runner, git = await validation_case(
        session_factory, tmp_path, acceptance_factory=reviewed_acceptance_case if reviewed else None
    )
    git.candidate_diff = lambda _: GitCandidateDiff(
        head_sha=proposal.review.candidate.head_sha,
        diff=GitDiff(text="", original_byte_count=0, truncated=False),
        changed_paths=(),
    )
    controller = SubscriptionPublicationController(
        dispatch._store, validation=validation, git_factory=lambda _: git
    )
    return factory, proposal, dispatch, command, controller, runner, git


def publication_validator(dispatch, proposal, git):
    async def base(*args):
        return proposal.worktree.base_sha

    return PrEvidenceValidator(
        dispatch._store,
        ApprovedPlanLoader(dispatch._store),
        lambda _: git,
        SimpleNamespace(get_base=base),
    )


async def approved_publication_case(session_factory, tmp_path):
    (
        factory,
        proposal,
        dispatch,
        validation_command,
        controller,
        runner,
        git,
    ) = await publication_case(session_factory, tmp_path)
    async with factory() as work:
        outcome = await controller.validate(validation_command, work)
    commands = PostgresCommandRepository(session_factory)
    await commands.complete(validation_command.id, worker_id=validation_command.lease_owner)
    actor, approval_id = uuid4(), uuid4()
    async with factory() as work:
        run = await work.runs.get_for_update(validation_command.run_id)
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
            command_type="approve_pr",
            idempotency_key=f"{run.id}:approve-pr",
            payload={"approval_id": str(approval_id)},
            expected_run_version=run.version,
            actor_id=actor,
        )
        await work.commit()
    command = await commands.claim_next(worker_id="approve-pr", lease_seconds=120)
    assert command is not None and command.command_type == "approve_pr"
    validator = publication_validator(dispatch, proposal, git)
    handler = ApprovePrHandler(validator, ApprovedPlanLoader(dispatch._store))
    async with factory() as work:
        await handler(command, work)
    async with factory() as work:
        await handler(command, work)
    await commands.complete(command.id, worker_id=command.lease_owner)
    return factory, proposal, dispatch, validator, approval_id, outcome, runner, git


@pytest.mark.integration
async def test_acceptance_opens_pr_gate_without_inventing_a_reviewer(session_factory, tmp_path):
    factory, proposal, dispatch, command, controller, runner, _ = await publication_case(
        session_factory, tmp_path
    )
    async with factory() as work:
        outcome = await controller.validate(command, work)
    async with factory() as work:
        run = await work.runs.get(command.run_id)
        assert run.state is RunState.AWAITING_PR_APPROVAL
        assert run.pending_gate is ApprovalGate.PR
        assert run.version == command.expected_run_version + 1
        assert run.pending_evidence_digest == outcome.pr_evidence_digest
        evidence = decode_pr_approval_evidence(
            await dispatch._store.open_bytes(outcome.pr_evidence_digest)
        )
        assert isinstance(evidence, SubscriptionPrApprovalEvidence)
        assert "review_digest" not in evidence.model_dump()
        assert evidence.candidate_tree_digest == proposal.review.candidate.tree_digest
        acceptance = decode_evidence_manifest(
            await dispatch._store.open_bytes(evidence.acceptance_digest)
        )
        assert isinstance(acceptance, SubscriptionAcceptanceEvidenceManifest)
        assert acceptance.producer_attempt_id == proposal.attempt_id
        assert not acceptance.selection.review_required and acceptance.review_handoff is None
        assert (
            acceptance.selection.no_review_reason
            in (await dispatch._store.open_bytes(evidence.body_digest)).decode()
        )
        assert (
            await work.session.scalar(
                select(AgentExecution.id).where(
                    AgentExecution.run_id == run.id, AgentExecution.role == "reviewer"
                )
            )
            is None
        )
        assert (
            await work.session.scalar(
                select(RunCommand.id).where(
                    RunCommand.run_id == run.id, RunCommand.command_type == "publish_pr"
                )
            )
            is None
        )
    async with factory() as work:
        assert await controller.validate(command, work) == outcome
    assert runner.calls == runner.runner.calls == 1


@pytest.mark.integration
async def test_reviewed_acceptance_retains_the_actual_handoff_at_the_pr_gate(
    session_factory, tmp_path
):
    factory, proposal, dispatch, command, controller, _, git = await publication_case(
        session_factory, tmp_path, reviewed=True
    )
    async with factory() as work:
        result = await controller.validate(command, work)
    async with factory() as work:
        verified = await publication_validator(dispatch, proposal, git).validate(
            work, command.run_id
        )
        manifest = decode_evidence_manifest(
            await dispatch._store.open_bytes(verified.evidence.acceptance_digest)
        )
        assert manifest.review_handoff == proposal.review.review_handoff
        assert manifest.review_handoff.attempt_id != proposal.attempt_id
        assert manifest.selection.review_required
        assert manifest.review_handoff.review_output.summary in verified.body.decode()
        assert (
            await work.runs.get(command.run_id)
        ).pending_evidence_digest == result.pr_evidence_digest


@pytest.mark.integration
async def test_delivery_routes_acceptance_to_the_subscription_publication_controller(
    session_factory, tmp_path
):
    factory, _, dispatch, command, controller, _, git = await publication_case(
        session_factory, tmp_path
    )
    delivery = DeliveryService(
        dispatch._store, validation=controller._validation, git_factory=lambda _: git
    )
    async with factory() as work:
        outcome = await delivery.validate(command, work)
        assert outcome.state is RunState.AWAITING_PR_APPROVAL


@pytest.mark.integration
async def test_pr_preflight_verifies_subscription_sources_without_creating_evidence(
    session_factory, tmp_path, monkeypatch
):
    factory, proposal, dispatch, command, controller, runner, git = await publication_case(
        session_factory, tmp_path
    )
    async with factory() as work:
        outcome = await controller.validate(command, work)

    async def no_write(*args, **kwargs):
        raise AssertionError("read-only preflight must not recreate evidence")

    monkeypatch.setattr(dispatch._store, "put_bytes", no_write)
    validator = publication_validator(dispatch, proposal, git)
    async with factory() as work:
        verified = await validator.validate(work, command.run_id)
        assert isinstance(verified.evidence, SubscriptionPrApprovalEvidence)
        assert (
            verified.evidence.acceptance_digest
            == (
                await work.evidence.get_by_id(
                    outcome.acceptance_evidence_set_id, run_id=command.run_id
                )
            ).manifest_digest
        )
    assert runner.calls == runner.runner.calls == 1


@pytest.mark.integration
async def test_consumed_subscription_approval_has_read_only_publication_recovery(
    session_factory, tmp_path, monkeypatch
):
    (
        factory,
        proposal,
        dispatch,
        validator,
        approval_id,
        outcome,
        runner,
        _,
    ) = await approved_publication_case(session_factory, tmp_path)
    async with factory() as work:
        verified = await validator.validate_for_publication(
            work, proposal.decision.run_id, approval_id
        )
        assert verified.approved.run.state is RunState.PUBLISHING_PR
        commands = tuple(
            (
                await work.session.scalars(
                    select(RunCommand).where(
                        RunCommand.run_id == proposal.decision.run_id,
                        RunCommand.command_type == "publish_pr",
                    )
                )
            ).all()
        )
        assert len(commands) == 1 and commands[0].payload == {"approval_id": str(approval_id)}
        assert (
            len(
                [
                    event
                    for event in await work.events.list_after(proposal.decision.run_id, 0)
                    if event.event_type == "run.pr_approved"
                ]
            )
            == 1
        )

    def no_git(*args, **kwargs):
        raise AssertionError("historical recovery must not inspect current Git")

    async def no_io(*args, **kwargs):
        raise AssertionError("historical recovery must not write artifacts or query the remote")

    monkeypatch.setattr(dispatch._store, "put_bytes", no_io)
    recovery = PrEvidenceValidator(
        dispatch._store,
        ApprovedPlanLoader(dispatch._store),
        no_git,
        SimpleNamespace(get_base=no_io),
    )
    async with factory() as work:
        frozen = await recovery.for_recovery(work, proposal.decision.run_id, approval_id)
        assert frozen.evidence == verified.evidence
        assert (
            frozen.evidence.acceptance_digest
            == (
                await work.evidence.get_by_id(
                    outcome.acceptance_evidence_set_id, run_id=proposal.decision.run_id
                )
            ).manifest_digest
        )
    assert runner.calls == runner.runner.calls == 1
