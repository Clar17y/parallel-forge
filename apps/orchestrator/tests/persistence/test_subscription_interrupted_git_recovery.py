"""Owner acknowledgement of an unknown unpublished preparation preserves a saved decision."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from forge.api.schemas.subscription_tasks import SubscriptionAttemptPage
from forge.application.ports.worktrees import GitWorkingTreeSnapshot, ManagedWorktree
from forge.application.services.auth import AuthenticatedActor
from forge.application.services.subscription_candidate import SubscriptionCandidateApplication
from forge.application.services.subscription_effect_recovery import SubscriptionEffectRecovery
from forge.application.services.subscription_recovery import SubscriptionRecoveryService
from forge.application.services.tool_recovery import ToolRecoveryService
from forge.application.services.tools import _git_prepare_payload
from forge.domain.operation import canonical_digest
from forge.domain.resource import WorktreeIdentity
from forge.domain.subscription import SpecialistPurpose, ToolCallBinding, encode_subscription_record
from forge.domain.subscription_recovery import RecoveryApplyRequest, RecoveryPreviewRequest
from forge.domain.tool import SubscriptionToolAuthorizationContext, ToolName
from forge.persistence.models import Run
from forge.persistence.models.execution import OperationIntent, ToolCall
from forge.persistence.models.scheduling import (
    SubscriptionScheduledEffect,
    SubscriptionScheduledTask,
)
from forge.persistence.models.subscription import (
    SubscriptionAttempt,
    SubscriptionClientLaunch,
    SubscriptionOperationBinding,
    SubscriptionTask,
)
from forge.persistence.models.subscription_recovery import SubscriptionRecoveryWorker
from forge.persistence.models.subscription_results import (
    SubscriptionAttemptResult,
    SubscriptionRepairDebit,
)
from forge.persistence.models.subscription_usage import SubscriptionAttemptConsumption
from forge.persistence.queries.recovery import startup_intervention_hold
from forge.persistence.queries.subscription_tasks import SubscriptionTaskQuery
from forge.persistence.repositories.subscription_interrupted_git import (
    InterruptedGitPreparationRecovery,
)
from forge.persistence.repositories.subscription_recovery import RecoveryConflict
from sqlalchemy import JSON, func, select
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_review_selection import selection_case

ACTION = "acknowledge_interrupted_git_preparation"


async def interrupted_git_case(session_factory, tmp_path):
    snapshot = GitWorkingTreeSnapshot(
        head_sha="b" * 40, base_sha="a" * 40, files=(), changed_paths=()
    )
    factory, admission, _ = await selection_case(
        session_factory,
        tmp_path,
        tree_digest=snapshot.candidate_tree_digest,
        candidate_commit=snapshot.head_sha,
        plan_scope=("apps",),
    )
    identity = admission.attempt
    operation_id = uuid4()
    async with factory() as work:
        run = await work.runs.get(identity.run_id)
        scheduled = await work.session.get(SubscriptionScheduledTask, identity.task_id)
        attempt = await work.session.get(SubscriptionAttempt, identity.attempt_id)
        result = await work.session.get(SubscriptionAttemptResult, identity.attempt_id)
        result.disposition = "fenced"
        scheduled.lease_expires_at = datetime.now(UTC) - timedelta(minutes=1)
        worktree = ManagedWorktree(
            identity=WorktreeIdentity.for_run(run.project_id, run.id, run.branch_name, False),
            path=Path(run.worktree_path),
            base_sha=run.base_sha,
        )
        context = SubscriptionToolAuthorizationContext(
            run_id=run.id,
            task_id=identity.task_id,
            attempt_id=identity.attempt_id,
            worktree_id=scheduled.worktree_id,
            purpose=SpecialistPurpose.PRIMARY,
            policy_version=run.policy_version,
            permitted_tools=frozenset({ToolName.GIT_COMMIT}),
            invocation_id=operation_id,
        )
        message = "feat: retained candidate"
        digest = canonical_digest({"message": message})
        payload = _git_prepare_payload(context, worktree, message, digest, owned_paths=("apps",))
        work.session.add(
            OperationIntent(
                id=operation_id,
                run_id=run.id,
                operation_kind="git.commit.prepare.v1",
                idempotency_key=f"git.commit:{operation_id}:prepare",
                request_digest=canonical_digest(payload),
                request_payload=payload,
                status="PENDING",
                execution_owner="old-git-owner",
                execution_lease_expires_at=datetime.now(UTC) - timedelta(minutes=1),
            )
        )
        import hashlib

        work.session.add(
            ToolCall(
                id=operation_id,
                run_id=run.id,
                subscription_task_id=identity.task_id,
                subscription_attempt_id=identity.attempt_id,
                subscription_purpose="primary",
                tool_name=ToolName.GIT_COMMIT.value,
                authorized=True,
                status="RUNNING",
                started_at=datetime.now(UTC) - timedelta(minutes=2),
                result_metadata_schema_version=1,
                normalized_arguments={
                    "message_digest": hashlib.sha256(message.encode()).hexdigest()
                },
                result_metadata={
                    "operation_intent_id": str(operation_id),
                    "correlation_id": str(operation_id),
                    "policy_version": run.policy_version,
                    "request_digest": digest,
                    "resource_id": scheduled.worktree_id,
                    "invocation_schema_version": 1,
                },
            )
        )
        work.session.add(
            SubscriptionScheduledEffect(
                id=operation_id,
                run_id=run.id,
                task_id=identity.task_id,
                lease_owner=attempt.lease_owner,
                lease_generation=attempt.lease_generation,
                candidate_epoch=attempt.candidate_epoch,
                whole_worktree_exclusive=True,
                state="reconciling",
            )
        )
        work.session.add(
            SubscriptionOperationBinding(
                attempt_id=identity.attempt_id,
                provider_call_key="commit",
                durable_operation_id=operation_id,
                payload=encode_subscription_record(
                    ToolCallBinding(
                        attempt_id=identity.attempt_id,
                        provider_call_key="commit",
                        durable_operation_id=operation_id,
                        tool_name=ToolName.GIT_COMMIT,
                        arguments_digest=digest,
                    )
                ),
            )
        )
        await work.commit()
    return factory, admission, operation_id, snapshot


@pytest.mark.parametrize("blocker", ["pending_read", "publication"])
async def test_recovery_stops_database_reads_after_proving_a_conflict(
    session_factory, tmp_path, monkeypatch, blocker
):
    factory, admission, operation_id, _ = await interrupted_git_case(session_factory, tmp_path)
    identity = admission.attempt
    extra_id = uuid4()
    async with factory() as work:
        if blocker == "pending_read":
            work.session.add(
                SubscriptionOperationBinding(
                    attempt_id=identity.attempt_id,
                    provider_call_key="read",
                    durable_operation_id=extra_id,
                    payload=encode_subscription_record(
                        ToolCallBinding(
                            attempt_id=identity.attempt_id,
                            provider_call_key="read",
                            durable_operation_id=extra_id,
                            tool_name=ToolName.GIT_DIFF,
                            arguments_digest=canonical_digest({}),
                        )
                    ),
                )
            )
            work.session.add(
                ToolCall(
                    id=extra_id,
                    run_id=identity.run_id,
                    subscription_task_id=identity.task_id,
                    subscription_attempt_id=identity.attempt_id,
                    subscription_purpose="primary",
                    tool_name=ToolName.GIT_DIFF.value,
                    normalized_arguments={},
                    authorized=True,
                    status="SUCCEEDED",
                    started_at=datetime.now(UTC),
                    completed_at=datetime.now(UTC),
                )
            )
        else:
            work.session.add(
                OperationIntent(
                    id=extra_id,
                    run_id=identity.run_id,
                    operation_kind="git.commit.publish.v1",
                    idempotency_key=f"git.commit:{operation_id}:publish",
                    request_digest=canonical_digest({}),
                    request_payload={},
                    status="FAILED",
                    last_error="Uncertain publication",
                    completed_at=datetime.now(UTC),
                )
            )
        await work.commit()
    async with factory() as work:
        get, scalar = work.session.get, work.session.scalar
        conflict_seen = False
        later_reads = []

        async def counted_get(model, key, *args, **kwargs):
            nonlocal conflict_seen
            if conflict_seen:
                later_reads.append("get")
            result = await get(model, key, *args, **kwargs)
            if key == extra_id and result is not None:
                conflict_seen = True
            return result

        async def counted_scalar(statement, *args, **kwargs):
            nonlocal conflict_seen
            if conflict_seen:
                later_reads.append("scalar")
            result = await scalar(statement, *args, **kwargs)
            if result == extra_id:
                conflict_seen = True
            return result

        monkeypatch.setattr(work.session, "get", counted_get)
        monkeypatch.setattr(work.session, "scalar", counted_scalar)
        preview = await InterruptedGitPreparationRecovery(work.session).preview(
            identity.run_id, identity.task_id, identity.attempt_id
        )
        assert not preview.eligible and preview.reason_code == "other_work_unresolved"
        assert conflict_seen
        assert not later_reads


@pytest.mark.integration
@pytest.mark.parametrize("intervened", [False, True])
async def test_owner_acknowledgment_reuses_stopped_selection_without_new_provider_or_usage(
    session_factory, tmp_path, intervened
):
    factory, admission, operation_id, snapshot = await interrupted_git_case(
        session_factory, tmp_path
    )
    identity = admission.attempt
    if intervened:
        async with factory() as work:
            run = await work.runs.get(identity.run_id)
            await work.runs.intervene(
                run.id,
                run.version,
                "run.recovery_intervention",
                {"reason": "startup_outcome_unresolved", "operation_id": str(operation_id)},
            )
            await work.commit()
    actor = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())
    service = SubscriptionRecoveryService(factory)
    projection = await SubscriptionTaskQuery(session_factory).attempts(
        identity.run_id, identity.task_id
    )
    latest = next(
        item for item in projection["attempts"] if item["attempt_id"] == identity.attempt_id
    )
    assert latest["recovery"]["eligible_actions"] == [ACTION]
    SubscriptionAttemptPage.model_validate(projection)
    async with factory() as work:
        original = await work.session.get(SubscriptionAttemptResult, identity.attempt_id)
        original_payload, original_digest = original.result_payload, original.result_digest
        usage = await work.session.get(SubscriptionAttemptConsumption, identity.attempt_id)
        usage_payload = usage.telemetry_payload
        attempts = await work.session.scalar(select(func.count()).select_from(SubscriptionAttempt))
    preview = await service.preview(
        run_id=identity.run_id,
        task_id=identity.task_id,
        attempt_id=identity.attempt_id,
        actor=actor,
        request=RecoveryPreviewRequest(action=ACTION),
    )
    assert preview.eligible, preview.message
    assert preview.budget_impact.provider_attempts == preview.budget_impact.repair_units == 0
    request = RecoveryApplyRequest(action=ACTION, preview_token=preview.preview_token)
    receipt = await service.apply(
        run_id=identity.run_id,
        task_id=identity.task_id,
        attempt_id=identity.attempt_id,
        actor=actor,
        idempotency_key="ack",
        request=request,
    )
    assert receipt == await service.apply(
        run_id=identity.run_id,
        task_id=identity.task_id,
        attempt_id=identity.attempt_id,
        actor=actor,
        idempotency_key="ack",
        request=request,
    )
    async with factory() as work:
        result = await work.session.get(SubscriptionAttemptResult, identity.attempt_id)
        assert result.result_payload == original_payload and result.result_digest == original_digest
        assert result.disposition == "decision_pending" and not result.accepted
        assert (await work.runs.get(identity.run_id)).state.value == "IMPLEMENTING"
        assert (
            await work.session.get(SubscriptionAttemptConsumption, identity.attempt_id)
        ).telemetry_payload == usage_payload
        assert await work.session.get(SubscriptionRepairDebit, identity.attempt_id) is None
        operation = await work.session.get(OperationIntent, operation_id)
        assert operation.status == "FAILED" and operation.outcome_payload is None
        call = await work.session.get(ToolCall, operation_id)
        ack = call.result_metadata["recovery"]
        assert call.status == "CANCELLED" and ack["operation_outcome"] == "unknown"
        assert ack["owner_acknowledged_stopped"] and not ack["stop_confirmed"]
        assert (
            await work.session.get(SubscriptionScheduledEffect, operation_id)
        ).state == "rejected"
        callback = await work.session.scalar(
            select(SubscriptionOperationBinding).where(
                SubscriptionOperationBinding.durable_operation_id == operation_id
            )
        )
        assert callback.receipt_payload["operation_id"] == str(operation_id)
        assert not callback.receipt_payload["accepted"]

    # Startup sees a terminal owner receipt, not supervisor proof or a replayable effect.
    verifier = ToolRecoveryService(factory, None)
    assert await verifier.recover_all() == 0
    assert await verifier.verify_terminal_effect(operation_id) is None
    assert (
        await SubscriptionEffectRecovery(factory, terminal_verifier=verifier).reconcile_all() == 0
    )
    async with factory() as work:
        pending = await work.subscription_decisions.pending_applications(None, 100)
        assert identity.attempt_id in {item.attempt_id for item in pending}
    if intervened:
        async with factory() as work:
            next_operation_id = uuid4()
            work.session.add(
                OperationIntent(
                    id=next_operation_id,
                    run_id=identity.run_id,
                    operation_kind="repository.write_file",
                    idempotency_key="later-work",
                    request_digest=canonical_digest({}),
                    request_payload={},
                    status="PENDING",
                )
            )
            await work.session.flush()
            assert not await work.session.scalar(select(startup_intervention_hold(identity.run_id)))
            run = await work.runs.get(identity.run_id)
            await work.runs.intervene(
                run.id,
                run.version,
                "run.recovery_intervention",
                {"reason": "startup_outcome_unresolved", "operation_id": str(next_operation_id)},
            )
            assert await work.session.scalar(select(startup_intervention_hold(identity.run_id)))
            await work.rollback()

    async def capture(_proposal):
        return snapshot

    applied = await SubscriptionCandidateApplication(factory, capture).apply(identity.attempt_id)
    assert applied.accepted and applied.disposition == "review_selected"
    async with factory() as work:
        assert (
            await work.session.scalar(select(func.count()).select_from(SubscriptionAttempt))
            == attempts
        )
    with pytest.raises(RecoveryConflict):
        await service.apply(
            run_id=identity.run_id,
            task_id=identity.task_id,
            attempt_id=identity.attempt_id,
            actor=actor,
            idempotency_key="another",
            request=request,
        )


@pytest.mark.integration
@pytest.mark.parametrize(
    "change,code",
    [
        ("task_lease", "ownership_active"),
        ("operation_lease", "ownership_active"),
        ("missing_callback", "source_missing"),
        ("artifact", "source_changed"),
        ("fresh_worker", "ownership_active"),
        ("provider_stop", "source_unproved"),
        ("missing_usage", "source_unproved"),
        ("callback", "source_changed"),
        ("publication", "other_work_unresolved"),
        ("extra_effect", "effect_uncertain"),
        ("pause", "run_controlled"),
        ("cancel", "run_controlled"),
        ("cancelled_run", "run_controlled"),
        ("other_intervention", "run_controlled"),
        ("version", "source_changed"),
    ],
)
async def test_owner_recovery_refuses_changed_source_and_pending_controls(
    session_factory, tmp_path, change, code
):
    factory, admission, operation_id, _ = await interrupted_git_case(session_factory, tmp_path)
    identity = admission.attempt
    actor = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())
    service = SubscriptionRecoveryService(factory)
    args = {
        "run_id": identity.run_id,
        "task_id": identity.task_id,
        "attempt_id": identity.attempt_id,
        "actor": actor,
    }
    preview = await service.preview(**args, request=RecoveryPreviewRequest(action=ACTION))
    assert preview.eligible
    async with factory() as work:
        scheduled = await work.session.get(SubscriptionScheduledTask, identity.task_id)
        operation = await work.session.get(OperationIntent, operation_id)
        if change == "task_lease":
            scheduled.lease_expires_at = datetime.now(UTC) + timedelta(minutes=1)
        elif change == "operation_lease":
            operation.execution_lease_expires_at = datetime.now(UTC) + timedelta(minutes=1)
        elif change == "missing_callback":
            callback = await work.session.scalar(
                select(SubscriptionOperationBinding).where(
                    SubscriptionOperationBinding.durable_operation_id == operation_id
                )
            )
            await work.session.delete(callback)
        elif change == "artifact":
            call = await work.session.get(ToolCall, operation_id)
            call.result_metadata = {**call.result_metadata, "artifact_digests": ["a" * 64]}
        elif change == "fresh_worker":
            work.session.add(
                SubscriptionRecoveryWorker(
                    worker_id=scheduled.lease_owner,
                    contract_version=1,
                    observed_at=datetime.now(UTC),
                )
            )
        elif change == "provider_stop":
            launch = await work.session.scalar(
                select(SubscriptionClientLaunch).where(
                    SubscriptionClientLaunch.attempt_id == identity.attempt_id
                )
            )
            launch.state = "uncertain"
        elif change == "missing_usage":
            await work.session.delete(
                await work.session.get(SubscriptionAttemptConsumption, identity.attempt_id)
            )
        elif change == "callback":
            callback = await work.session.scalar(
                select(SubscriptionOperationBinding).where(
                    SubscriptionOperationBinding.durable_operation_id == operation_id
                )
            )
            callback.payload = encode_subscription_record(
                ToolCallBinding(
                    attempt_id=identity.attempt_id,
                    provider_call_key="different",
                    durable_operation_id=operation_id,
                    tool_name=ToolName.GIT_COMMIT,
                    arguments_digest=canonical_digest({"message": "different"}),
                )
            )
        elif change == "publication":
            work.session.add(
                OperationIntent(
                    id=uuid4(),
                    run_id=identity.run_id,
                    operation_kind="git.commit.publish.v1",
                    idempotency_key=f"git.commit:{operation_id}:publish",
                    request_digest=canonical_digest({}),
                    request_payload={},
                    status="FAILED",
                    last_error="Uncertain publication",
                    completed_at=datetime.now(UTC),
                )
            )
        elif change == "extra_effect":
            work.session.add(
                SubscriptionScheduledEffect(
                    id=uuid4(),
                    run_id=identity.run_id,
                    task_id=identity.task_id,
                    lease_owner=scheduled.lease_owner,
                    lease_generation=scheduled.lease_generation,
                    candidate_epoch=admission.candidate_epoch,
                    whole_worktree_exclusive=True,
                    state="reconciling",
                )
            )
        elif change == "pause":
            scheduled.pause_requested = True
        elif change == "cancel":
            (await work.session.get(SubscriptionTask, identity.task_id)).cancel_requested = True
        elif change == "cancelled_run":
            (await work.session.get(Run, identity.run_id)).state = "CANCELLED"
        elif change == "other_intervention":
            run = await work.runs.get(identity.run_id)
            await work.runs.intervene(
                run.id, run.version, "run.unrelated_intervention", {"reason": "owner_attention"}
            )
        elif change == "version":
            (await work.session.get(SubscriptionTask, identity.task_id)).version += 1
        await work.commit()
    current = await service.preview(**args, request=RecoveryPreviewRequest(action=ACTION))
    assert not current.eligible and current.reason_code == code
    with pytest.raises(RecoveryConflict):
        await service.apply(
            **args,
            idempotency_key="stale",
            request=RecoveryApplyRequest(action=ACTION, preview_token=preview.preview_token),
        )
    async with factory() as work:
        assert (
            await work.session.get(SubscriptionAttemptResult, identity.attempt_id)
        ).disposition == "fenced"
        assert (
            await work.session.get(SubscriptionScheduledEffect, operation_id)
        ).state == "reconciling"


@pytest.mark.integration
async def test_owner_action_binds_actor_snapshot_and_still_requires_exact_candidate(
    session_factory, tmp_path
):
    from dataclasses import replace

    factory, admission, _, snapshot = await interrupted_git_case(session_factory, tmp_path)
    identity = admission.attempt
    actor = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())
    service = SubscriptionRecoveryService(factory)
    args = {
        "run_id": identity.run_id,
        "task_id": identity.task_id,
        "attempt_id": identity.attempt_id,
    }
    preview = await service.preview(
        **args, actor=actor, request=RecoveryPreviewRequest(action=ACTION)
    )
    request = RecoveryApplyRequest(action=ACTION, preview_token=preview.preview_token)
    other = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())
    with pytest.raises(RecoveryConflict):
        await service.apply(**args, actor=other, idempotency_key="actor", request=request)
    async with factory() as work:
        (await work.session.get(Run, identity.run_id)).version += 1
        await work.commit()
    with pytest.raises(RecoveryConflict):
        await service.apply(**args, actor=actor, idempotency_key="version", request=request)
    preview = await service.preview(
        **args, actor=actor, request=RecoveryPreviewRequest(action=ACTION)
    )
    await service.apply(
        **args,
        actor=actor,
        idempotency_key="fresh",
        request=RecoveryApplyRequest(action=ACTION, preview_token=preview.preview_token),
    )

    async def capture(_proposal):
        return replace(snapshot, head_sha="c" * 40)

    outcome = await SubscriptionCandidateApplication(factory, capture).apply(identity.attempt_id)
    assert not outcome.accepted


@pytest.mark.integration
@pytest.mark.parametrize(
    "tool,wrong_source,json_null",
    [
        (ToolName.GIT_DIFF, False, False),
        (ToolName.GIT_DIFF, False, True),
        (ToolName.REPOSITORY_WRITE_FILE, False, False),
        (ToolName.REPOSITORY_WRITE_FILE, False, True),
        (ToolName.GIT_DIFF, True, False),
    ],
)
async def test_unstarted_read_reservation_is_retained_without_replay_or_claimed_result(
    session_factory, tmp_path, tool, wrong_source, json_null
):
    factory, admission, _, snapshot = await interrupted_git_case(session_factory, tmp_path)
    identity = admission.attempt
    operation_id = uuid4()
    async with factory() as work:
        work.session.add(
            SubscriptionOperationBinding(
                attempt_id=identity.attempt_id,
                provider_call_key="unstarted",
                durable_operation_id=operation_id,
                payload=encode_subscription_record(
                    ToolCallBinding(
                        attempt_id=uuid4() if wrong_source else identity.attempt_id,
                        provider_call_key="unstarted",
                        durable_operation_id=operation_id,
                        tool_name=tool,
                        arguments_digest=canonical_digest({}),
                    )
                ),
                **({"receipt_payload": JSON.NULL} if json_null else {}),
            )
        )
        await work.commit()
    async with factory() as work:
        stored_null = (
            await work.session.execute(
                select(
                    SubscriptionOperationBinding.receipt_payload.is_(None),
                    func.jsonb_typeof(SubscriptionOperationBinding.receipt_payload),
                ).where(SubscriptionOperationBinding.durable_operation_id == operation_id)
            )
        ).one()
        assert tuple(stored_null) == ((False, "null") if json_null else (True, None))
    actor = AuthenticatedActor(actor_id=uuid4(), actor_class="operator", session_id=uuid4())
    service = SubscriptionRecoveryService(factory)
    args = {
        "run_id": identity.run_id,
        "task_id": identity.task_id,
        "attempt_id": identity.attempt_id,
        "actor": actor,
    }
    preview = await service.preview(**args, request=RecoveryPreviewRequest(action=ACTION))
    if tool is not ToolName.GIT_DIFF or wrong_source:
        assert not preview.eligible and preview.reason_code == "other_work_unresolved"
        return
    assert preview.eligible, preview.message
    await service.apply(
        **args,
        idempotency_key="ack-with-read",
        request=RecoveryApplyRequest(action=ACTION, preview_token=preview.preview_token),
    )
    async with factory() as work:
        callback = await work.session.scalar(
            select(SubscriptionOperationBinding).where(
                SubscriptionOperationBinding.durable_operation_id == operation_id
            )
        )
        assert callback.receipt_payload is None
        assert await work.session.get(ToolCall, operation_id) is None
        assert await work.session.get(OperationIntent, operation_id) is None
        assert await work.session.get(SubscriptionScheduledEffect, operation_id) is None

    async def capture(_proposal):
        return snapshot

    applied = await SubscriptionCandidateApplication(factory, capture).apply(identity.attempt_id)
    assert applied.accepted and applied.disposition == "review_selected"
