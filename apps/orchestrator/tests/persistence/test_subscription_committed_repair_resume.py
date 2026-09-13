"""A failed delivery cannot repeat an already committed subscription repair."""

from types import SimpleNamespace

import pytest
from forge.application.services.subscription_remote_remediation import (
    SubscriptionRemoteRemediationController,
)
from forge.domain.command import CommandStatus
from forge.domain.run import RunState
from forge.persistence.models.scheduling import SubscriptionScheduledTask
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_base_adoption import base_adoption_case
from test_subscription_remote_remediation import remote_failure_case
from test_subscription_resume_controls import pause_and_resume


async def failed_repair_case(session_factory, tmp_path, kind):
    if kind == "base":
        case = await base_adoption_case(session_factory, tmp_path)
        factory, original, command = case.factory, case.original, case.command
        dispatch, commands, repairs = case.dispatch, case.commands, case.repairs
        service = case.service
    else:
        (
            factory,
            original,
            dispatch,
            validator,
            commands,
            _,
            command,
            _,
            _,
        ) = await remote_failure_case(session_factory, tmp_path)
        repairs = SubscriptionRemoteRemediationController(
            dispatch._store, validator, validator._git_factory
        )
        service = repairs
    async with factory() as work:
        await service.execute(command, work)
    failed = await commands.fail(
        command.id, worker_id=command.lease_owner, error="delivery completion unavailable"
    )
    assert failed.status is CommandStatus.FAILED
    return SimpleNamespace(
        factory=factory,
        original=original,
        command=command,
        dispatch=dispatch,
        commands=commands,
        repairs=repairs,
        failed=failed,
    )


@pytest.mark.integration
@pytest.mark.parametrize("kind", ["remote", "base"])
async def test_resume_acknowledges_committed_repair_without_rewriting_failed_delivery(
    session_factory, tmp_path, kind
):
    case = await failed_repair_case(session_factory, tmp_path, kind)
    continued = await pause_and_resume(
        case.factory,
        session_factory,
        case.command.run_id,
        case.dispatch._store,
        repairs=case.repairs,
    )
    assert continued is None
    async with case.factory() as work:
        assert await work.commands.get(case.command.id) == case.failed
        assert (await work.runs.get(case.command.run_id)).state is RunState.REMEDIATING
        task = await work.session.get(SubscriptionScheduledTask, case.original.decision.task_id)
        assert task.state == "queued" and task.repairs == 1
