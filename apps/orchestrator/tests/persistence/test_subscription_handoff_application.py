"""Atomic completion uses settled source, observation and verified evidence."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from forge.application.ports.subscription_decisions import SubscriptionDecisionError
from forge.application.ports.subscription_handoff import (
    HandoffCallProof,
    VerifiedSubscriptionHandoff,
)
from forge.application.services.subscription_decisions import SubscriptionDecisionApplication
from forge.domain.operation import canonical_digest
from forge.domain.subscription import encode_subscription_record
from forge.persistence.models.scheduling import SubscriptionScheduledTask
from forge.persistence.models.subscription import SubscriptionAttempt, SubscriptionTask
from forge.persistence.models.subscription_handoff import SubscriptionHandoffFence
from forge.persistence.models.subscription_results import SubscriptionAttemptResult
from forge.persistence.repositories.scheduling import PostgresSchedulingRepository
from forge.persistence.repositories.subscription_handoff_evidence import (
    PostgresSubscriptionHandoffEvidence,
)
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_handoff_proposal import handoff_case


async def application_case(
    session_factory, tmp_path, monkeypatch, *, repairs=0, primary_budget=None
):
    factory, parent, child, handoff = await handoff_case(
        session_factory, tmp_path, repairs=repairs, primary_budget=primary_budget
    )
    application = SubscriptionDecisionApplication(factory)
    observation = await application.begin_handoff_observation(child.attempt.attempt_id, uuid4())
    identity = UUID(handoff.evidence_receipt_ids[0])
    proof = VerifiedSubscriptionHandoff(
        run_id=child.task.run_id,
        task_id=child.task.task_id,
        attempt_id=child.attempt.attempt_id,
        snapshot_call_id=identity,
        manifest_digest="d" * 64,
        candidate_tree_digest=handoff.candidate_tree_digest,
        policy_version=child.envelope.safety_policy_version,
        task_digest=canonical_digest(encode_subscription_record(child.task)),
        handoff_digest=canonical_digest(encode_subscription_record(handoff)),
        call_proofs=(HandoffCallProof(identity, "e" * 64, "f" * 64),),
        artifact_proofs=(("d" * 64, "1" * 64),),
        checks_match_snapshot=True,
        output_digest="2" * 64,
        current_tree_digest="3" * 64,
    )

    # Evidence IO/digest locking has separate real broker/Git integration tests.
    # This fixture isolates the transaction's source, fence and wake behavior.
    async def verified(self, supplied):
        return True

    monkeypatch.setattr(PostgresSubscriptionHandoffEvidence, "verify", verified)
    return factory, parent, child, application, observation, proof


@pytest.mark.integration
async def test_handoff_completion_records_evidence_and_wakes_parent_once(
    session_factory, tmp_path, monkeypatch
):
    factory, parent, child, application, observation, proof = await application_case(
        session_factory, tmp_path, monkeypatch
    )
    result = await application.apply_handoff(observation, proof)
    assert result.accepted and result.disposition == "handoff_completed" and not result.replayed
    assert (await application.apply_handoff(observation, proof)).replayed
    async with factory() as work:
        task = await work.session.get(SubscriptionTask, child.task.task_id)
        scheduled = await work.session.get(SubscriptionScheduledTask, task.id)
        attempt = await work.session.get(SubscriptionAttempt, child.attempt.attempt_id)
        stored = await work.session.get(SubscriptionAttemptResult, attempt.id)
        assert task.state == scheduled.state == attempt.status == "terminal"
        assert scheduled.lease_owner is None
        assert task.version == observation.proposal.task_version + 1
        assert stored.application_payload["current_tree_digest"] == proof.current_tree_digest
        assert canonical_digest(stored.application_payload) == stored.application_digest
        assert (await work.session.get(SubscriptionTask, parent.task.task_id)).state == "queued"


@pytest.mark.integration
async def test_concurrent_completion_applies_once(session_factory, tmp_path, monkeypatch):
    import asyncio

    _, _, _, application, observation, proof = await application_case(
        session_factory, tmp_path, monkeypatch
    )
    results = await asyncio.gather(
        *(application.apply_handoff(observation, proof) for _ in range(2))
    )
    assert all(result.accepted for result in results)
    assert sorted(result.replayed for result in results) == [False, True]


@pytest.mark.integration
@pytest.mark.parametrize(
    "mutation",
    [
        "historical",
        "checks",
        "task_digest",
        "handoff_digest",
        "candidate_digest",
        "receipts",
        "expired",
        "replaced",
        "cancel",
        "verification",
    ],
)
async def test_completion_rejects_stale_or_incomplete_proof(
    session_factory, tmp_path, monkeypatch, mutation
):
    factory, parent, child, application, observation, proof = await application_case(
        session_factory, tmp_path, monkeypatch
    )
    if mutation == "historical":
        proof = replace(proof, current_tree_digest=None)
    elif mutation == "checks":
        proof = replace(proof, checks_match_snapshot=False)
    elif mutation in {"task_digest", "handoff_digest"}:
        proof = replace(proof, **{mutation: "0" * 64})
    elif mutation == "candidate_digest":
        proof = replace(proof, candidate_tree_digest="0" * 64)
    elif mutation == "receipts":
        proof = replace(proof, call_proofs=())
    elif mutation == "verification":

        async def rejected(self, supplied):
            return False

        monkeypatch.setattr(PostgresSubscriptionHandoffEvidence, "verify", rejected)
    else:
        async with factory() as work:
            if mutation == "cancel":
                row = await work.session.get(SubscriptionTask, child.task.task_id)
                row.cancel_requested = True
            else:
                row = await work.session.get(
                    SubscriptionHandoffFence, observation.proposal.worktree.identity.worktree_name
                )
                if mutation == "expired":
                    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
                else:
                    row.token = uuid4()
            await work.commit()
    with pytest.raises(SubscriptionDecisionError):
        await application.apply_handoff(observation, proof)
    async with factory() as work:
        result = await work.session.get(SubscriptionAttemptResult, child.attempt.attempt_id)
        assert result.disposition == "decision_pending" and result.application_payload is None
        assert (await work.session.get(SubscriptionTask, child.task.task_id)).state == "reconciling"
        assert (await work.session.get(SubscriptionTask, parent.task.task_id)).state == "blocked"


@pytest.mark.integration
async def test_completion_rolls_back_child_receipt_and_parent_wake_together(
    session_factory, tmp_path, monkeypatch
):
    factory, parent, child, application, observation, proof = await application_case(
        session_factory, tmp_path, monkeypatch
    )
    original = PostgresSchedulingRepository.reconcile_expired

    async def fail_after_wake(self, *args, **kwargs):
        await original(self, *args, **kwargs)
        raise RuntimeError("injected after wake")

    with monkeypatch.context() as patch:
        patch.setattr(PostgresSchedulingRepository, "reconcile_expired", fail_after_wake)
        with pytest.raises(RuntimeError, match="injected"):
            await application.apply_handoff(observation, proof)
    async with factory() as work:
        result = await work.session.get(SubscriptionAttemptResult, child.attempt.attempt_id)
        assert result.disposition == "decision_pending" and result.application_payload is None
        assert (await work.session.get(SubscriptionTask, parent.task.task_id)).state == "blocked"
        assert (
            await work.session.get(SubscriptionScheduledTask, child.task.task_id)
        ).lease_owner is not None
    assert (await application.apply_handoff(observation, proof)).accepted


@pytest.mark.integration
async def test_completion_replay_after_pause_requires_exact_application_receipt(
    session_factory, tmp_path, monkeypatch
):
    factory, _, child, application, observation, proof = await application_case(
        session_factory, tmp_path, monkeypatch
    )
    await application.apply_handoff(observation, proof)
    async with factory() as work:
        run = await work.runs.get(child.task.run_id)
        await work.runs.pause(run.id, run.version, "run.paused", {}, actor_class="operator")
        await work.commit()
    assert (await application.apply_handoff(observation, proof)).replayed
    with pytest.raises(SubscriptionDecisionError, match="replay"):
        await application.apply_handoff(observation, replace(proof, output_digest="0" * 64))


@pytest.mark.integration
async def test_migration_refuses_to_discard_application_evidence(
    session_factory, tmp_path, monkeypatch
):
    import importlib.util
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    factory, _, _, application, observation, proof = await application_case(
        session_factory, tmp_path, monkeypatch
    )
    await application.apply_handoff(observation, proof)
    path = (
        Path(__file__).resolve().parents[2]
        / "migrations/versions/20260911_0017_subscription_application_receipt.py"
    )
    spec = importlib.util.spec_from_file_location("handoff_application_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    def downgrade(connection):
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()

    async with factory() as work:
        connection = await work.session.connection()
        with pytest.raises(RuntimeError, match="must not be discarded"):
            await connection.run_sync(downgrade)


@pytest.mark.integration
@pytest.mark.parametrize("after_snapshot", ["same", "changed", "unavailable"])
async def test_recovery_completes_real_snapshot_handoff_without_mocked_evidence(
    session_factory, tmp_path, monkeypatch, after_snapshot
):
    import asyncio

    from forge.application.ports.subscription_gateway import SubscriptionInvocationResult
    from forge.application.services.subscription_broker import (
        ControlledSubscriptionEffect,
        SubscriptionToolBroker,
    )
    from forge.application.services.subscription_decision_recovery import (
        SubscriptionDecisionRecovery,
    )
    from forge.application.services.subscription_execution import SubscriptionDecisionExecutor
    from forge.application.services.subscription_handoff import SubscriptionHandoffVerifier
    from forge.application.services.subscription_handoff_application import (
        SubscriptionHandoffApplication,
    )
    from forge.application.services.subscription_requests import SubscriptionRequestBuilder
    from forge.application.services.tool_recovery import ToolRecoveryService
    from forge.application.services.tools import ControlledToolService
    from forge.artifacts.filesystem import FilesystemArtifactStore
    from forge.domain.resource import WorktreeIdentity
    from forge.domain.subscription import HandoffStatus, TaskHandoff
    from forge.domain.tool import SubscriptionToolAuthorizationContext, ToolName
    from forge.persistence.models import Run
    from subscription_launch_fixture import record_stopped_launch
    from test_subscription_delegation_application import delegation_case
    from test_subscription_usage import _known, _reservation

    from apps.orchestrator.tests.tools.test_git import _controlled, _git

    repository = tmp_path / "repo"
    repository.mkdir()
    factory, parent, _, _ = await delegation_case(session_factory, repository)
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "user.name", "Forge Test")
    _git(repository, "config", "user.email", "forge@example.test")
    (repository / ".gitignore").write_text("*\n!.gitignore\n!apps/\n!apps/**\n", encoding="utf-8")
    source = repository / "apps" / "feature" / "file.py"
    source.parent.mkdir(parents=True)
    source.write_text("initial\n", encoding="utf-8")
    _git(repository, "add", ".gitignore", "apps/feature/file.py")
    _git(repository, "commit", "-m", "initial")
    git = _controlled(repository, tmp_path / "git-state")
    async with factory() as work:
        run = await work.runs.get(parent.task.run_id)
        identity = WorktreeIdentity.for_run(run.project_id, run.id, run.branch_name, False)
    worktree = git.create_worktree(identity, git.resolve_default_base_sha())
    async with factory() as work:
        row = await work.session.get(Run, parent.task.run_id)
        row.worktree_path, row.base_sha = str(worktree.path), worktree.base_sha
        await work.commit()
    await SubscriptionDecisionApplication(factory).apply_delegation(parent.attempt.attempt_id)
    executor = SubscriptionDecisionExecutor(factory)
    child = await executor.admit_next("real-handoff", _reservation())
    request = await SubscriptionRequestBuilder(factory).build(child)
    authority = request.authorization
    context = SubscriptionToolAuthorizationContext(
        run_id=authority.run_id,
        task_id=authority.task_id,
        attempt_id=authority.attempt_id,
        worktree_id=authority.worktree_id,
        purpose=authority.role,
        policy_version=authority.policy_version,
        permitted_tools=authority.permitted_tools,
    )
    artifacts = FilesystemArtifactStore(tmp_path / "handoff-artifacts")
    tools = ControlledToolService(
        factory, controlled_git=git, worktree=worktree, artifact_store=artifacts
    )
    broker = SubscriptionToolBroker(
        factory,
        lease=child.lease,
        authority=authority,
        effect=ControlledSubscriptionEffect(tools, context),
        owned_paths=child.task.owned_paths,
        expected_candidate_epoch=child.candidate_epoch,
    )
    # The implementation output is seeded; snapshot, receipts, artifacts, source
    # settlement, observation and completion below use real production services.
    (worktree.path / "apps" / "feature" / "file.py").write_text("implemented\n", encoding="utf-8")
    snapshot = await broker.invoke(
        token=authority.broker_token,
        provider_call_key="handoff-snapshot",
        tool_name=ToolName.GIT_DIFF,
        arguments={"scope": "snapshot"},
    )
    assert snapshot.accepted and snapshot.result["status"] == "succeeded"
    handoff = TaskHandoff(
        run_id=child.task.run_id,
        task_id=child.task.task_id,
        attempt_id=child.attempt.attempt_id,
        status=HandoffStatus.COMPLETED,
        changed_paths=("apps/feature/file.py",),
        candidate_tree_digest=snapshot.result["metadata"]["candidate_tree_digest"],
        evidence_receipt_ids=(str(snapshot.operation_id),),
    )
    stopped = await record_stopped_launch(session_factory, child)
    await executor.settle(
        child,
        SubscriptionInvocationResult(
            attempt=child.attempt,
            decision=handoff,
            telemetry=_known(),
            launch_proof=stopped,
        ),
    )
    if after_snapshot == "changed":
        (worktree.path / "apps" / "feature" / "file.py").write_text(
            "later output\n", encoding="utf-8"
        )

    async def capture(proposal):
        return await asyncio.to_thread(
            git.working_tree_snapshot,
            proposal.worktree,
            secret_paths=proposal.policy.effective_secret_paths,
        )

    service = SubscriptionHandoffApplication(
        factory,
        SubscriptionHandoffVerifier(factory, artifacts, ToolRecoveryService(factory, artifacts)),
        capture,
    )
    recovery = SubscriptionDecisionRecovery(factory, artifacts, handoffs=service)
    if after_snapshot == "unavailable":

        async def unavailable(*args, **kwargs):
            raise OSError("storage temporarily unavailable")

        with monkeypatch.context() as patch:
            patch.setattr(artifacts, "open_bytes", unavailable)
            deferred = await recovery.reconcile_all()
        assert (deferred.applied, deferred.deferred) == (0, 1)
        async with factory() as work:
            stored = await work.session.get(SubscriptionAttemptResult, child.attempt.attempt_id)
            assert stored.disposition == "decision_pending" and stored.application_payload is None
            assert (await work.subscription_budget.usage(child.task.run_id)).consumed.repairs == 0
    report = await recovery.reconcile_all()
    assert (report.applied, report.deferred, report.unsupported) == (1, 0, 0)
    assert (await recovery.reconcile_all()).applied == 0
    assert (await service.apply(child.attempt.attempt_id)).replayed
    async with factory() as work:
        stored = await work.session.get(SubscriptionAttemptResult, child.attempt.attempt_id)
        if after_snapshot == "changed":
            assert not stored.accepted and stored.disposition == "handoff_rejected"
            assert stored.application_payload["rejection_reason"] == "outputs_changed"
        else:
            assert stored.accepted and stored.disposition == "handoff_completed"
            assert (
                stored.application_payload["current_tree_digest"] == handoff.candidate_tree_digest
            )
        assert (await work.session.get(SubscriptionTask, parent.task.task_id)).state == "queued"
