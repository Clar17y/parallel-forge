"""PostgreSQL persistence contracts for the v0.2 subscription runtime."""

import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine


@pytest.mark.integration
async def test_subscription_repository_exposes_versioned_profile_and_frozen_envelope(
    session_factory, persisted_run
) -> None:
    from forge.domain.subscription import (
        AttemptIdentity,
        AuthMode,
        BillingMode,
        ExecutionEnvelope,
        HandoffStatus,
        LogicalTaskContract,
        OperatorProfile,
        ReasoningEffort,
        RolePreference,
        RouteBinding,
        RouteSpec,
        SpecialistPurpose,
        TaskBudget,
        TaskHandoff,
    )
    from forge.persistence.repositories.subscription import SubscriptionConflict
    from forge.persistence.unit_of_work import PostgresUnitOfWork

    profile_id = uuid4()
    route = RouteSpec(
        provider="openai",
        client="codex",
        model="astra",
        effort=ReasoningEffort.LOW,
        auth_mode=AuthMode.SUBSCRIPTION,
        billing_mode=BillingMode.ALLOWANCE_ONLY,
    )
    profile = OperatorProfile(
        profile_id=profile_id,
        version=1,
        preferences=(RolePreference(purpose=SpecialistPurpose.PRIMARY, preferred_route=route),),
    )
    envelope = ExecutionEnvelope(
        run_id=persisted_run.id,
        profile_id=profile_id,
        profile_version=1,
        safety_policy_version=1,
        routes=((SpecialistPurpose.PRIMARY, RouteBinding(requested=route, effective=route)),),
    )
    async with PostgresUnitOfWork(session_factory) as work:
        assert await work.subscription.store_profile(profile) == profile
        await work.subscription.select_project_profile(persisted_run.project_id, profile)
        assert await work.subscription.freeze_envelope(envelope) == envelope
        assert await work.subscription.envelope_for_run(persisted_run.id) == envelope
        with pytest.raises(SubscriptionConflict):
            await work.subscription.store_profile(replace(profile, preferences=()))
        with pytest.raises(SubscriptionConflict):
            await work.subscription.freeze_envelope(replace(envelope, safety_policy_version=2))
        contract = LogicalTaskContract(
            run_id=persisted_run.id,
            task_id=uuid4(),
            purpose=SpecialistPurpose.PRIMARY,
            route=RouteBinding(requested=route, effective=route),
            budget=TaskBudget(max_tool_calls=2),
        )
        assert await work.subscription.create_task(contract, idempotency_key="primary") == contract
        assert await work.subscription.create_task(contract, idempotency_key="primary") == contract
        with pytest.raises(SubscriptionConflict):
            await work.subscription.create_task(
                replace(contract, max_repairs=1), idempotency_key="primary"
            )
        await work.subscription.initialize_budget(
            persisted_run.id, contract.task_id, TaskBudget(max_tool_calls=2)
        )
        await work.subscription.initialize_budget(
            persisted_run.id, None, TaskBudget(max_tool_calls=2)
        )
        assert (
            await work.subscription.reserve_budget(
                persisted_run.id,
                contract.task_id,
                TaskBudget(max_tool_calls=1),
                reservation_id=uuid4(),
                idempotency_key="reservation-1",
            )
        ).reserved_tool_calls == 1
        with pytest.raises(SubscriptionConflict):
            await work.subscription.reserve_budget(
                persisted_run.id,
                contract.task_id,
                TaskBudget(max_tool_calls=2),
                reservation_id=uuid4(),
                idempotency_key="reservation-2",
            )
        with pytest.raises(SubscriptionConflict):
            await work.subscription.reserve_budget(
                persisted_run.id,
                uuid4(),
                TaskBudget(max_tool_calls=1),
                reservation_id=uuid4(),
                idempotency_key="foreign-task",
            )
        attempt = AttemptIdentity(
            run_id=persisted_run.id, task_id=contract.task_id, attempt_id=uuid4()
        )
        await work.subscription.create_attempt(
            attempt,
            route_payload=RouteBinding(requested=route, effective=route),
            idempotency_key="attempt",
        )
        handoff = TaskHandoff(
            run_id=persisted_run.id,
            task_id=contract.task_id,
            attempt_id=attempt.attempt_id,
            status=HandoffStatus.BLOCKED,
            summary="blocked",
        )
        assert (
            await work.subscription.record_decision(handoff, idempotency_key="handoff") == handoff
        )
        with pytest.raises(SubscriptionConflict):
            await work.subscription.record_decision(
                replace(handoff, summary="other"), idempotency_key="handoff"
            )


@pytest.mark.integration
async def test_budget_reservation_is_atomic_across_two_postgres_sessions(
    session_factory, persisted_run
) -> None:
    """Two contenders cannot reserve the same remaining task budget."""
    from forge.domain.subscription import (
        AuthMode,
        BillingMode,
        LogicalTaskContract,
        ReasoningEffort,
        RouteBinding,
        RouteSpec,
        SpecialistPurpose,
        TaskBudget,
    )
    from forge.persistence.repositories.subscription import (
        PostgresSubscriptionRepository,
        SubscriptionConflict,
    )
    from forge.persistence.unit_of_work import PostgresUnitOfWork

    route = RouteSpec(
        provider="openai",
        client="codex",
        model="astra",
        effort=ReasoningEffort.LOW,
        auth_mode=AuthMode.SUBSCRIPTION,
        billing_mode=BillingMode.ALLOWANCE_ONLY,
    )
    task = LogicalTaskContract(
        run_id=persisted_run.id,
        task_id=uuid4(),
        purpose=SpecialistPurpose.PRIMARY,
        route=RouteBinding(requested=route, effective=route),
        budget=TaskBudget(max_tool_calls=1),
    )
    async with PostgresUnitOfWork(session_factory) as work:
        await work.subscription.create_task(task, idempotency_key="primary")
        await work.subscription.initialize_budget(
            persisted_run.id, task.task_id, TaskBudget(max_tool_calls=1)
        )
        await work.subscription.initialize_budget(
            persisted_run.id, None, TaskBudget(max_tool_calls=1)
        )
        await work.commit()
    entered = asyncio.Event()

    async def contender(index: int) -> bool:
        async with session_factory() as session:
            repo = PostgresSubscriptionRepository(session)
            entered.set()
            await entered.wait()
            try:
                await repo.reserve_budget(
                    persisted_run.id,
                    task.task_id,
                    TaskBudget(max_tool_calls=1),
                    reservation_id=uuid4(),
                    idempotency_key=f"contender-{index}",
                )
            except SubscriptionConflict:
                await session.rollback()
                return False
            await session.commit()
            return True

    results = await asyncio.gather(contender(1), contender(2))
    assert results.count(True) == 1
    async with session_factory() as session:
        repo = PostgresSubscriptionRepository(session)
        with pytest.raises(SubscriptionConflict):
            await repo.reserve_budget(
                persisted_run.id,
                task.task_id,
                TaskBudget(max_tool_calls=1),
                reservation_id=uuid4(),
                idempotency_key="exhausted",
            )
        await session.execute(
            text("TRUNCATE subscription_budget_pools, subscription_tasks CASCADE")
        )
        await session.commit()


def _route():
    from forge.domain.subscription import AuthMode, BillingMode, ReasoningEffort, RouteSpec

    return RouteSpec(
        provider="openai",
        client="codex",
        model="astra",
        effort=ReasoningEffort.LOW,
        auth_mode=AuthMode.SUBSCRIPTION,
        billing_mode=BillingMode.ALLOWANCE_ONLY,
    )


def _binding():
    from forge.domain.subscription import RouteBinding

    route = _route()
    return RouteBinding(requested=route, effective=route)


def _task(run_id, *, task_id=None, tool_calls=2, parent_task_id=None, purpose=None):
    from forge.domain.subscription import (
        LogicalTaskContract,
        RouteBinding,
        SpecialistPurpose,
        TaskBudget,
    )

    route = _route()
    return LogicalTaskContract(
        run_id=run_id,
        task_id=task_id or uuid4(),
        purpose=purpose or SpecialistPurpose.PRIMARY,
        route=RouteBinding(requested=route, effective=route),
        budget=TaskBudget(max_tool_calls=tool_calls),
        parent_task_id=parent_task_id,
    )


@pytest.mark.integration
async def test_cross_run_task_attempt_operation_and_handoff_lineage_is_rejected(
    session_factory, persisted_run
) -> None:
    """Every child identity must remain bound to one run and one task."""
    from forge.domain.subscription import (
        AttemptIdentity,
        HandoffStatus,
        TaskHandoff,
        ToolCallBinding,
    )
    from forge.domain.tool import ToolName
    from forge.persistence.repositories.subscription import SubscriptionConflict
    from forge.persistence.unit_of_work import PostgresUnitOfWork

    second_run = replace(persisted_run, id=uuid4())
    first_task = _task(persisted_run.id)
    second_task = _task(second_run.id)
    attempt = AttemptIdentity(
        run_id=persisted_run.id, task_id=first_task.task_id, attempt_id=uuid4()
    )
    async with PostgresUnitOfWork(session_factory) as work:
        await work.runs.create(second_run)
        await work.subscription.create_task(first_task, idempotency_key="first")
        with pytest.raises(SubscriptionConflict, match="foreign task lineage"):
            await work.subscription.create_task(
                replace(second_task, dependency_task_ids=(first_task.task_id,)),
                idempotency_key="cross-run-task",
            )
        await work.subscription.create_task(second_task, idempotency_key="second")
        await work.subscription.create_attempt(
            attempt, route_payload=_binding(), idempotency_key="attempt"
        )

        with pytest.raises(SubscriptionConflict, match="foreign task lineage"):
            await work.subscription.create_attempt(
                replace(attempt, run_id=second_run.id, attempt_id=uuid4()),
                route_payload=_binding(),
                idempotency_key="cross-run-attempt",
            )
        # A binding presented for another run/task cannot borrow an existing attempt.
        cross_run_binding = ToolCallBinding(
            attempt_id=attempt.attempt_id,
            provider_call_key="cross-run-call",
            durable_operation_id=uuid4(),
            tool_name=ToolName.REPOSITORY_WRITE_FILE,
            arguments_digest="c" * 64,
        )
        with pytest.raises(SubscriptionConflict, match="foreign attempt lineage"):
            await work.subscription.bind_operation(
                cross_run_binding, run_id=second_run.id, task_id=second_task.task_id
            )
        handoff = TaskHandoff(
            run_id=second_run.id,
            task_id=second_task.task_id,
            attempt_id=attempt.attempt_id,
            status=HandoffStatus.BLOCKED,
            summary="blocked",
        )
        with pytest.raises(SubscriptionConflict, match="foreign attempt lineage"):
            await work.subscription.record_decision(handoff, idempotency_key="cross-run-handoff")


@pytest.mark.integration
async def test_run_and_task_budget_is_atomic_across_tasks_and_release_does_not_refund_spend(
    session_factory, persisted_run
) -> None:
    """Concurrent task allocation is capped by RUN and consumed work stays consumed."""
    from forge.domain.subscription import SpecialistPurpose, TaskBudget
    from forge.persistence.repositories.subscription import (
        PostgresSubscriptionRepository,
        SubscriptionConflict,
    )
    from forge.persistence.unit_of_work import PostgresUnitOfWork

    first_task = _task(persisted_run.id, tool_calls=2)
    tasks = (
        first_task,
        _task(
            persisted_run.id,
            tool_calls=2,
            parent_task_id=first_task.task_id,
            purpose=SpecialistPurpose.ROUTINE_IMPLEMENTATION,
        ),
    )
    async with PostgresUnitOfWork(session_factory) as work:
        for index, task in enumerate(tasks):
            await work.subscription.create_task(task, idempotency_key=f"task-{index}")
            await work.subscription.initialize_budget(persisted_run.id, task.task_id, task.budget)
        await work.subscription.initialize_budget(
            persisted_run.id, None, TaskBudget(max_tool_calls=3)
        )
        await work.commit()

    reservations = {task.task_id: uuid4() for task in tasks}

    async def reserve(task_id):
        async with session_factory() as session:
            repo = PostgresSubscriptionRepository(session)
            try:
                await repo.reserve_budget(
                    persisted_run.id,
                    task_id,
                    TaskBudget(max_tool_calls=2),
                    reservation_id=reservations[task_id],
                    idempotency_key=f"reservation-{task_id}",
                )
            except SubscriptionConflict:
                await session.rollback()
                return False
            await session.commit()
            return True

    try:
        results = await asyncio.gather(*(reserve(task.task_id) for task in tasks))
        assert results.count(True) == 1
        winner = tasks[results.index(True)].task_id
        # The same durable reservation identity replays without a second debit.
        assert await reserve(winner)
        async with session_factory() as session:
            repo = PostgresSubscriptionRepository(session)
            await repo.settle_budget(
                persisted_run.id,
                winner,
                TaskBudget(max_tool_calls=2),
                reservation_id=reservations[winner],
                consumed=True,
            )
            # Exact terminal replay is idempotent; reversing it must fail closed.
            await repo.settle_budget(
                persisted_run.id,
                winner,
                TaskBudget(max_tool_calls=2),
                reservation_id=reservations[winner],
                consumed=True,
            )
            with pytest.raises(SubscriptionConflict, match="budget settlement conflicts"):
                await repo.settle_budget(
                    persisted_run.id,
                    winner,
                    TaskBudget(max_tool_calls=2),
                    reservation_id=reservations[winner],
                    consumed=False,
                )
            await session.commit()
        # A retry or lease release must not restore the already spent run allowance.
        assert not await reserve(tasks[1].task_id)
    finally:
        async with session_factory() as session:
            await session.execute(
                text("TRUNCATE subscription_budget_pools, subscription_tasks CASCADE")
            )
            await session.commit()


@pytest.mark.integration
async def test_operation_and_decision_replays_reject_different_payloads(
    session_factory, persisted_run
) -> None:
    from forge.domain.subscription import (
        AttemptIdentity,
        HandoffStatus,
        TaskHandoff,
        ToolCallBinding,
    )
    from forge.domain.tool import ToolName
    from forge.persistence.repositories.subscription import SubscriptionConflict
    from forge.persistence.unit_of_work import PostgresUnitOfWork

    task = _task(persisted_run.id)
    attempt = AttemptIdentity(run_id=persisted_run.id, task_id=task.task_id, attempt_id=uuid4())
    binding = ToolCallBinding(
        attempt_id=attempt.attempt_id,
        provider_call_key="provider-call",
        durable_operation_id=uuid4(),
        tool_name=ToolName.REPOSITORY_WRITE_FILE,
        arguments_digest="d" * 64,
    )
    handoff = TaskHandoff(
        run_id=persisted_run.id,
        task_id=task.task_id,
        attempt_id=attempt.attempt_id,
        status=HandoffStatus.BLOCKED,
        summary="first",
    )
    async with PostgresUnitOfWork(session_factory) as work:
        await work.subscription.create_task(task, idempotency_key="task")
        await work.subscription.create_attempt(
            attempt, route_payload=_binding(), idempotency_key="attempt"
        )
        assert (
            await work.subscription.bind_operation(
                binding, run_id=persisted_run.id, task_id=task.task_id
            )
            == binding
        )
        assert (
            await work.subscription.bind_operation(
                binding, run_id=persisted_run.id, task_id=task.task_id
            )
            == binding
        )
        with pytest.raises(SubscriptionConflict, match="operation replay conflicts"):
            await work.subscription.bind_operation(
                replace(binding, arguments_digest="e" * 64),
                run_id=persisted_run.id,
                task_id=task.task_id,
            )
        assert (
            await work.subscription.record_decision(handoff, idempotency_key="decision") == handoff
        )
        with pytest.raises(SubscriptionConflict, match="decision replay conflicts"):
            await work.subscription.record_decision(
                replace(handoff, summary="different"), idempotency_key="decision"
            )


@pytest.mark.integration
async def test_subscription_schema_enforces_null_scope_uniqueness_and_same_run_fks(
    session_factory, persisted_run
) -> None:
    """Database constraints reject cross-run children and duplicate run pools."""
    from forge.domain.subscription import SpecialistPurpose
    from forge.persistence.unit_of_work import PostgresUnitOfWork

    first = _task(persisted_run.id)
    same_run_child = _task(
        persisted_run.id,
        parent_task_id=first.task_id,
        purpose=SpecialistPurpose.ROUTINE_IMPLEMENTATION,
    )
    second_run = replace(persisted_run, id=uuid4())
    second = _task(second_run.id)
    async with PostgresUnitOfWork(session_factory) as work:
        await work.runs.create(second_run)
        await work.subscription.create_task(first, idempotency_key="constraint-first")
        await work.subscription.create_task(same_run_child, idempotency_key="constraint-child")
        await work.subscription.create_task(second, idempotency_key="constraint-second")
        await work.commit()

    async with session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO subscription_budget_pools (id, run_id, task_row_id, payload) "
                "VALUES (:id, :run_id, NULL, '{}'::jsonb)"
            ),
            {"id": uuid4(), "run_id": persisted_run.id},
        )
        with pytest.raises(IntegrityError):
            await session.execute(
                text(
                    "INSERT INTO subscription_budget_pools (id, run_id, task_row_id, payload) "
                    "VALUES (:id, :run_id, NULL, '{}'::jsonb)"
                ),
                {"id": uuid4(), "run_id": persisted_run.id},
            )
        await session.rollback()
        await session.execute(
            text(
                "INSERT INTO subscription_task_dependencies "
                "(run_id, task_id, dependency_task_id) VALUES (:run_id, :task_id, :dependency)"
            ),
            {
                "run_id": persisted_run.id,
                "task_id": same_run_child.task_id,
                "dependency": first.task_id,
            },
        )
        await session.commit()
        with pytest.raises(IntegrityError):
            await session.execute(
                text(
                    "INSERT INTO subscription_task_dependencies "
                    "(run_id, task_id, dependency_task_id) VALUES (:run_id, :task_id, :dependency)"
                ),
                {
                    "run_id": persisted_run.id,
                    "task_id": first.task_id,
                    "dependency": second.task_id,
                },
            )
        await session.rollback()
        await session.execute(
            text("TRUNCATE subscription_budget_pools, subscription_tasks CASCADE")
        )
        await session.commit()


@pytest.mark.integration
async def test_concurrent_missing_run_pool_initialization_is_serialized(
    session_factory, persisted_run
) -> None:
    from forge.domain.subscription import TaskBudget
    from forge.persistence.repositories.subscription import PostgresSubscriptionRepository

    async def initialize(index: int) -> bool:
        del index
        async with session_factory() as session:
            repo = PostgresSubscriptionRepository(session)
            await repo.initialize_budget(persisted_run.id, None, TaskBudget(max_tool_calls=4))
            await session.commit()
            return True

    assert await asyncio.gather(initialize(1), initialize(2)) == [True, True]
    async with session_factory() as session:
        count = (
            await session.execute(
                text(
                    "SELECT count(*) FROM subscription_budget_pools "
                    "WHERE run_id=:run_id AND task_row_id IS NULL"
                ),
                {"run_id": persisted_run.id},
            )
        ).scalar_one()
        assert count == 1
        await session.execute(
            text("TRUNCATE subscription_budget_pools, subscription_tasks CASCADE")
        )
        await session.commit()


@pytest.mark.integration
async def test_attempt_rejects_binding_with_foreign_requested_route(
    session_factory, persisted_run
) -> None:
    from forge.domain.subscription import (
        AttemptIdentity,
        RouteBinding,
        RouteMapping,
    )
    from forge.persistence.repositories.subscription import SubscriptionConflict
    from forge.persistence.unit_of_work import PostgresUnitOfWork

    task = _task(persisted_run.id)
    preferred = task.route
    alternate = replace(preferred.requested, model="other-model")
    binding = RouteBinding(
        requested=alternate,
        effective=preferred.effective,
        mapping_applied=RouteMapping(
            requested=alternate,
            effective=preferred.effective,
            approved_by="operator",
            approval_id="approval-1",
            reason="test",
        ),
    )
    attempt = AttemptIdentity(run_id=persisted_run.id, task_id=task.task_id, attempt_id=uuid4())
    async with PostgresUnitOfWork(session_factory) as work:
        await work.subscription.create_task(task, idempotency_key="route-task")
        with pytest.raises(SubscriptionConflict, match="task request"):
            await work.subscription.create_attempt(
                attempt, route_payload=binding, idempotency_key="route-attempt"
            )
        with pytest.raises(SubscriptionConflict, match="task binding"):
            await work.subscription.create_attempt(
                attempt,
                route_payload=replace(preferred, is_primary=not preferred.is_primary),
                idempotency_key="changed-binding",
            )
        await work.rollback()


def test_populated_subscription_schema_explicitly_refuses_downgrade(
    test_database_url, alembic_config_factory
) -> None:
    config = alembic_config_factory(test_database_url)
    command.upgrade(config, "head")

    async def seed_and_read(*, seed: bool):
        engine = create_async_engine(test_database_url)
        try:
            if seed:
                async with engine.begin() as connection:
                    await connection.execute(
                        text(
                            "INSERT INTO subscription_profile_versions (id, profile_id, version, payload) VALUES (:row_id, :profile_id, 1, '{}'::jsonb)"
                        ),
                        {"row_id": uuid4(), "profile_id": uuid4()},
                    )
                return None
            async with engine.connect() as connection:
                return (
                    (
                        await connection.execute(text("SELECT version_num FROM alembic_version"))
                    ).scalar_one(),
                    (
                        await connection.execute(
                            text("SELECT count(*) FROM subscription_profile_versions")
                        )
                    ).scalar_one(),
                )
        finally:
            await engine.dispose()

    asyncio.run(seed_and_read(seed=True))
    with pytest.raises(
        Exception, match="remove v0.2 disposable subscription data before downgrade"
    ):
        command.downgrade(config, "20260909_0006")
    assert asyncio.run(seed_and_read(seed=False)) == (
        ScriptDirectory.from_config(config).get_current_head(),
        1,
    )


def test_schema_0007_representative_runs_and_evidence_upgrade_without_rewriting(
    test_database_url, alembic_config_factory
) -> None:
    """Approval, execution, and recovery rows retain exact legacy identity at head."""
    config = alembic_config_factory(test_database_url)
    command.upgrade(config, "20260908_0007")
    ids = {
        name: uuid4()
        for name in (
            "project",
            "task",
            "approval",
            "execution",
            "recovery",
            "actor",
            "agent",
            "artifact",
            "operation",
        )
    }
    policy_digest, task_digest, evidence_digest = "1" * 64, "2" * 64, "3" * 64

    async def seed():
        engine = create_async_engine(test_database_url)
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO projects (id, canonical_path, github_repository, default_branch) VALUES (:project, '/tmp/legacy', 'owner/repo', 'main')"
                ),
                ids,
            )
            await connection.execute(
                text(
                    "INSERT INTO project_policy_versions (project_id, version, policy_digest, document_schema_version, document) VALUES (:project, 1, :digest, 1, CAST(:document AS jsonb))"
                ),
                {**ids, "digest": policy_digest, "document": '{"legacy":true}'},
            )
            await connection.execute(
                text("UPDATE projects SET current_policy_version=1 WHERE id=:project"), ids
            )
            await connection.execute(
                text(
                    "INSERT INTO tasks (id, project_id, normalized_text, task_digest) VALUES (:task, :project, 'legacy task', :digest)"
                ),
                {**ids, "digest": task_digest},
            )
            for key, state, version in (
                ("approval", "AWAITING_PLAN_APPROVAL", 4),
                ("execution", "IMPLEMENTING", 7),
                ("recovery", "PAUSED", 9),
            ):
                await connection.execute(
                    text(
                        "INSERT INTO runs (id, project_id, task_id, policy_version, state, version, suspended_state, suspension_kind, suspension_context_schema_version, suspension_context, local_remediation_count, remote_remediation_count, token_budget, cost_budget_minor, duration_budget_seconds, database_state, pending_gate, pending_evidence_digest) VALUES (:run, :project, :task, 1, :state, :version, :suspended, :kind, :context_version, CAST(:context AS jsonb), 0, 0, 123, 456, 789, 'DISABLED', :gate, :evidence)"
                    ),
                    {
                        **ids,
                        "run": ids[key],
                        "state": state,
                        "version": version,
                        "suspended": "IMPLEMENTING" if key == "recovery" else None,
                        "kind": "PAUSE" if key == "recovery" else None,
                        "context_version": 1 if key == "recovery" else None,
                        "context": '{"reason":"operator"}' if key == "recovery" else None,
                        "gate": "plan" if key == "approval" else None,
                        "evidence": evidence_digest if key == "approval" else None,
                    },
                )
            await connection.execute(
                text(
                    "INSERT INTO approvals (id, run_id, gate, evidence_digest, run_version, policy_version, authenticated_actor_id) VALUES (gen_random_uuid(), :approval, 'plan', :evidence, 4, 1, :actor)"
                ),
                {**ids, "evidence": evidence_digest},
            )
            await connection.execute(
                text(
                    "INSERT INTO agent_executions (id, run_id, role, instruction_digest, instruction_version, provider, model, status) VALUES (:agent, :execution, 'developer', :digest, 'legacy-v1', 'openai', 'legacy-model', 'RUNNING')"
                ),
                {**ids, "digest": "4" * 64},
            )
            await connection.execute(
                text(
                    "INSERT INTO operation_intents (id, run_id, operation_kind, idempotency_key, request_digest, request_schema_version, request_payload, status, outcome_schema_version, outcome_payload, attempt_count, started_at, completed_at) VALUES (:operation, :execution, 'repository_write_file', 'legacy-operation', :digest, 1, CAST(:request AS jsonb), 'SUCCEEDED', 1, CAST(:outcome AS jsonb), 1, now(), now())"
                ),
                {
                    **ids,
                    "digest": "6" * 64,
                    "request": '{"path":"legacy.txt"}',
                    "outcome": '{"receipt":"legacy-receipt"}',
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO artifacts (id, digest, media_type, storage_pointer, size_bytes, metadata_schema_version, metadata) VALUES (:artifact, :digest, 'application/json', :pointer, 17, 1, CAST(:metadata AS jsonb))"
                ),
                {
                    **ids,
                    "digest": "5" * 64,
                    "pointer": f"sha256/55/{'5' * 62}.blob",
                    "metadata": '{"stage":"recovery"}',
                },
            )
        await engine.dispose()

    asyncio.run(seed())
    command.upgrade(config, "head")

    async def verify():
        engine = create_async_engine(test_database_url)
        async with engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        text(
                            "SELECT id, state, version, token_budget, cost_budget_minor, duration_budget_seconds, pending_evidence_digest, suspension_context FROM runs ORDER BY version"
                        )
                    )
                )
                .mappings()
                .all()
            )
            assert [(row["id"], row["state"], row["version"]) for row in rows] == [
                (ids["approval"], "AWAITING_PLAN_APPROVAL", 4),
                (ids["execution"], "IMPLEMENTING", 7),
                (ids["recovery"], "PAUSED", 9),
            ]
            assert all(
                (row["token_budget"], row["cost_budget_minor"], row["duration_budget_seconds"])
                == (123, 456, 789)
                for row in rows
            )
            assert rows[0]["pending_evidence_digest"] == evidence_digest
            assert rows[2]["suspension_context"] == {"reason": "operator"}
            assert (
                await connection.execute(
                    text(
                        "SELECT policy_digest FROM project_policy_versions WHERE project_id=:project"
                    ),
                    ids,
                )
            ).scalar_one() == policy_digest
            assert (
                await connection.execute(text("SELECT task_digest FROM tasks WHERE id=:task"), ids)
            ).scalar_one() == task_digest
            assert (
                await connection.execute(
                    text("SELECT instruction_digest FROM agent_executions WHERE id=:agent"), ids
                )
            ).scalar_one() == "4" * 64
            artifact = (
                await connection.execute(
                    text("SELECT digest, size_bytes, metadata FROM artifacts WHERE id=:artifact"),
                    ids,
                )
            ).one()
            assert artifact == ("5" * 64, 17, {"stage": "recovery"})
            operation = (
                await connection.execute(
                    text(
                        "SELECT request_digest, request_payload, outcome_payload, attempt_count FROM operation_intents WHERE id=:operation"
                    ),
                    ids,
                )
            ).one()
            assert operation == ("6" * 64, {"path": "legacy.txt"}, {"receipt": "legacy-receipt"}, 1)
            assert (
                await connection.execute(text("SELECT count(*) FROM subscription_envelopes"))
            ).scalar_one() == 0
        await engine.dispose()

    asyncio.run(verify())
