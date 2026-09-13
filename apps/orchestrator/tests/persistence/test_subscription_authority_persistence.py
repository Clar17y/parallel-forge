"""Real controlled effects with subscription lineage and no legacy execution."""

import hashlib
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from forge.application.services.subscription_broker import (
    BrokerDenied,
    ControlledSubscriptionEffect,
    SubscriptionToolBroker,
)
from forge.domain.subscription import (
    AttemptIdentity,
    BrokerAuthorizationBinding,
    RouteBinding,
    SpecialistPurpose,
)
from forge.domain.tool import SubscriptionToolAuthorizationContext, ToolName
from forge.persistence.models import AgentExecution, RunEvent, Step, ToolCall
from forge.persistence.models.subscription import SubscriptionAttempt, SubscriptionTask
from forge.persistence.repositories.tool_calls import PostgresToolCallRepository
from forge.persistence.unit_of_work import PostgresUnitOfWork
from sqlalchemy import delete, select, update
from test_scheduler_acceptance import (
    _admit_run,
    _enqueue,
    _remove_disposable_subscription_rows,  # noqa: F401 - imported autouse fixture
    _route,
)
from test_tool_write_invocation import (
    _ControlledArtifactStore,
    _ControlledWriter,
    _Git,
    _seed_test_database,
    _setup_service,
)


@pytest.mark.integration
@pytest.mark.parametrize(
    "tool_name,lose_receipt",
    [
        (ToolName.REPOSITORY_WRITE_FILE, False),
        (ToolName.REPOSITORY_READ_FILE, False),
        (ToolName.REPOSITORY_LIST_FILES, False),
        (ToolName.REPOSITORY_SEARCH, False),
        (ToolName.REPOSITORY_READ_INSTRUCTIONS, False),
        (ToolName.GIT_STATUS, False),
        (ToolName.GIT_DIFF, False),
        (ToolName.REPOSITORY_DELETE_FILE, False),
        (ToolName.REPOSITORY_RENAME_FILE, False),
        (ToolName.REPOSITORY_DELETE_FILE, True),
        (ToolName.REPOSITORY_RENAME_FILE, True),
        (ToolName.REPOSITORY_WRITE_FILE, True),
    ],
)
async def test_subscription_effect_uses_actual_lineage_and_recovers_without_legacy_execution(
    session_factory, tmp_path, monkeypatch, tool_name, lose_receipt
):
    (
        project_id,
        run_id,
        step_id,
        execution_id,
        base_sha,
        branch,
        repo,
        tree,
        _,
    ) = await _seed_test_database(session_factory, tmp_path)
    async with session_factory() as session, session.begin():
        await session.execute(delete(AgentExecution).where(AgentExecution.id == execution_id))
        await session.execute(delete(Step).where(Step.id == step_id))
    if tool_name is not ToolName.REPOSITORY_WRITE_FILE:
        (Path(tree) / "apps").mkdir()
        (Path(tree) / "apps/hello.py").write_text("original read content", encoding="utf-8")
        (Path(tree) / "apps/AGENTS.md").write_text("local instructions", encoding="utf-8")
    git_calls = []
    if tool_name in {ToolName.GIT_STATUS, ToolName.GIT_DIFF}:
        from forge.application.ports.worktrees import GitCandidateDiff, GitDiff, GitStatus

        def git_read(_self, bound):
            assert bound.path == Path(tree)
            git_calls.append(bound)
            if tool_name is ToolName.GIT_STATUS:
                return GitStatus(text=" M apps/hello.py", original_byte_count=16, truncated=False)
            return GitCandidateDiff(
                head_sha="b" * 40,
                diff=GitDiff(text="candidate diff", original_byte_count=14, truncated=False),
                changed_paths=("apps/hello.py",),
            )

        monkeypatch.setattr(
            _Git,
            "status" if tool_name is ToolName.GIT_STATUS else "candidate_diff",
            git_read,
            raising=False,
        )
    writer = _ControlledWriter(Path(tree))
    store = _ControlledArtifactStore(tmp_path / "artifacts")
    if lose_receipt:
        original_put = store.put_bytes
        first = True

        async def fail_first_receipt(*args, **kwargs):
            nonlocal first
            if first:
                first = False
                raise OSError("injected lost artifact receipt")
            return await original_put(*args, **kwargs)

        monkeypatch.setattr(store, "put_bytes", fail_first_receipt)

    service, _, worktree = _setup_service(
        session_factory,
        project_id,
        run_id,
        branch,
        base_sha,
        repo,
        tree,
        writer,
        store,
    )
    attempt_id = uuid4()
    async with PostgresUnitOfWork(session_factory) as work:
        run = await work.runs.get(run_id)
        primary = await _admit_run(work, run, (_route("p"), _route("p")))
        task_id = await _enqueue(
            work,
            run_id,
            provider="p",
            worktree=worktree.identity.worktree_name,
            parent_id=primary,
            paths=("apps",),
        )
        await work.subscription.create_attempt(
            AttemptIdentity(run_id=run_id, task_id=task_id, attempt_id=attempt_id),
            route_payload=RouteBinding(requested=_route("p"), effective=_route("p")),
            idempotency_key="attempt",
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
    assert lease is not None and lease.task_id == task_id
    context = SubscriptionToolAuthorizationContext(
        run_id=run_id,
        task_id=task_id,
        attempt_id=attempt_id,
        worktree_id=worktree.identity.worktree_name,
        purpose=SpecialistPurpose.ROUTINE_IMPLEMENTATION,
        policy_version=1,
        permitted_tools=frozenset({tool_name}),
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
            permitted_tools=context.permitted_tools,
            broker_token="test-only",
        ),
        effect=ControlledSubscriptionEffect(service, context),
    )
    digest = hashlib.sha256(b"original read content").hexdigest()
    arguments = {
        ToolName.REPOSITORY_WRITE_FILE: {"path": "apps/hello.py", "content": "# unicode: é\n"},
        ToolName.REPOSITORY_READ_FILE: {"path": "apps/hello.py"},
        ToolName.REPOSITORY_LIST_FILES: {"path": "apps"},
        ToolName.REPOSITORY_SEARCH: {"path": "apps", "literal": "original"},
        ToolName.REPOSITORY_READ_INSTRUCTIONS: {"target_path": "apps/hello.py"},
        ToolName.GIT_STATUS: {},
        ToolName.GIT_DIFF: {"scope": "candidate"},
        ToolName.REPOSITORY_DELETE_FILE: {"path": "apps/hello.py", "expected_digest": digest},
        ToolName.REPOSITORY_RENAME_FILE: {
            "source": "apps/hello.py",
            "destination": "apps/renamed.py",
            "expected_digest": digest,
        },
    }[tool_name]
    kwargs = {
        "token": "test-only",
        "provider_call_key": "effect",
        "tool_name": tool_name,
        "arguments": arguments,
    }
    if lose_receipt:
        from forge.application.services.tool_recovery import (
            ToolRecoveryDisposition,
            ToolRecoveryService,
        )
        from forge.domain.tool import ToolCallStatus

        with pytest.raises(BrokerDenied):
            await broker.invoke(**kwargs)
        async with session_factory() as session:
            call_id = await session.scalar(select(ToolCall.id).where(ToolCall.run_id == run_id))
        assert call_id is not None
        recovery = ToolRecoveryService(lambda: PostgresUnitOfWork(session_factory), store)
        recovered = await recovery.recover_one(call_id)
        assert recovered.disposition is ToolRecoveryDisposition.SETTLED
        async with PostgresUnitOfWork(session_factory) as work:
            call = await work.tool_calls.get(call_id)
            assert call.status is ToolCallStatus.SUCCEEDED
            assert call.subscription_attempt_id == attempt_id
            assert call.agent_execution_id is None
            assert len(call.artifact_digests) == 1
        assert writer.call_count == 1
        return
    receipt = await broker.invoke(**kwargs)
    assert receipt.accepted
    assert receipt.result["status"] == "succeeded"
    mutation = tool_name in {
        ToolName.REPOSITORY_WRITE_FILE,
        ToolName.REPOSITORY_DELETE_FILE,
        ToolName.REPOSITORY_RENAME_FILE,
    }
    expected_intent = receipt.operation_id if mutation else None
    assert receipt.result["operation_intent_id"] == (
        str(expected_intent) if expected_intent else None
    )
    assert await broker.invoke(**kwargs) == receipt
    assert writer.call_count == int(mutation)
    metadata = receipt.result["metadata"]
    if tool_name is ToolName.REPOSITORY_LIST_FILES:
        assert any(entry["path"] == "apps/hello.py" for entry in metadata["entries"])
    elif tool_name is ToolName.REPOSITORY_SEARCH:
        assert metadata["matches"][0]["line_text"] == "original read content"
    elif tool_name is ToolName.REPOSITORY_READ_INSTRUCTIONS:
        assert metadata["documents"][0]["content"] == "local instructions"
        assert metadata["documents"][0]["untrusted_repository_content"] is True
    elif tool_name is ToolName.GIT_STATUS:
        assert metadata["text"] == " M apps/hello.py"
        assert len(git_calls) == 1
    elif tool_name is ToolName.GIT_DIFF:
        assert metadata["head_sha"] == "b" * 40
        assert metadata["text"] == "candidate diff"
        assert metadata["untrusted_repository_content"] is True
        assert len(git_calls) == 1
    if tool_name in {ToolName.REPOSITORY_DELETE_FILE, ToolName.REPOSITORY_RENAME_FILE}:
        assert not (Path(tree) / "apps/hello.py").exists()
    if tool_name is ToolName.REPOSITORY_RENAME_FILE:
        assert (Path(tree) / "apps/renamed.py").read_text(
            encoding="utf-8"
        ) == "original read content"
    async with session_factory() as session:
        calls = (await session.scalars(select(ToolCall).where(ToolCall.run_id == run_id))).all()
        assert len(calls) == 1
        record = await PostgresToolCallRepository(session).get(calls[0].id)
        assert record.agent_execution_id is None and record.step_id is None
        assert record.subscription_task_id == task_id
        assert record.subscription_attempt_id == attempt_id
        assert record.operation_intent_id == expected_intent
        event = await session.scalar(
            select(RunEvent).where(
                RunEvent.run_id == run_id, RunEvent.event_type == "tool_call.completed"
            )
        )
        assert event is not None and event.actor_id == attempt_id
        assert event.payload["subscription_task_id"] == str(task_id)
        assert (
            await session.scalars(select(AgentExecution).where(AgentExecution.run_id == run_id))
        ).all() == []
