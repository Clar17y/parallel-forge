"""Subscription commit authority uses the existing two-phase controlled effect."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from uuid import uuid4, uuid5

import pytest
from forge.application.services.recovery import OperationExecutor
from forge.application.services.subscription_broker import (
    ControlledSubscriptionEffect,
    SubscriptionToolBroker,
)
from forge.application.services.tools import ControlledToolService
from forge.domain.operation import canonical_digest
from forge.domain.resource import WorktreeIdentity
from forge.domain.subscription import (
    AttemptIdentity,
    BrokerAuthorizationBinding,
    ExecutionEnvelope,
    LogicalTaskContract,
    OperatorProfile,
    RolePreference,
    RouteBinding,
    SpecialistPurpose,
    TaskBudget,
    encode_subscription_record,
)
from forge.domain.tool import SubscriptionToolAuthorizationContext, ToolName
from forge.persistence.models import AgentExecution, Run, Step
from forge.persistence.models.scheduling import SubscriptionScheduledTask
from forge.persistence.models.subscription import SubscriptionAttempt, SubscriptionTask
from forge.persistence.repositories.operations import PostgresOperationRepository
from forge.persistence.unit_of_work import PostgresUnitOfWork
from sqlalchemy import delete, update

from apps.orchestrator.tests.persistence.test_scheduler_acceptance import (
    _remove_disposable_subscription_rows,  # noqa: F401
)
from apps.orchestrator.tests.persistence.test_subscription_named_check import (
    _enqueue,
    _route,
)
from apps.orchestrator.tests.persistence.test_tool_write_invocation import (
    _ControlledArtifactStore,
    _seed_test_database,
)
from apps.orchestrator.tests.tools.test_git import _controlled, _git


async def _case(session_factory, tmp_path: Path, *, closed_reader=None, primary_writer=False):
    (
        project_id,
        run_id,
        step_id,
        execution_id,
        _,
        branch,
        repository_path,
        _,
        _,
    ) = await _seed_test_database(session_factory, tmp_path)
    repository = Path(repository_path)
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "user.name", "Forge Test")
    _git(repository, "config", "user.email", "forge@example.test")
    (repository / "README.md").write_text("initial\n", encoding="utf-8")
    (repository / ".gitignore").write_text(".worktrees/\n", encoding="utf-8")
    _git(repository, "add", "README.md", ".gitignore")
    _git(repository, "commit", "-m", "initial")
    git = _controlled(repository, tmp_path / "git-state")
    base_sha = git.resolve_default_base_sha()
    identity = WorktreeIdentity.for_run(project_id, run_id, branch, False)
    worktree = git.create_worktree(identity, base_sha)
    (worktree.path / "README.md").write_text("changed\n", encoding="utf-8")
    async with PostgresUnitOfWork(session_factory) as work:
        await work.session.execute(delete(AgentExecution).where(AgentExecution.id == execution_id))
        await work.session.execute(delete(Step).where(Step.id == step_id))
        await work.session.execute(
            update(Run)
            .where(Run.id == run_id)
            .values(base_sha=base_sha, worktree_path=str(worktree.path))
        )
        route = _route("p")
        specialist = (
            SpecialistPurpose.PRIMARY
            if primary_writer
            else closed_reader or SpecialistPurpose.INTEGRATION
        )
        primary_paths = ("README.md",) if primary_writer else ("apps",)
        profile_purpose = (
            SpecialistPurpose.INTEGRATION if specialist is SpecialistPurpose.PRIMARY else specialist
        )
        profile = OperatorProfile(
            profile_id=uuid4(),
            version=1,
            preferences=(
                RolePreference(purpose=SpecialistPurpose.PRIMARY, preferred_route=route),
                RolePreference(purpose=profile_purpose, preferred_route=route),
            ),
        )
        await work.subscription.store_profile(profile)
        await work.subscription.freeze_envelope(
            ExecutionEnvelope(
                run_id=run_id,
                profile_id=profile.profile_id,
                profile_version=1,
                safety_policy_version=1,
                routes=(
                    (
                        SpecialistPurpose.PRIMARY,
                        RouteBinding(requested=route, effective=route, is_primary=True),
                    ),
                    (profile_purpose, RouteBinding(requested=route, effective=route)),
                ),
            )
        )
        primary = uuid4()
        await work.subscription.create_task(
            LogicalTaskContract(
                run_id=run_id,
                task_id=primary,
                purpose=SpecialistPurpose.PRIMARY,
                route=RouteBinding(requested=route, effective=route, is_primary=True),
                budget=TaskBudget(),
                owned_paths=primary_paths,
            ),
            idempotency_key=f"primary-{primary}",
        )
        await work.scheduler.admit_run(run_id)
        if specialist is SpecialistPurpose.PRIMARY:
            from forge.domain.scheduling import ScheduleTask

            task = primary
            await work.scheduler.enqueue(
                ScheduleTask(
                    run_id=run_id,
                    task_id=task,
                    worktree_id=identity.worktree_name,
                    owned_paths=primary_paths,
                    max_repairs=3,
                )
            )
        else:
            task = await _enqueue(
                work,
                run_id=run_id,
                provider="p",
                worktree=identity.worktree_name,
                parent_id=primary,
                paths=() if closed_reader else ("apps",),
                purpose=specialist,
            )
        if closed_reader:
            epoch = await work.scheduler.begin_candidate(run_id)
            await work.scheduler.close_candidate(run_id, epoch)
        attempt = uuid4()
        await work.subscription.create_attempt(
            AttemptIdentity(run_id=run_id, task_id=task, attempt_id=attempt),
            route_payload=RouteBinding(
                requested=route, effective=route, is_primary=specialist is SpecialistPurpose.PRIMARY
            ),
            idempotency_key="commit-attempt",
        )
        await work.session.execute(
            update(SubscriptionTask).where(SubscriptionTask.id == task).values(state="running")
        )
        await work.session.execute(
            update(SubscriptionAttempt)
            .where(SubscriptionAttempt.id == attempt)
            .values(status="running")
        )
        lease = await work.scheduler.claim_ready("commit-owner", timedelta(seconds=30))
        if closed_reader:
            assert lease is not None
            await work.session.execute(
                update(SubscriptionAttempt)
                .where(SubscriptionAttempt.id == attempt)
                .values(
                    lease_owner=lease.owner,
                    lease_generation=lease.generation,
                    candidate_epoch=1,
                    envelope_digest=canonical_digest(
                        encode_subscription_record(await work.subscription.envelope_for_run(run_id))
                    ),
                    task_digest=canonical_digest(
                        encode_subscription_record(await work.subscription.get_task(run_id, task))
                    ),
                )
            )
        await work.commit()
    assert lease
    context = SubscriptionToolAuthorizationContext(
        run_id=run_id,
        task_id=task,
        attempt_id=attempt,
        worktree_id=identity.worktree_name,
        purpose=specialist,
        policy_version=1,
        permitted_tools=frozenset({ToolName.GIT_DIFF if closed_reader else ToolName.GIT_COMMIT}),
    )
    store = _ControlledArtifactStore(tmp_path / "artifacts")
    service = ControlledToolService(
        lambda: PostgresUnitOfWork(session_factory),
        controlled_git=git,
        worktree=worktree,
        artifact_store=store,
        operation_executor=OperationExecutor(PostgresOperationRepository(session_factory)),
    )
    authority = BrokerAuthorizationBinding(
        run_id=run_id,
        task_id=task,
        attempt_id=attempt,
        worktree_id=identity.worktree_name,
        role=specialist,
        policy_version=1,
        permitted_tools=frozenset({ToolName.GIT_DIFF if closed_reader else ToolName.GIT_COMMIT}),
        broker_token="commit-token",
    )
    broker = SubscriptionToolBroker(
        lambda: PostgresUnitOfWork(session_factory),
        lease=lease,
        authority=authority,
        effect=ControlledSubscriptionEffect(service, context),
    )
    return broker, git, attempt, store, worktree


@pytest.mark.integration
@pytest.mark.parametrize("primary_writer", [False, True])
async def test_subscription_commit_runs_two_phases_once_and_replays(
    session_factory, tmp_path, primary_writer
):
    broker, _git_adapter, attempt, _, _ = await _case(
        session_factory, tmp_path, primary_writer=primary_writer
    )
    args = {"message": "feat: subscription commit"}
    receipt = await broker.invoke(
        token="commit-token",
        provider_call_key="commit",
        tool_name=ToolName.GIT_COMMIT,
        arguments=args,
    )
    assert receipt.accepted and receipt.result["status"] == "succeeded"
    assert (
        await broker.invoke(
            token="commit-token",
            provider_call_key="commit",
            tool_name=ToolName.GIT_COMMIT,
            arguments=args,
        )
        == receipt
    )
    async with PostgresUnitOfWork(session_factory) as work:
        call = await work.tool_calls.get(uuid5(attempt, "forge-subscription-tool-v1:commit"))
        assert call.subscription_attempt_id == attempt and call.agent_execution_id is None
        prepare = await work.operations.get(call.operation_intent_id)
        publish = await work.operations.get_by_idempotency_key(f"git.commit:{call.id}:publish")
        assert prepare.id == call.id
        assert prepare.request_payload["authority_schema_version"] == (3 if primary_writer else 2)
        if primary_writer:
            assert prepare.request_payload["owned_paths_json"] == '["README.md"]'
            assert publish.request_payload["owned_paths_json"] == '["README.md"]'
        assert publish is not None and publish.id != prepare.id
        assert publish.request_payload["subscription_attempt_id"] == str(attempt)


@pytest.mark.integration
@pytest.mark.parametrize(
    "primary_writer,corruption",
    [
        (False, None),
        (False, "preparation_attempt"),
        (False, "publication_attempt"),
        (True, None),
        (True, "preparation_attempt"),
        (True, "publication_attempt"),
        (True, "preparation_scope"),
        (True, "publication_scope"),
        (True, "old_codec"),
    ],
)
async def test_subscription_commit_recovers_historical_receipts_without_live_lease(
    session_factory, tmp_path, monkeypatch, corruption, primary_writer
):
    from datetime import UTC, datetime

    from forge.application.services.tool_recovery import (
        ToolRecoveryDisposition,
        ToolRecoveryService,
    )
    from forge.domain.operation import canonical_digest
    from forge.domain.tool import ToolCallStatus
    from forge.persistence.models import OperationIntent

    broker, git_adapter, attempt, store, worktree = await _case(
        session_factory, tmp_path, primary_writer=primary_writer
    )
    from forge.application.services.subscription_broker import BrokerDenied

    async def lose_receipt(*args, **kwargs):
        raise OSError("injected artifact receipt loss after publication")

    with monkeypatch.context() as patch:
        patch.setattr(store, "put_bytes", lose_receipt)
        with pytest.raises(BrokerDenied):
            await broker.invoke(
                token="commit-token",
                provider_call_key="commit",
                tool_name=ToolName.GIT_COMMIT,
                arguments={"message": "feat: historical subscription commit"},
            )
    call_id = uuid5(attempt, "forge-subscription-tool-v1:commit")
    async with PostgresUnitOfWork(session_factory) as work:
        call = await work.tool_calls.get(call_id)
        assert call.status is ToolCallStatus.RUNNING
        await work.session.execute(
            update(SubscriptionScheduledTask)
            .where(SubscriptionScheduledTask.task_id == call.subscription_task_id)
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await work.session.execute(
            update(SubscriptionAttempt)
            .where(SubscriptionAttempt.id == attempt)
            .values(status="terminal")
        )
        publication = await work.operations.get_by_idempotency_key(f"git.commit:{call_id}:publish")
        row = await work.session.get(OperationIntent, publication.id)
        row.execution_lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await work.commit()
    from forge.application.adapters.git_commit import PublishGitCommitAdapter
    from forge.application.services.recovery import RecoveryService
    from forge.domain.operation import OperationStatus

    operations = PostgresOperationRepository(session_factory)
    reconciled = await RecoveryService(operations).reconcile(
        publication.id, PublishGitCommitAdapter(git_adapter, worktree, operations)
    )
    assert reconciled.status is OperationStatus.SUCCEEDED
    async with PostgresUnitOfWork(session_factory) as work:
        if corruption:
            if corruption.startswith("preparation") or corruption == "old_codec":
                operation_id = call.operation_intent_id
            else:
                publication = await work.operations.get_by_idempotency_key(
                    f"git.commit:{call_id}:publish"
                )
                operation_id = publication.id
            row = await work.session.get(OperationIntent, operation_id)
            if corruption == "old_codec":
                payload = dict(row.request_payload)
                del payload["owned_paths_json"]
                row.request_payload = payload | {"authority_schema_version": 2}
            elif corruption.endswith("scope"):
                row.request_payload = {**row.request_payload, "owned_paths_json": '["foreign"]'}
            else:
                row.request_payload = {
                    **row.request_payload,
                    "subscription_attempt_id": str(uuid4()),
                }
            row.request_digest = canonical_digest(row.request_payload)
        await work.commit()
    recovery = ToolRecoveryService(lambda: PostgresUnitOfWork(session_factory), store)
    result = await recovery.recover_one(call_id)
    expected = (
        ToolRecoveryDisposition.SETTLED
        if corruption is None
        else ToolRecoveryDisposition.INTERVENTION
    )
    assert result.disposition is expected
    async with PostgresUnitOfWork(session_factory) as work:
        recovered = await work.tool_calls.get(call_id)
        assert recovered.status is (
            ToolCallStatus.SUCCEEDED if corruption is None else ToolCallStatus.RUNNING
        )
        assert recovered.subscription_attempt_id == attempt
        assert recovered.agent_execution_id is None
    if corruption is None:
        assert (await recovery.recover_one(call_id)).disposition is ToolRecoveryDisposition.TERMINAL
