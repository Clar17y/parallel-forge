"""PostgreSQL recovery of interrupted subscription broker finalization."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from forge.application.ports.tools import ToolCallRecord
from forge.application.services.subscription_broker import (
    ControlledSubscriptionEffect,
    SubscriptionToolBroker,
)
from forge.application.services.subscription_effect_recovery import SubscriptionEffectRecovery
from forge.domain.operation import canonical_digest
from forge.domain.subscription import (
    AttemptIdentity,
    BrokerAuthorizationBinding,
    RouteBinding,
    SpecialistPurpose,
    ToolCallBinding,
)
from forge.domain.tool import SubscriptionToolAuthorizationContext, ToolCallStatus, ToolName
from forge.persistence.models.execution import AgentExecution, OperationIntent, ToolCall
from forge.persistence.models.scheduling import (
    SubscriptionScheduledEffect,
    SubscriptionScheduledTask,
)
from forge.persistence.models.subscription import (
    SubscriptionAttempt,
    SubscriptionOperationBinding,
    SubscriptionTask,
)
from forge.persistence.unit_of_work import PostgresUnitOfWork
from sqlalchemy import delete, null, select, update
from test_scheduler_acceptance import (
    _admit_run,
    _enqueue,
    _remove_disposable_subscription_rows,  # noqa: F401
    _route,
)
from test_tool_write_invocation import (
    _ControlledArtifactStore,
    _ControlledWriter,
    _seed_test_database,
    _setup_service,
)


async def _interrupted_effect(
    session_factory,
    run,
    *,
    terminal=True,
    operation=True,
    operation_status="SUCCEEDED",
    operation_kind=ToolName.REPOSITORY_WRITE_FILE,
    terminal_status=ToolCallStatus.SUCCEEDED,
    authorized=True,
    foreign=False,
    active=False,
    effect_state="reconciling",
):
    attempt_id, effect_id = uuid4(), uuid4()
    args = {"path": "apps/one.py", "content": "x"}
    async with PostgresUnitOfWork(session_factory) as work:
        parent = await _admit_run(work, run, (_route("p"), _route("p")))
        task_id = await _enqueue(
            work, run.id, provider="p", worktree="tree", parent_id=parent, paths=("apps",)
        )
        await work.subscription.create_attempt(
            AttemptIdentity(run_id=run.id, task_id=task_id, attempt_id=attempt_id),
            route_payload=RouteBinding(requested=_route("p"), effective=_route("p")),
            idempotency_key="attempt",
        )
        await work.session.execute(update(SubscriptionTask).where(SubscriptionTask.id == task_id).values(state="running"))
        await work.session.execute(update(SubscriptionAttempt).where(SubscriptionAttempt.id == attempt_id).values(status="running"))
        await work.commit()
    async with PostgresUnitOfWork(session_factory) as work:
        lease = await work.scheduler.claim_ready("owner", timedelta(seconds=30))
        assert lease is not None
        binding = ToolCallBinding(
            attempt_id=attempt_id,
            provider_call_key="provider-call",
            durable_operation_id=effect_id,
            tool_name=ToolName.REPOSITORY_WRITE_FILE,
            arguments_digest=canonical_digest(args),
        )
        await work.subscription.bind_operation(binding, run_id=run.id, task_id=task_id)
        effect = await work.scheduler.admit_effect(lease, effect_id, owned_paths=("apps/one.py",))
        if effect_state == "reconciling":
            await work.scheduler.reconcile_effect(effect)
        if terminal:
            now = datetime.now(UTC)
            request_digest = canonical_digest(args)
            if operation:
                work.session.add(
                    OperationIntent(
                    id=effect_id, run_id=run.id, operation_kind=operation_kind.value,
                    idempotency_key=f"tool:{effect_id}", request_digest=canonical_digest({
                        "authority_schema_version": 2, "subscription_task_id": str(task_id),
                        "subscription_attempt_id": str(attempt_id),
                        "subscription_purpose": SpecialistPurpose.ROUTINE_IMPLEMENTATION.value,
                        "run_id": str(run.id), "policy_version": 1, "request_digest": request_digest,
                        "worktree_id": "tree", "path": "apps/one.py", "content_digest": "a" * 64,
                        "content_byte_count": 1, "project_id": str(run.project_id),
                    }), request_schema_version=1, request_payload={
                        "authority_schema_version": 2, "subscription_task_id": str(task_id),
                        "subscription_attempt_id": str(attempt_id),
                        "subscription_purpose": SpecialistPurpose.ROUTINE_IMPLEMENTATION.value,
                        "run_id": str(run.id), "policy_version": 1, "request_digest": request_digest,
                        "worktree_id": "tree", "path": "apps/one.py", "content_digest": "a" * 64,
                        "content_byte_count": 1, "project_id": str(run.project_id),
                    }, status="SUCCEEDED", outcome_schema_version=1,
                    outcome_payload={}, completed_at=now,
                    )
                )
            call = ToolCallRecord(
                id=effect_id,
                run_id=run.id,
                agent_execution_id=None,
                subscription_task_id=task_id if not foreign else parent,
                subscription_attempt_id=attempt_id,
                subscription_purpose=SpecialistPurpose.ROUTINE_IMPLEMENTATION.value,
                tool_name=ToolName.REPOSITORY_WRITE_FILE,
                normalized_arguments=args,
                authorized=authorized,
                status=terminal_status,
                started_at=now,
                completed_at=now,
                result_metadata={
                    "result_status": terminal_status.value, "authorized": authorized,
                    "started_at": now.isoformat(), "completed_at": now.isoformat(),
                    "operation_intent_id": str(effect_id) if operation else None,
                    "correlation_id": str(effect_id),
                    "policy_version": 1, "duration_ms": 0, "artifact_digests": [],
                    "request_digest": request_digest, "resource_id": "tree",
                    "invocation_schema_version": 1,
                    **(
                        {}
                        if terminal_status is ToolCallStatus.SUCCEEDED
                        else {"error": {"code": "authorization_denied", "message": "denied"}}
                    ),
                },
                policy_version=1, duration_ms=0, correlation_id=effect_id,
                operation_intent_id=effect_id if operation else None,
                result_metadata_schema_version=1,
                request_digest=request_digest, resource_id="tree", invocation_schema_version=1,
            )
            await work.tool_calls.record(call)
            if operation and operation_status != "SUCCEEDED":
                await work.session.execute(
                    update(OperationIntent)
                    .where(OperationIntent.id == effect_id)
                    .values(
                        status=operation_status,
                        outcome_schema_version=null(),
                        outcome_payload=null(),
                        completed_at=null(),
                    )
                )
        if not active:
            await work.session.execute(
                update(SubscriptionScheduledTask).where(SubscriptionScheduledTask.task_id == task_id)
                .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
        await work.commit()
    return effect_id, attempt_id


@pytest.mark.integration
async def test_noninvoked_terminal_mutation_denial_can_close_stale_effect(
    session_factory, persisted_run
):
    effect_id, _ = await _interrupted_effect(
        session_factory,
        persisted_run,
        operation=False,
        terminal_status=ToolCallStatus.DENIED,
        authorized=False,
        effect_state="admitted",
    )
    recovery = SubscriptionEffectRecovery(lambda: PostgresUnitOfWork(session_factory))
    assert await recovery.reconcile_all() == 1
    async with session_factory() as session:
        effect = await session.get(SubscriptionScheduledEffect, effect_id)
        binding = await session.scalar(
            select(SubscriptionOperationBinding).where(
                SubscriptionOperationBinding.durable_operation_id == effect_id
            )
        )
        assert effect is not None and effect.state == "rejected"
        assert binding is not None and binding.receipt_payload is not None
        assert binding.receipt_payload["result"]["operation_intent_id"] is None


@pytest.mark.integration
async def test_actual_predispatch_mutation_denial_recovers_without_writer_execution(
    session_factory, tmp_path
):
    project_id, run_id, _, _, base_sha, branch, repo, tree, _ = await _seed_test_database(
        session_factory, tmp_path
    )
    writer = _ControlledWriter(tree)
    service, _, worktree = _setup_service(
        session_factory,
        project_id,
        run_id,
        branch,
        base_sha,
        repo,
        tree,
        writer,
        _ControlledArtifactStore(tmp_path / "artifacts"),
    )
    attempt_id = uuid4()
    async with PostgresUnitOfWork(session_factory) as work:
        run = await work.runs.get(run_id)
        parent = await _admit_run(work, run, (_route("p"), _route("p")))
        task_id = await _enqueue(
            work,
            run_id,
            provider="p",
            worktree=worktree.identity.worktree_name,
            parent_id=parent,
            paths=("apps",),
        )
        await work.subscription.create_attempt(
            AttemptIdentity(run_id=run_id, task_id=task_id, attempt_id=attempt_id),
            route_payload=RouteBinding(requested=_route("p"), effective=_route("p")),
            idempotency_key="denied-attempt",
        )
        await work.session.execute(
            update(SubscriptionTask).where(SubscriptionTask.id == task_id).values(state="running")
        )
        await work.session.execute(
            update(SubscriptionAttempt)
            .where(SubscriptionAttempt.id == attempt_id)
            .values(status="running")
        )
        lease = await work.scheduler.claim_ready("owner", timedelta(seconds=30))
        await work.commit()
    assert lease is not None
    context = SubscriptionToolAuthorizationContext(
        run_id=run_id,
        task_id=task_id,
        attempt_id=attempt_id,
        worktree_id=worktree.identity.worktree_name,
        purpose=SpecialistPurpose.ROUTINE_IMPLEMENTATION,
        policy_version=1,
        permitted_tools=frozenset(),
    )
    broker = SubscriptionToolBroker(
        lambda: PostgresUnitOfWork(session_factory),
        lease=lease,
        authority=BrokerAuthorizationBinding(
            run_id=run_id,
            task_id=task_id,
            attempt_id=attempt_id,
            worktree_id=context.worktree_id,
            role=context.purpose,
            policy_version=1,
            permitted_tools=frozenset({ToolName.REPOSITORY_WRITE_FILE}),
            broker_token="test-only",
        ),
        effect=ControlledSubscriptionEffect(service, context),
    )
    receipt = await broker.invoke(
        token="test-only",
        provider_call_key="denied",
        tool_name=ToolName.REPOSITORY_WRITE_FILE,
        arguments={"path": "apps/denied.py", "content": "must not write"},
    )
    assert receipt.accepted is False and writer.call_count == 0
    effect_id = receipt.operation_id
    async with session_factory() as session, session.begin():
        await session.execute(
            update(SubscriptionScheduledEffect)
            .where(SubscriptionScheduledEffect.id == effect_id)
            .values(state="admitted")
        )
        await session.execute(
            update(SubscriptionOperationBinding)
            .where(SubscriptionOperationBinding.durable_operation_id == effect_id)
            .values(receipt_payload=None)
        )
        await session.execute(
            update(SubscriptionScheduledTask)
            .where(SubscriptionScheduledTask.task_id == task_id)
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    assert await SubscriptionEffectRecovery(
        lambda: PostgresUnitOfWork(session_factory)
    ).reconcile_all() == 1
    assert writer.call_count == 0


@pytest.mark.integration
async def test_recovery_keeps_unproved_synthetic_mutation_fenced(session_factory, persisted_run):
    effect_id, _ = await _interrupted_effect(session_factory, persisted_run)
    recovery = SubscriptionEffectRecovery(lambda: PostgresUnitOfWork(session_factory))
    assert await recovery.reconcile_all() == 0
    assert await recovery.reconcile_all() == 0
    async with session_factory() as session:
        effect = await session.get(SubscriptionScheduledEffect, effect_id)
        binding = await session.scalar(select(SubscriptionOperationBinding).where(SubscriptionOperationBinding.durable_operation_id == effect_id))
        assert effect is not None and effect.state == "reconciling"
        assert binding is not None and binding.receipt_payload is None


@pytest.mark.integration
@pytest.mark.parametrize(
    "terminal,operation,foreign,active",
    [(False, True, False, False), (True, False, False, False), (True, True, True, False), (True, True, False, True)],
)
async def test_recovery_keeps_missing_foreign_or_active_effect_fenced(
    session_factory, persisted_run, terminal, operation, foreign, active
):
    effect_id, _ = await _interrupted_effect(
        session_factory,
        persisted_run,
        terminal=terminal,
        operation=operation,
        foreign=foreign,
        active=active,
    )
    assert await SubscriptionEffectRecovery(lambda: PostgresUnitOfWork(session_factory)).reconcile_all() == 0
    async with session_factory() as session:
        effect = await session.get(SubscriptionScheduledEffect, effect_id)
        binding = await session.scalar(select(SubscriptionOperationBinding).where(SubscriptionOperationBinding.durable_operation_id == effect_id))
        assert effect is not None and effect.state == "reconciling"
        assert binding is not None and binding.receipt_payload is None


@pytest.mark.integration
@pytest.mark.parametrize(
    ("operation_status", "operation_kind"),
    [
        ("PENDING", ToolName.REPOSITORY_WRITE_FILE),
        ("SUCCEEDED", ToolName.REPOSITORY_DELETE_FILE),
    ],
)
async def test_recovery_keeps_nonterminal_or_foreign_operation_intent_fenced(
    session_factory, persisted_run, operation_status, operation_kind
):
    effect_id, _ = await _interrupted_effect(
        session_factory,
        persisted_run,
        operation_status=operation_status,
        operation_kind=operation_kind,
    )
    recovery = SubscriptionEffectRecovery(lambda: PostgresUnitOfWork(session_factory))
    assert await recovery.reconcile_all() == 0
    async with session_factory() as session:
        effect = await session.get(SubscriptionScheduledEffect, effect_id)
        binding = await session.scalar(
            select(SubscriptionOperationBinding).where(
                SubscriptionOperationBinding.durable_operation_id == effect_id
            )
        )
        assert effect is not None and effect.state == "reconciling"
        assert binding is not None and binding.receipt_payload is None


@pytest.mark.integration
@pytest.mark.parametrize(
    ("effect_state", "active", "expected_count", "expected_state"),
    [("admitted", True, 0, "admitted")],
)
async def test_recovery_distinguishes_live_and_stale_effect_states(
    session_factory, persisted_run, effect_state, active, expected_count, expected_state
):
    effect_id, _ = await _interrupted_effect(
        session_factory, persisted_run, effect_state=effect_state, active=active
    )
    assert (
        await SubscriptionEffectRecovery(
            lambda: PostgresUnitOfWork(session_factory)
        ).reconcile_all()
        == expected_count
    )
    async with session_factory() as session:
        effect = await session.get(SubscriptionScheduledEffect, effect_id)
        assert effect is not None and effect.state == expected_state


@pytest.mark.integration
async def test_recovery_excludes_legacy_tool_lineage(session_factory, persisted_run):
    effect_id, _ = await _interrupted_effect(session_factory, persisted_run)
    async with session_factory() as session, session.begin():
        await session.execute(delete(ToolCall).where(ToolCall.id == effect_id))
        execution = AgentExecution(
            id=uuid4(), run_id=persisted_run.id, step_id=None, role="developer",
            instruction_version="legacy", provider="p", model="m", status="SUCCEEDED",
        )
        session.add(execution)
        session.add(
            ToolCall(
                id=effect_id, run_id=persisted_run.id, agent_execution_id=execution.id,
                subscription_task_id=None, subscription_attempt_id=None, subscription_purpose=None,
                tool_name=ToolName.REPOSITORY_WRITE_FILE.value, arguments_schema_version=1,
                normalized_arguments={"path": "apps/one.py", "content": "x"}, authorized=True,
                status="SUCCEEDED", result_metadata_schema_version=1,
                result_metadata={"result_status": "succeeded"},
                started_at=datetime.now(UTC), completed_at=datetime.now(UTC),
            )
        )
    assert await SubscriptionEffectRecovery(lambda: PostgresUnitOfWork(session_factory)).reconcile_all() == 0
    async with session_factory() as session:
        effect = await session.get(SubscriptionScheduledEffect, effect_id)
        assert effect is not None and effect.state == "reconciling"


@pytest.mark.integration
@pytest.mark.parametrize(
    ("tool_name", "effect_state", "fail_commit"),
    [
        (ToolName.REPOSITORY_READ_FILE, "admitted", False),
        (ToolName.REPOSITORY_READ_FILE, "reconciling", False),
        (ToolName.REPOSITORY_WRITE_FILE, "admitted", False),
        (ToolName.REPOSITORY_WRITE_FILE, "reconciling", False),
        (ToolName.REPOSITORY_READ_FILE, "admitted", True),
    ],
)
async def test_actual_controlled_terminal_evidence_recovers_lost_broker_receipt(
    session_factory, tmp_path, monkeypatch, tool_name, effect_state, fail_commit
):
    import test_subscription_authority_persistence as actual

    seeded = []
    seed = actual._seed_test_database

    async def capture_seed(*args, **kwargs):
        result = await seed(*args, **kwargs)
        seeded.append(result)
        return result

    monkeypatch.setattr(actual, "_seed_test_database", capture_seed)
    await actual.test_subscription_effect_uses_actual_lineage_and_recovers_without_legacy_execution(
        session_factory, tmp_path, monkeypatch, tool_name, False
    )
    run_id = seeded[0][1]
    async with session_factory() as session, session.begin():
        call = await session.scalar(select(ToolCall).where(ToolCall.run_id == run_id))
        assert call is not None
        operation_binding = await session.scalar(
            select(SubscriptionOperationBinding).where(
                SubscriptionOperationBinding.attempt_id == call.subscription_attempt_id
            )
        )
        assert operation_binding is not None
        effect_id = operation_binding.durable_operation_id
        if tool_name is ToolName.REPOSITORY_WRITE_FILE:
            operation_id = call.result_metadata["operation_intent_id"]
            assert operation_id == str(effect_id) and call.id == effect_id
            assert await session.get(OperationIntent, operation_id) is not None
        await session.execute(update(SubscriptionScheduledEffect).where(
            SubscriptionScheduledEffect.id == effect_id
        ).values(state=effect_state))
        await session.execute(update(SubscriptionOperationBinding).where(
            SubscriptionOperationBinding.durable_operation_id == effect_id
        ).values(receipt_payload=None))
        await session.execute(update(SubscriptionScheduledTask).where(
            SubscriptionScheduledTask.task_id == call.subscription_task_id
        ).values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1)))
    failed = False

    class FailOnceUnitOfWork(PostgresUnitOfWork):
        async def commit(self):
            nonlocal failed
            if not failed:
                failed = True
                await self.rollback()
                raise RuntimeError("injected receipt commit failure")
            await super().commit()

    recovery = SubscriptionEffectRecovery(
        lambda: (FailOnceUnitOfWork(session_factory) if fail_commit else PostgresUnitOfWork(session_factory))
    )
    if fail_commit:
        with pytest.raises(RuntimeError, match="receipt commit failure"):
            await recovery.reconcile_all()
        async with session_factory() as session:
            rolled_back_effect = await session.get(SubscriptionScheduledEffect, effect_id)
            rolled_back_binding = await session.scalar(
                select(SubscriptionOperationBinding).where(
                    SubscriptionOperationBinding.durable_operation_id == effect_id
                )
            )
            assert rolled_back_effect is not None and rolled_back_effect.state == effect_state
            assert rolled_back_binding is not None and rolled_back_binding.receipt_payload is None
    assert await recovery.reconcile_all() == 1
    assert await recovery.reconcile_all() == 0
    async with session_factory() as session:
        effect = await session.get(SubscriptionScheduledEffect, effect_id)
        assert effect.state == "rejected"
        call = await session.get(ToolCall, effect_id)
        assert call.status == "SUCCEEDED"
        binding = await session.scalar(
            select(SubscriptionOperationBinding).where(
                SubscriptionOperationBinding.durable_operation_id == effect_id
            )
        )
        assert binding is not None and binding.receipt_payload is not None
        if tool_name is ToolName.REPOSITORY_READ_FILE:
            assert binding.receipt_payload["result"]["operation_intent_id"] is None
