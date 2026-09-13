"""Publication re-proves outcomes instead of trusting a validation summary."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import forge.application.services.validation as validation_module
import pytest
from forge.application.ports.commands import CommandLeaseLost, CommandRecoveryRequired
from forge.application.ports.worktrees import GitSnapshotFile
from forge.application.services.pr_evidence import PrEvidenceValidationError
from forge.domain.approval import decode_pr_approval_evidence
from forge.domain.evidence import EvidenceStatus
from forge.domain.run import RunState
from forge.persistence.models import RunCommand
from forge.persistence.models.subscription_results import SubscriptionRepairDebit
from forge.persistence.repositories.runs import PostgresRunRepository
from test_scheduler_acceptance import (
    _remove_disposable_subscription_rows,  # noqa: F401 - pytest fixture
)
from test_subscription_publication import publication_case, publication_validator


@pytest.mark.integration
@pytest.mark.parametrize("failed_check", [False, True])
async def test_expired_lease_after_final_observation_cannot_publish_a_decision(
    session_factory, tmp_path, monkeypatch, failed_check
):
    factory, proposal, _, command, controller, runner, _ = await publication_case(
        session_factory, tmp_path
    )
    if failed_check:
        runner.runner.terminal = replace(
            runner.runner.terminal, result=replace(runner.runner.terminal.result, exit_code=1)
        )
    capture, observations = controller._candidate, 0
    async with factory() as work:

        async def expire(source):
            nonlocal observations
            observed = await capture(source)
            observations += 1
            if observations == 2:
                (await work.session.get(RunCommand, command.id)).lease_expires_at = datetime.now(
                    UTC
                ) - timedelta(seconds=1)
                await work.session.flush()
            return observed

        monkeypatch.setattr(controller, "_candidate", expire)
        with pytest.raises(CommandLeaseLost):
            await controller.validate(command, work)
    async with factory() as work:
        run = await work.runs.get(command.run_id)
        assert run.state is RunState.VALIDATING and run.pending_gate is None
        assert run.local_remediation_count == 0
        assert await work.session.get(SubscriptionRepairDebit, proposal.attempt_id) is None


@pytest.mark.integration
async def test_publication_gate_rolls_back_and_retries_without_another_check(
    session_factory, tmp_path, monkeypatch
):
    factory, _, _, command, controller, runner, _ = await publication_case(
        session_factory, tmp_path
    )
    await_approval = PostgresRunRepository.await_approval

    async def crash(self, *args, **kwargs):
        await await_approval(self, *args, **kwargs)
        raise RuntimeError("crash after PR gate")

    with monkeypatch.context() as patch:
        patch.setattr(PostgresRunRepository, "await_approval", crash)
        async with factory() as work:
            with pytest.raises(RuntimeError, match="crash after PR gate"):
                await controller.validate(command, work)
    async with factory() as work:
        run = await work.runs.get(command.run_id)
        assert run.state is RunState.VALIDATING and run.pending_gate is None
        assert not [
            event
            for event in await work.events.list_after(run.id, 0)
            if event.event_type == "run.subscription_acceptance_decided"
        ]
    async with factory() as work:
        assert (await controller.validate(command, work)).state is RunState.AWAITING_PR_APPROVAL
    assert runner.calls == runner.runner.calls == 1


@pytest.mark.integration
async def test_same_head_content_drift_during_storage_cannot_open_the_gate(
    session_factory, tmp_path, monkeypatch
):
    factory, _, _, command, controller, _, git = await publication_case(session_factory, tmp_path)
    freeze = controller._publication.freeze
    snapshot = git.working_tree_snapshot

    def drift(*args, **kwargs):
        return replace(
            snapshot(*args, **kwargs),
            files=(
                GitSnapshotFile(
                    path="changed.py",
                    mode="100644",
                    content_digest="e" * 64,
                    byte_count=1,
                ),
            ),
            changed_paths=("changed.py",),
        )

    async def freeze_then_drift(*args, **kwargs):
        frozen = await freeze(*args, **kwargs)
        monkeypatch.setattr(git, "working_tree_snapshot", drift)
        return frozen

    monkeypatch.setattr(controller._publication, "freeze", freeze_then_drift)
    async with factory() as work:
        with pytest.raises(CommandRecoveryRequired, match="candidate differs"):
            await controller.validate(command, work)
    async with factory() as work:
        run = await work.runs.get(command.run_id)
        assert run.state is RunState.VALIDATING and run.pending_gate is None


@pytest.mark.integration
@pytest.mark.parametrize(
    "field", ["acceptance_digest", "validation_digest", "runner_evidence_digest", "body_digest"]
)
async def test_pr_preflight_refuses_altered_retained_artifact_bytes(
    session_factory, tmp_path, monkeypatch, field
):
    factory, proposal, dispatch, command, controller, runner, git = await publication_case(
        session_factory, tmp_path
    )
    async with factory() as work:
        result = await controller.validate(command, work)
    evidence = decode_pr_approval_evidence(
        await dispatch._store.open_bytes(result.pr_evidence_digest)
    )
    original = dispatch._store.open_bytes

    async def corrupted(digest):
        wire = await original(digest)
        return wire + b" " if digest == getattr(evidence, field) else wire

    monkeypatch.setattr(dispatch._store, "open_bytes", corrupted)
    async with factory() as work:
        with pytest.raises(PrEvidenceValidationError):
            await publication_validator(dispatch, proposal, git).validate(work, command.run_id)
    assert runner.calls == runner.runner.calls == 1


@pytest.mark.integration
async def test_bounded_output_is_publishable_with_its_verified_truncation(
    session_factory, tmp_path
):
    factory, _, dispatch, command, controller, runner, _ = await publication_case(
        session_factory, tmp_path
    )
    wire = json.dumps(
        {
            "captured_byte_count": 0,
            "encoding": "utf-8-replacement",
            "original_byte_count": 100,
            "stream": "stdout",
            "text": "",
            "truncated": True,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    output = await dispatch._store.put_bytes(
        wire, media_type="application/vnd.forge.command-output+json"
    )
    runner.runner.terminal = replace(
        runner.runner.terminal,
        result=replace(
            runner.runner.terminal.result,
            stdout_digest=output.digest,
            stdout_original_byte_count=100,
            stdout_truncated=True,
        ),
    )
    async with factory() as work:
        assert (await controller.validate(command, work)).state is RunState.AWAITING_PR_APPROVAL


@pytest.mark.integration
async def test_passed_summary_cannot_hide_a_failed_terminal_command(
    session_factory, tmp_path, monkeypatch
):
    factory, _, _, command, controller, runner, _ = await publication_case(
        session_factory, tmp_path
    )
    runner.runner.terminal = replace(
        runner.runner.terminal, result=replace(runner.runner.terminal.result, exit_code=1)
    )
    member = validation_module.ValidationEvidenceMember

    def inconsistent_summary(**values):
        return member(**(values | {"status": EvidenceStatus.PASSED, "exit_code": 0}))

    # Inject an inconsistent upstream projection while keeping the actual durable
    # controller request, failing terminal receipt and artifact bytes intact.
    monkeypatch.setattr(validation_module, "ValidationEvidenceMember", inconsistent_summary)
    async with factory() as work:
        with pytest.raises(CommandRecoveryRequired, match="validation|runner|result"):
            await controller.validate(command, work)
    async with factory() as work:
        run = await work.runs.get(command.run_id)
        assert run.state is RunState.VALIDATING and run.pending_gate is None
