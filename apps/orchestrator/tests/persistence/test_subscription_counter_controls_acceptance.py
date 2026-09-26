"""A7: composed stopped results survive controls and restart without another client."""

import hashlib
from pathlib import Path
from uuid import uuid4

import pytest
from forge.application.services.subscription_profiles import LocalOperatorProfileActor
from forge.application.services.subscription_task_controls import SubscriptionTaskControlService
from forge.artifacts.filesystem import FilesystemArtifactStore
from forge.domain.operation import canonical_digest
from forge.domain.subscription_task_controls import (
    SubscriptionTaskControlRequest,
    TaskControlConflict,
)
from forge.domain.tool import ToolName
from forge.evaluations.check_evidence import read_check_evidence
from forge.persistence.models.subscription import SubscriptionTask
from forge.persistence.models.subscription_results import SubscriptionAttemptResult
from forge.persistence.queries.subscription_tasks import SubscriptionTaskQuery
from forge.worker.composition import compose_worker_handlers
from subscription_counter_manifest import retain_counter_manifest
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_counter_acceptance import PATHS, PRIMARY, WRITER, prepared_counter_case


@pytest.mark.integration
@pytest.mark.parametrize("action", ["pause", "cancel"])
async def test_stored_counter_result_respects_control_across_worker_restart(
    session_factory, tmp_path, action
):
    case = await prepared_counter_case(session_factory, tmp_path)
    handlers = case.handlers
    script = case.script
    actor = LocalOperatorProfileActor()
    key = str(uuid4())
    try:
        worked = await case.worker.run_once()
        assert not script.errors, script.errors
        assert worked.attempt.result.failure is None
        assert worked.attempt.settlement.disposition == "decision_pending"
        task_id = worked.admission.task.task_id
        attempt_id = worked.admission.attempt.attempt_id

        async def request(action, pause_id=None):
            async with case.factory() as work:
                run = await work.runs.get(case.run.id)
                task = await work.session.get(SubscriptionTask, task_id)
                return SubscriptionTaskControlRequest(
                    action=action,
                    expected_run_version=run.version,
                    expected_task_version=task.version,
                    reason="A7 retained result inspection",
                    pause_receipt_id=pause_id,
                )

        async def control(body, mutation_key):
            return await SubscriptionTaskControlService(case.factory).control(
                run_id=case.run.id,
                task_id=task_id,
                actor=actor,
                idempotency_key=mutation_key,
                request=body,
            )

        async with case.factory() as work:
            stored = await work.session.get(SubscriptionAttemptResult, attempt_id)
            original_result = stored.result_digest, canonical_digest(stored.result_payload)
            original_usage = await work.subscription_budget.usage(case.run.id)
            tree = Path((await work.runs.get(case.run.id)).worktree_path)
            assert original_usage.consumed.provider_attempts == 3
            assert original_usage.consumed.named_checks == 2
            assert original_usage.consumed.repairs == 0
            assert original_usage.outstanding.provider_attempts == 0
        original_files = {
            path: hashlib.sha256((tree / path).read_bytes()).hexdigest() for path in PATHS
        }
        body = await request(action)
        receipt = await control(body, key)
        assert receipt.status == ("paused" if action == "pause" else "cancelled")
        assert await control(body, key) == receipt
        async with case.factory() as work:
            assert await work.scheduler._active_count(run_id=case.run.id) == 0

        await handlers.aclose()
        handlers = compose_worker_handlers(
            case.settings,
            session_factory,
            subscription_adapters=(script.adapter(PRIMARY), script.adapter(WRITER)),
        )
        blocked = await handlers.subscription_decision_recovery.reconcile_all()
        assert blocked.applied == 0
        before_resume = await SubscriptionTaskQuery(session_factory).tasks(case.run.id)
        projected = next(row for row in before_resume["tasks"] if row["task_id"] == task_id)
        assert projected["pause_requested"] if action == "pause" else projected["cancel_requested"]
        assert projected["repairs"] == projected["unsettled_effects"] == 0
        assert await control(body, key) == receipt
        resumed = None
        if action == "pause":
            # A stale tab cannot use the pre-pause versions for a fresh operation.
            with pytest.raises(TaskControlConflict):
                await control(body, str(uuid4()))
            resumed = await control(await request("resume", receipt.receipt_id), str(uuid4()))
            assert resumed.status == "decision_pending"
            recovered = await handlers.subscription_decision_recovery.reconcile_all()
            assert recovered.applied == 1 and recovered.deferred == recovered.unsupported == 0
            async with case.factory() as work:
                replay = await work.subscription_decisions.handoff_replay(attempt_id)
                assert replay.accepted and replay.disposition == "handoff_completed"
        else:
            async with case.factory() as work:
                stored = await work.session.get(SubscriptionAttemptResult, attempt_id)
                assert not stored.accepted
                task = await work.session.get(SubscriptionTask, task_id)
                assert task.state == "terminal" and task.cancel_requested
                assert await work.subscription_decisions.handoff_replay(attempt_id) is None
        assert (await handlers.subscription_decision_recovery.reconcile_all()).applied == 0
        assert await control(body, key) == receipt
        async with case.factory() as work:
            stored = await work.session.get(SubscriptionAttemptResult, attempt_id)
            assert (
                stored.result_digest,
                canonical_digest(stored.result_payload),
            ) == original_result
            assert await work.subscription_budget.usage(case.run.id) == original_usage
        assert {
            path: hashlib.sha256((tree / path).read_bytes()).hexdigest() for path in PATHS
        } == original_files
        assert len(script.requests) == 3
        passing = next(
            item
            for item in script.receipts
            if item["tool_name"] == ToolName.BUILD_RUN_NAMED_CHECK.value
            and item["status"] == "succeeded"
        )
        store = FilesystemArtifactStore(case.settings.artifact_root)
        grade = await read_check_evidence(
            case.fixture.case_contract,
            store,
            {**passing, "tool_call_id": passing["operation_id"]},
            command_name="unit",
        )
        assert grade is not None and all(grade[0].values()) and all(grade[1].values())
        await retain_counter_manifest(
            case.factory,
            store,
            case.fixture,
            script,
            run_id=case.run.id,
            tmp_path=tmp_path,
            grade=grade,
            scenario=f"A7-stored-result-{action}",
            operator_view={
                "hook": "task_pending_decision_paused_before_application"
                if action == "pause"
                else "result_persisted_before_acceptance",
                "original_control": receipt,
                "resume": resumed,
                "snapshot_after_restart": before_resume,
                "snapshot_after_recovery": await SubscriptionTaskQuery(session_factory).tasks(
                    case.run.id
                ),
                "result_digest_preserved": original_result[0],
                "file_digests_preserved": original_files,
                "provider_attempts_after_control": 0,
                "repair_debits_after_control": 0,
                "handoff_applied_once": action == "pause",
                "cancelled_result_retained_unaccepted": action == "cancel",
            },
            restarted_before_handoff=action == "pause",
        )
    finally:
        await handlers.aclose()
