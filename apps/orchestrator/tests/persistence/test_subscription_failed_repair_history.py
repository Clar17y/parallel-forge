"""Prepared publication must re-prove failed-delivery acknowledgment at the write boundary."""

import pytest
from forge.application.services.recovery import OperationExecutor
from forge.application.services.release import ReleaseService
from forge.domain.run import RunState
from forge.persistence.models import RunCommand, RunEvent
from forge.persistence.repositories.operations import PostgresOperationRepository
from sqlalchemy import select
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_base_publication import adopted_acceptance_case
from test_subscription_remote_publication import remote_acceptance_case


@pytest.mark.integration
@pytest.mark.parametrize("kind", ["remote", "base"])
@pytest.mark.parametrize("changed", ["binding", "failed_source", "resume_status"])
async def test_reviewed_push_rejects_acknowledgment_changed_after_final_checks(
    session_factory, tmp_path, kind, changed
):
    case = (
        await adopted_acceptance_case(session_factory, tmp_path, fail_after_repair=True)
        if kind == "base"
        else await remote_acceptance_case(session_factory, tmp_path, fail_after_repair=True)
    )
    source = case.command if kind == "base" else case.repair_command
    async with case.factory() as work:
        outcome = await case.controller.validate(case.validation_command, work)
        assert outcome.state is RunState.MONITORING_PR
    await case.commands.complete(
        case.validation_command.id, worker_id=case.validation_command.lease_owner
    )
    push = await case.commands.claim_next(worker_id="changed-acknowledgment", lease_seconds=120)
    assert push is not None and push.command_type == "push_reviewed_pr"
    async with case.factory() as work:
        event = await work.session.scalar(
            select(RunEvent).where(
                RunEvent.run_id == source.run_id,
                RunEvent.event_type == "run.resumed",
            )
        )
        if changed == "binding":
            values = event.payload["subscription_delivery_acknowledgments"]
            event.payload = dict(event.payload) | {
                "subscription_delivery_acknowledgments": [dict(values[0]) | {"extra": True}],
            }
        elif changed == "failed_source":
            (await work.session.get(RunCommand, source.id)).error_summary = "different failure"
        else:
            from uuid import UUID

            (
                await work.session.get(RunCommand, UUID(event.payload["command_id"]))
            ).status = "FAILED"
        await work.commit()
    pushes = []

    class Push:
        async def push(self, tree, policy, head):
            pushes.append(head)

    release = ReleaseService(
        case.validator,
        case.writes,
        lambda _: Push(),
        OperationExecutor(PostgresOperationRepository(session_factory)),
    )
    for _ in range(2):
        async with case.factory() as work:
            await release.push_reviewed(push, work)
    assert pushes == []
    async with case.factory() as work:
        assert (await work.runs.get(source.run_id)).state is RunState.AWAITING_HUMAN_INTERVENTION
        rejected = [event for event in await work.events.list_after(source.run_id, 0)
                    if event.event_type == "run.publication_evidence_rejected"]
        assert len(rejected) == 1 and rejected[0].payload["source_command_id"] == str(push.id)
