"""Operator resume retains the exact final acceptance for controller validation."""

from dataclasses import replace
from datetime import timedelta

import pytest
from forge.application.adapters.controller_check import CONTROLLER_CHECK_KIND
from forge.application.handlers.run_controls import ResumeRunHandler
from forge.application.ports.commands import CommandRecoveryRequired
from forge.domain.command import CommandStatus
from forge.domain.run import RunState
from forge.persistence.models import OperationIntent, RunCommand, RunEvent
from forge.persistence.models.scheduling import SubscriptionScheduledTask
from forge.persistence.models.subscription_results import SubscriptionAttemptResult
from forge.persistence.repositories.evidence import PostgresEvidenceRepository
from sqlalchemy import select
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_acceptance_validation import validation_case
from test_subscription_resume_controls import pause_and_resume, pause_for_resume


@pytest.mark.integration
@pytest.mark.parametrize("stage", ["unadmitted", "admitted", "settled"])
async def test_validation_resume_retains_the_original_acceptance(session_factory, tmp_path, stage):
    factory, proposal, dispatch, original, validator, runner, _ = await validation_case(
        session_factory, tmp_path
    )
    async with factory() as work:
        result = await work.session.get(SubscriptionAttemptResult, proposal.attempt_id)
        before = (result.result_digest, result.result_payload, result.application_digest)
        schedule = await work.session.get(SubscriptionScheduledTask, proposal.decision.task_id)
        repairs = schedule.repairs
        binding = await work.subscription_decisions.acceptance_validation_binding(
            proposal.attempt_id
        )
        assert binding is not None and binding.command.id == original.id

    previous_checks = int(stage == "settled")
    if stage == "admitted":
        environment = validator._environment_resolver

        async def interrupted(*args):
            raise RuntimeError("worker stopped after controller admission")

        validator._environment_resolver = interrupted
        with pytest.raises(RuntimeError, match="worker stopped"):
            async with factory() as work:
                await validator.execute(original, work)
        validator._environment_resolver = environment
    if previous_checks:
        async with factory() as work:
            await validator.execute(original, work)
        runner.runner.terminal = replace(
            runner.runner.terminal,
            result=replace(
                runner.runner.terminal.result,
                started_at=runner.runner.terminal.result.started_at + timedelta(seconds=1),
            ),
        )
    continued = await pause_and_resume(
        factory,
        session_factory,
        original.run_id,
        dispatch._store,
        source=original,
    )
    assert continued is not None and continued.command_type == "validate"
    assert continued.id != original.id
    assert continued.payload["acceptance_attempt_id"] == str(proposal.attempt_id)
    assert continued.payload["semantic_attempt"] == (
        original.payload["semantic_attempt"] + int(stage != "unadmitted")
    )
    assert runner.calls == runner.runner.calls == previous_checks
    async with factory() as work:
        assert (await work.runs.get(original.run_id)).state is RunState.VALIDATING
        assert (await work.commands.get(original.id)).status is CommandStatus.CANCELLED
        result = await work.session.get(SubscriptionAttemptResult, proposal.attempt_id)
        assert (result.result_digest, result.result_payload, result.application_digest) == before
        schedule = await work.session.get(SubscriptionScheduledTask, proposal.decision.task_id)
        assert schedule.repairs == repairs
        evidence = await validator.execute(continued, work)
    async with factory() as work:
        assert await validator.execute(continued, work) == evidence
        retained = await work.subscription_decisions.acceptance_validation_binding(
            proposal.attempt_id
        )
        assert retained is not None and retained.command.id == original.id
    assert runner.calls == runner.runner.calls == previous_checks + 1


@pytest.mark.integration
@pytest.mark.parametrize("changed", ["candidate_event", "acceptance", "projection", "receipt"])
async def test_validation_resume_rejects_changed_source_evidence(
    session_factory, tmp_path, changed, monkeypatch
):
    factory, proposal, dispatch, original, validator, _, _ = await validation_case(
        session_factory, tmp_path
    )
    if changed in {"candidate_event", "acceptance"}:

        async def interrupted(*args):
            raise RuntimeError("stopped after admission")

        validator._environment_resolver = interrupted
        with pytest.raises(RuntimeError, match="stopped after admission"):
            async with factory() as work:
                await validator.execute(original, work)
    else:
        async with factory() as work:
            await validator.execute(original, work)
    _, resume = await pause_for_resume(factory, session_factory, original.run_id, source=original)
    async with factory() as work:
        if changed == "candidate_event":
            event = await work.session.scalar(
                select(RunEvent).where(
                    RunEvent.run_id == original.run_id,
                    RunEvent.event_type == "run.validation_started",
                )
            )
            event.payload = dict(event.payload) | {
                "candidate": dict(event.payload["candidate"]) | {"tree_digest": "f" * 64}
            }
        elif changed == "acceptance":
            command = await work.session.get(RunCommand, original.id)
            command.payload = dict(command.payload) | {"acceptance_attempt_id": str(resume.id)}
        elif changed == "projection":
            # The database already rejects changes to immutable evidence. Also
            # exercise the verifier against a mismatched repository projection.
            read = PostgresEvidenceRepository.get_by_id

            async def altered_projection(self, *args, **kwargs):
                evidence = await read(self, *args, **kwargs)
                if evidence.run_id == original.run_id and evidence.kind == "validation":
                    return replace(evidence, candidate_tree_digest="f" * 64)
                return evidence

            monkeypatch.setattr(PostgresEvidenceRepository, "get_by_id", altered_projection)
        else:
            intent = await work.session.scalar(
                select(OperationIntent).where(
                    OperationIntent.run_id == original.run_id,
                    OperationIntent.operation_kind == CONTROLLER_CHECK_KIND,
                )
            )
            assert intent is not None
            intent.outcome_payload = dict(intent.outcome_payload) | {
                "command_result_digest": "f" * 64
            }
        await work.commit()
    with pytest.raises(CommandRecoveryRequired):
        async with factory() as work:
            await ResumeRunHandler(artifact_store=dispatch._store)(resume, work)
    async with factory() as work:
        assert (await work.runs.get(original.run_id)).state is RunState.PAUSED
        assert (await work.commands.get(original.id)).status is CommandStatus.LEASED
        result = await work.session.get(SubscriptionAttemptResult, proposal.attempt_id)
        assert result.disposition == "acceptance_prepared"
