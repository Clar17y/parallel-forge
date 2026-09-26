"""Control snapshots distinguish audited requests from settled stop authority."""

from copy import deepcopy

import pytest
from forge.api.schemas.subscription_tasks import SubscriptionTaskPage
from forge.application.services.subscription_task_controls import SubscriptionTaskControlService
from forge.persistence.models.api import ApiMutation
from forge.persistence.models.subscription_task_stops import SubscriptionTaskStop
from forge.persistence.queries.subscription_tasks import SubscriptionTaskQuery
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_scope_application import scope_case
from test_subscription_task_control_recovery import (
    active_case,
    control,
    finish_client,
    scope_result,
)
from test_subscription_task_controls import _control, _request, _seed, _service


async def snapshot(session_factory, run_id, task_id):
    page = SubscriptionTaskPage.model_validate(
        await SubscriptionTaskQuery(session_factory).tasks(run_id)
    )
    return page, next(task for task in page.tasks if task.task_id == task_id)


@pytest.mark.integration
async def test_queued_control_projection_survives_restart_and_follows_causal_receipt_order(
    session_factory,
    persisted_run,
):
    _, child = await _seed(session_factory, persisted_run)
    paused = await _control(_service(session_factory), persisted_run.id, child, _request("pause"))
    page, task = await snapshot(session_factory, persisted_run.id, child)
    assert page.run_version == 0 and page.run_allows_execution
    assert task.control.status == "paused" and task.control.pause_receipt_id == paused.receipt_id
    assert task.control.receipt_id == paused.receipt_id and task.control.reason == paused.reason
    resumed = await _control(
        _service(session_factory),
        persisted_run.id,
        child,
        _request("resume", 1, pause_receipt_id=paused.receipt_id),
    )
    _, task = await snapshot(session_factory, persisted_run.id, child)
    assert task.control.receipt_id == resumed.receipt_id and task.control.status == "queued"
    assert task.control.pause_receipt_id is None and not task.pause_requested
    cancelled = await _control(
        _service(session_factory), persisted_run.id, child, _request("cancel", 2)
    )
    _, task = await snapshot(session_factory, persisted_run.id, child)
    assert task.control.receipt_id == cancelled.receipt_id and task.control.status == "cancelled"


@pytest.mark.integration
async def test_running_pause_projection_changes_only_after_durable_stop_settlement(
    session_factory, tmp_path
):
    factory, _, child, executor, proof = await active_case(session_factory, tmp_path)
    paused = await control(factory, child, "pause")
    _, task = await snapshot(session_factory, child.task.run_id, child.task.task_id)
    assert task.control.status == "pause_requested" and task.control.pause_receipt_id is None
    await finish_client(factory, child, proof)
    await executor.settle(child, scope_result(child, proof))
    _, task = await snapshot(session_factory, child.task.run_id, child.task.task_id)
    assert task.control.status == "pause_requested" and task.control.pause_receipt_id is None
    assert (await SubscriptionTaskControlService(factory).reconcile_all()).stopped == 1
    _, task = await snapshot(session_factory, child.task.run_id, child.task.task_id)
    assert task.control.status == "paused" and task.control.pause_receipt_id == paused.receipt_id
    async with factory() as work:
        original = await work.session.get(ApiMutation, paused.receipt_id)
        assert original.response_payload["receipt"]["status"] == "pause_requested"


@pytest.mark.integration
@pytest.mark.parametrize("tamper", ["receipt", "settlement"])
async def test_unverified_control_evidence_never_presents_a_resumable_pause(
    session_factory, tmp_path, tamper
):
    factory, _, _, child = await scope_case(session_factory, tmp_path)
    paused = await control(factory, child, "pause")
    async with factory() as work:
        if tamper == "receipt":
            row = await work.session.get(ApiMutation, paused.receipt_id)
            payload = deepcopy(row.response_payload)
            payload["receipt"]["reason"] = "token=must-not-project"
            row.response_payload = payload
        else:
            stop = await work.session.get(SubscriptionTaskStop, paused.receipt_id)
            stop.settlement_digest = "0" * 64
        await work.commit()
    _, task = await snapshot(session_factory, child.task.run_id, child.task.task_id)
    assert task.control is None and task.pause_requested
    assert "must-not-project" not in task.model_dump_json()
