"""Failed final checks reopen bounded work without rewriting accepted history."""

from copy import deepcopy
from dataclasses import replace

import pytest
from forge.application.ports.commands import CommandRecoveryRequired
from forge.application.ports.subscription_decisions import SubscriptionDecisionError
from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
from forge.application.services.subscription_requests import SubscriptionRequestBuilder
from forge.domain.event import thaw_payload
from forge.domain.run import RunState
from forge.domain.subscription import TaskHandoff, decode_subscription_record
from forge.persistence.models import Run, RunEvent
from forge.persistence.models.scheduling import SubscriptionScheduledTask, SubscriptionSchedulerRun
from forge.persistence.models.subscription import SubscriptionDecisionRecord, SubscriptionTask
from forge.persistence.models.subscription_results import (
    SubscriptionAttemptResult,
    SubscriptionRepairDebit,
)
from forge.persistence.repositories.commands import PostgresCommandRepository
from forge.persistence.repositories.runs import PostgresRunRepository
from sqlalchemy import select
from test_scheduler_acceptance import (
    _remove_disposable_subscription_rows,  # noqa: F401 - pytest fixture
)
from test_subscription_publication import publication_case
from test_subscription_usage import _reservation


async def failed_validation_case(session_factory, tmp_path):
    case = await publication_case(session_factory, tmp_path)
    runner = case[5]
    runner.runner.terminal = replace(
        runner.runner.terminal, result=replace(runner.runner.terminal.result, exit_code=1)
    )
    return case


@pytest.mark.integration
async def test_failed_final_validation_requeues_once_without_rewriting_acceptance(
    session_factory, tmp_path
):
    factory, proposal, _, command, controller, runner, _ = await failed_validation_case(
        session_factory, tmp_path
    )
    async with factory() as work:
        original = await work.session.get(SubscriptionAttemptResult, proposal.attempt_id)
        original_source = (
            original.result_digest,
            original.application_digest,
            deepcopy(original.application_payload),
        )
        primary_version = (
            await work.session.get(SubscriptionTask, proposal.decision.task_id)
        ).version
        repairs = (
            await work.session.get(SubscriptionScheduledTask, proposal.decision.task_id)
        ).repairs
    async with factory() as work:
        outcome = await controller.validate(command, work)
    assert outcome.state is RunState.REMEDIATING
    async with factory() as work:
        assert await controller.validate(command, work) == outcome
    async with factory() as work:
        run = await work.runs.get(command.run_id)
        primary = await work.session.get(SubscriptionTask, proposal.decision.task_id)
        scheduled = await work.session.get(SubscriptionScheduledTask, proposal.decision.task_id)
        scheduler = await work.session.get(SubscriptionSchedulerRun, run.id)
        retained = await work.session.get(SubscriptionAttemptResult, proposal.attempt_id)
        assert run.state is RunState.REMEDIATING and run.pending_gate is None
        assert run.version == command.expected_run_version + 1
        assert run.local_remediation_count == 1
        assert primary.state == scheduled.state == "queued"
        assert primary.version == primary_version + 1
        assert scheduled.repairs == repairs + 1
        assert scheduler.candidate_state == "open"
        assert scheduler.candidate_epoch == proposal.review.candidate_epoch + 1
        assert await work.session.get(SubscriptionRepairDebit, proposal.attempt_id) is not None
        assert retained.accepted and retained.disposition == "acceptance_prepared"
        assert (
            retained.result_digest,
            retained.application_digest,
            retained.application_payload,
        ) == original_source
        binding = await work.subscription_decisions.acceptance_validation_binding(
            proposal.attempt_id
        )
        assert binding is not None and binding.application_digest == original_source[1]
    assert runner.calls == runner.runner.calls == 1


@pytest.mark.integration
async def test_repair_primary_receives_failed_checks_under_its_original_contract(
    session_factory, tmp_path
):
    factory, proposal, _, command, controller, _, _ = await failed_validation_case(
        session_factory, tmp_path
    )
    async with factory() as work:
        outcome = await controller.validate(command, work)
    await PostgresCommandRepository(session_factory).complete(
        command.id, worker_id=command.lease_owner
    )
    following = await SubscriptionDecisionExecutor(factory).admit_next(
        "validation-repair", _reservation()
    )
    assert following is not None and following.task.task_id == proposal.decision.task_id
    request = await SubscriptionRequestBuilder(factory).build(following)
    assert request.run_state is RunState.REMEDIATING
    assert request.task == following.task and request.attempt_budget == _reservation()
    assert request.task.owned_paths == ("apps",)
    assert request.untrusted_context["candidate_epoch"] == proposal.review.candidate_epoch + 1
    context = next(
        item
        for item in request.untrusted_context["task_outcomes"]
        if item["task_id"] == str(proposal.decision.task_id)
    )
    handoff = decode_subscription_record(thaw_payload(context["recorded_handoff"]))
    assert isinstance(handoff, TaskHandoff)
    assert "Controller final validation failed: unit" in handoff.summary
    assert outcome.validation_digest in handoff.summary
    async with factory() as work:
        debit = await work.session.get(SubscriptionRepairDebit, proposal.attempt_id)
        assert debit.next_attempt_id == following.attempt.attempt_id


@pytest.mark.integration
@pytest.mark.parametrize("limit", ["task", "run"])
async def test_exhausted_final_validation_repair_requires_human_intervention(
    session_factory, tmp_path, limit
):
    factory, proposal, _, command, controller, runner, _ = await failed_validation_case(
        session_factory, tmp_path
    )
    async with factory() as work:
        if limit == "task":
            scheduled = await work.session.get(SubscriptionScheduledTask, proposal.decision.task_id)
            scheduled.repairs = scheduled.max_repairs
        else:
            run = await work.session.get(Run, command.run_id)
            run.local_remediation_count = proposal.policy.local_remediation_limit
        await work.commit()
    async with factory() as work:
        outcome = await controller.validate(command, work)
    assert outcome.state is RunState.AWAITING_HUMAN_INTERVENTION
    async with factory() as work:
        assert await controller.validate(command, work) == outcome
        scheduler = await work.session.get(SubscriptionSchedulerRun, command.run_id)
        assert (
            scheduler.candidate_state == "closed"
            and scheduler.candidate_epoch == proposal.review.candidate_epoch
        )
        assert await work.session.get(SubscriptionRepairDebit, proposal.attempt_id) is None
        assert (
            await work.session.get(SubscriptionTask, proposal.decision.task_id)
        ).state == "blocked"
    assert runner.calls == runner.runner.calls == 1


@pytest.mark.integration
async def test_validation_repair_transition_failure_rolls_back_budget_and_requeue(
    session_factory, tmp_path, monkeypatch
):
    factory, proposal, _, command, controller, runner, _ = await failed_validation_case(
        session_factory, tmp_path
    )
    begin = PostgresRunRepository.begin_local_remediation

    async def crash(self, *args, **kwargs):
        await begin(self, *args, **kwargs)
        raise RuntimeError("crash after repair transition")

    with monkeypatch.context() as patch:
        patch.setattr(PostgresRunRepository, "begin_local_remediation", crash)
        async with factory() as work:
            with pytest.raises(RuntimeError, match="crash after repair transition"):
                await controller.validate(command, work)
    async with factory() as work:
        run = await work.runs.get(command.run_id)
        assert run.state is RunState.VALIDATING and run.local_remediation_count == 0
        assert (
            await work.session.get(SubscriptionTask, proposal.decision.task_id)
        ).state == "blocked"
        assert await work.session.get(SubscriptionRepairDebit, proposal.attempt_id) is None
    async with factory() as work:
        assert (await controller.validate(command, work)).state is RunState.REMEDIATING
    assert runner.calls == runner.runner.calls == 1


@pytest.mark.integration
@pytest.mark.parametrize("corruption", ["event", "handoff", "debit"])
async def test_repair_replay_requires_retained_decision_and_budget_evidence(
    session_factory, tmp_path, corruption
):
    factory, proposal, _, command, controller, runner, _ = await failed_validation_case(
        session_factory, tmp_path
    )
    async with factory() as work:
        outcome = await controller.validate(command, work)
    async with factory() as work:
        if corruption == "event":
            event = await work.session.scalar(
                select(RunEvent).where(
                    RunEvent.run_id == command.run_id,
                    RunEvent.event_type == "run.subscription_validation_rejected",
                )
            )
            event.payload = {**event.payload, "unbound_decision": True}
        elif corruption == "handoff":
            handoff = await work.session.scalar(
                select(SubscriptionDecisionRecord).where(
                    SubscriptionDecisionRecord.run_id == command.run_id,
                    SubscriptionDecisionRecord.idempotency_key
                    == f"validation-rejection:{command.id}",
                )
            )
            handoff.payload = {**handoff.payload, "summary": "Different controller decision"}
        else:
            await work.session.delete(
                await work.session.get(SubscriptionRepairDebit, proposal.attempt_id)
            )
        await work.commit()
    async with factory() as work:
        with pytest.raises((CommandRecoveryRequired, SubscriptionDecisionError)):
            await controller.validate(command, work)
    async with factory() as work:
        run = await work.runs.get(command.run_id)
        assert run.version == outcome.version and run.local_remediation_count == 1
        if corruption == "debit":
            assert await work.session.get(SubscriptionRepairDebit, proposal.attempt_id) is None
    assert runner.calls == runner.runner.calls == 1


@pytest.mark.integration
async def test_cancelled_controller_check_cannot_spend_a_validation_repair(
    session_factory, tmp_path
):
    factory, proposal, _, command, controller, runner, _ = await failed_validation_case(
        session_factory, tmp_path
    )
    runner.runner.terminal = replace(runner.runner.terminal, caller_cancelled=True)
    async with factory() as work:
        with pytest.raises(CommandRecoveryRequired, match="cancelled"):
            await controller.validate(command, work)
    async with factory() as work:
        run = await work.runs.get(command.run_id)
        assert run.state is RunState.VALIDATING and run.local_remediation_count == 0
        assert await work.session.get(SubscriptionRepairDebit, proposal.attempt_id) is None
