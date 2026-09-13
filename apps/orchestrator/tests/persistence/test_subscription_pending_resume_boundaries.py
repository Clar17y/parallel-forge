"""Only an exact operator resume can exempt fully proved stopped decisions."""

from dataclasses import replace
from uuid import uuid4

import pytest
from forge.application.handlers.run_controls import ResumeRunHandler
from forge.application.ports.commands import CommandRecoveryRequired
from forge.artifacts.filesystem import FilesystemArtifactStore
from forge.domain.operation import canonical_digest
from forge.domain.run import RunState
from forge.domain.subscription import decode_subscription_record, encode_subscription_record
from forge.persistence.models.scheduling import (
    SubscriptionScheduledEffect,
    SubscriptionScheduledTask,
    SubscriptionSchedulerRun,
)
from forge.persistence.models.subscription import (
    SubscriptionAttempt,
    SubscriptionClientLaunch,
    SubscriptionTask,
)
from forge.persistence.models.subscription_results import SubscriptionAttemptResult
from forge.persistence.models.subscription_usage import SubscriptionAttemptConsumption
from forge.persistence.repositories.commands import PostgresCommandRepository
from sqlalchemy import select
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_resume_controls import pause_for_resume
from test_subscription_review_selection import selection_case


async def pending_case(session_factory, tmp_path):
    factory, admission, _ = await selection_case(session_factory, tmp_path)
    commands = PostgresCommandRepository(session_factory)
    async with factory() as work:
        sources = await work.commands.list_outstanding_normal(
            run_id=admission.task.run_id, exclude_command_id=None
        )
    assert len(sources) == 1 and sources[0].command_type == "prepare_worktree"
    await commands.complete(sources[0].id, worker_id=sources[0].lease_owner)
    _, resume = await pause_for_resume(factory, session_factory, admission.task.run_id)
    return factory, admission, resume, sources[0]


@pytest.mark.integration
async def test_pending_decision_is_not_quiescent_for_teardown_or_another_command(
    session_factory, tmp_path
):
    factory, admission, resume, source = await pending_case(session_factory, tmp_path)
    async with factory() as work:
        await work.runs.get_for_update(admission.task.run_id)
        for excluded in (None, source.id, uuid4()):
            proof = await work.runs.prove_quiescent(
                admission.task.run_id, exclude_command_id=excluded
            )
            assert not proof.is_quiescent and proof.unsettled_subscription_work == 2
        assert (
            await work.runs.prove_quiescent(admission.task.run_id, exclude_command_id=resume.id)
        ).is_quiescent
        assert (
            await work.session.get(SubscriptionAttempt, admission.attempt.attempt_id)
        ).status == "reconciling"


@pytest.mark.integration
@pytest.mark.parametrize(
    "changed",
    [
        "decision",
        "cross_identity",
        "quota",
        "usage",
        "client",
        "effect",
        "epoch",
        "task_control",
        "scope",
        "lease",
        "other_attempt",
    ],
)
async def test_resume_rejects_unproved_pending_decision_or_other_unsettled_work(
    session_factory, tmp_path, changed
):
    factory, admission, resume, _ = await pending_case(session_factory, tmp_path)
    async with factory() as work:
        identity = admission.attempt.attempt_id
        if changed == "decision":
            result = await work.session.get(SubscriptionAttemptResult, identity)
            result.result_payload = dict(result.result_payload) | {"decision": None}
            result.result_digest = canonical_digest(result.result_payload)
        elif changed == "cross_identity":
            result = await work.session.get(SubscriptionAttemptResult, identity)
            decision = decode_subscription_record(result.result_payload["decision"])
            result.result_payload = dict(result.result_payload) | {
                "decision": encode_subscription_record(replace(decision, run_id=uuid4()))
            }
            result.result_digest = canonical_digest(result.result_payload)
        elif changed == "quota":
            result = await work.session.get(SubscriptionAttemptResult, identity)
            result.result_payload = dict(result.result_payload) | {"quota_exhaustion": {}}
            result.result_digest = canonical_digest(result.result_payload)
        elif changed == "usage":
            consumption = await work.session.get(SubscriptionAttemptConsumption, identity)
            consumption.charged = dict(consumption.charged) | {"provider_attempts": 99}
        elif changed == "client":
            work.session.add(
                SubscriptionClientLaunch(
                    id=uuid4(),
                    attempt_id=identity,
                    launch_id="still-live",
                    worker_identity=admission.lease.owner,
                    state="started",
                )
            )
        elif changed == "effect":
            work.session.add(
                SubscriptionScheduledEffect(
                    id=uuid4(),
                    run_id=admission.task.run_id,
                    task_id=admission.task.task_id,
                    lease_owner=admission.lease.owner,
                    lease_generation=admission.lease.generation,
                    candidate_epoch=admission.candidate_epoch,
                    state="reconciling",
                )
            )
        elif changed == "epoch":
            (
                await work.session.get(SubscriptionSchedulerRun, admission.task.run_id)
            ).candidate_epoch += 1
        elif changed == "task_control":
            (
                await work.session.get(SubscriptionTask, admission.task.task_id)
            ).pause_requested = True
        elif changed == "scope":
            scheduled = await work.session.get(SubscriptionScheduledTask, admission.task.task_id)
            scheduled.owned_paths = ["forged"]
        elif changed == "lease":
            (await work.session.get(SubscriptionAttempt, identity)).lease_generation += 1
        else:
            attempt = await work.session.scalar(
                select(SubscriptionAttempt).where(
                    SubscriptionAttempt.run_id == admission.task.run_id,
                    SubscriptionAttempt.id != identity,
                )
            )
            assert attempt is not None
            attempt.status = "reconciling"
        await work.commit()
    with pytest.raises(CommandRecoveryRequired):
        async with factory() as work:
            await ResumeRunHandler(artifact_store=FilesystemArtifactStore(tmp_path / "artifacts"))(
                resume, work
            )
    async with factory() as work:
        assert (await work.runs.get(admission.task.run_id)).state is RunState.PAUSED
        result = await work.session.get(SubscriptionAttemptResult, admission.attempt.attempt_id)
        assert result.disposition == "decision_pending" and result.application_payload is None
