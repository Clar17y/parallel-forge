"""Real broker snapshots survive worker completion and primary candidate selection."""

import asyncio
from dataclasses import replace

import pytest
from forge.application.ports.subscription_gateway import SubscriptionInvocationResult
from forge.application.services.subscription_acceptance_receipts import (
    SubscriptionAcceptanceReceiptVerification,
)
from forge.application.services.subscription_broker import (
    ControlledSubscriptionEffect,
    SubscriptionToolBroker,
)
from forge.application.services.subscription_candidate import SubscriptionCandidateInspection
from forge.application.services.subscription_decisions import SubscriptionDecisionApplication
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
from forge.domain.subscription import (
    AcceptDecision,
    HandoffStatus,
    ReviewSelection,
    TaskBudget,
    TaskHandoff,
    encode_subscription_record,
)
from forge.domain.tool import SubscriptionToolAuthorizationContext, ToolName
from forge.persistence.models import Run
from forge.persistence.models.subscription import SubscriptionTask
from subscription_launch_fixture import record_stopped_launch
from test_scheduler_acceptance import _remove_disposable_subscription_rows  # noqa: F401
from test_subscription_delegation_application import delegation_case
from test_subscription_usage import _known, _reservation

from apps.orchestrator.tests.tools.test_git import _controlled, _git


async def retained_receipts_case(
    session_factory, tmp_path
):
    repository = tmp_path / "repo"
    repository.mkdir()
    factory, parent, _, _ = await delegation_case(
        session_factory, repository, primary_budget=TaskBudget(max_provider_attempts=8)
    )
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "user.name", "Forge Test")
    _git(repository, "config", "user.email", "forge@example.test")
    (repository / ".gitignore").write_text(".worktrees/\n", encoding="utf-8")
    (repository / "apps" / "feature").mkdir(parents=True, exist_ok=True)
    (repository / "apps" / "feature" / "file.py").write_text("initial\n", encoding="utf-8")
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
    application = SubscriptionDecisionApplication(factory)
    executor = SubscriptionDecisionExecutor(factory)
    artifacts = FilesystemArtifactStore(tmp_path / "artifacts")
    terminal = ToolRecoveryService(factory, artifacts)
    tools = ControlledToolService(
        factory, controlled_git=git, worktree=worktree, artifact_store=artifacts
    )

    async def receipt(admission):
        request = await SubscriptionRequestBuilder(factory).build(admission)
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
        broker = SubscriptionToolBroker(
            factory,
            lease=admission.lease,
            authority=authority,
            effect=ControlledSubscriptionEffect(tools, context),
            owned_paths=admission.task.owned_paths,
            expected_candidate_epoch=admission.candidate_epoch,
        )
        outcome = await broker.invoke(
            token=authority.broker_token,
            provider_call_key="snapshot",
            tool_name=ToolName.GIT_DIFF,
            arguments={"scope": "snapshot"},
        )
        assert outcome.accepted and outcome.result["status"] == "succeeded"
        return outcome

    async def settle(admission, decision):
        result = await executor.settle(
            admission,
            SubscriptionInvocationResult(
                attempt=admission.attempt,
                decision=decision,
                telemetry=_known(),
                launch_proof=await record_stopped_launch(session_factory, admission),
            ),
        )
        assert result.disposition == "decision_pending"

    async def capture(proposal):
        return await asyncio.to_thread(
            git.working_tree_snapshot,
            proposal.worktree,
            secret_paths=proposal.policy.effective_secret_paths,
        )

    await application.apply_delegation(parent.attempt.attempt_id)
    worker = await executor.admit_next("worker", _reservation())
    (worktree.path / "apps" / "feature" / "file.py").write_text("implemented\n", encoding="utf-8")
    worker_receipt = await receipt(worker)
    tree_digest = worker_receipt.result["metadata"]["candidate_tree_digest"]
    await settle(
        worker,
        TaskHandoff(
            run_id=worker.task.run_id,
            task_id=worker.task.task_id,
            attempt_id=worker.attempt.attempt_id,
            status=HandoffStatus.COMPLETED,
            changed_paths=("apps/feature/file.py",),
            candidate_tree_digest=tree_digest,
            evidence_receipt_ids=(str(worker_receipt.operation_id),),
        ),
    )
    handoffs = SubscriptionHandoffApplication(
        factory,
        SubscriptionHandoffVerifier(factory, artifacts, terminal),
        capture,
    )
    assert (await handoffs.apply(worker.attempt.attempt_id)).disposition == "handoff_completed"
    selecting = await executor.admit_next("primary-selects", _reservation())
    primary_receipt = await receipt(selecting)
    await settle(
        selecting,
        ReviewSelection(
            run_id=worker.task.run_id,
            candidate_tree_digest=tree_digest,
            review_required=False,
            candidate_commit=None,
            no_review_reason="Focused trivial change; retain exact candidate evidence",
        ),
    )
    await SubscriptionCandidateInspection(factory, capture).inspect(selecting.attempt.attempt_id)
    await application.finalize_review_selection(selecting.attempt.attempt_id)
    accepting = await executor.admit_next("primary-accepts", _reservation())
    await settle(
        accepting,
        AcceptDecision(
            run_id=worker.task.run_id,
            task_id=parent.task.task_id,
            candidate_tree_digest=tree_digest,
            candidate_commit=None,
            evidence_receipt_ids=(
                str(worker_receipt.operation_id),
                str(primary_receipt.operation_id),
            ),
            rationale="Accept the integrated candidate with retained producer evidence",
        ),
    )
    service = SubscriptionAcceptanceReceiptVerification(factory, artifacts, terminal)
    proof = await service.verify(accepting.attempt.attempt_id)
    assert proof is not None
    assert [(item.task_id, item.attempt_id) for item in proof.receipts] == [
        (worker.task.task_id, worker.attempt.attempt_id),
        (parent.task.task_id, selecting.attempt.attempt_id),
    ]
    assert all(item.matches_candidate for item in proof.receipts)
    # Retained receipts use the producer's frozen contract, never a task revision.
    async with factory() as work:
        row = await work.session.get(SubscriptionTask, worker.task.task_id)
        row.payload = encode_subscription_record(replace(worker.task, typed_acceptance=()))
        row.version += 1
        await work.commit()
    assert await service.verify(accepting.attempt.attempt_id) == proof
    return factory, parent, worker, proof


@pytest.mark.integration
async def test_final_receipts_retain_real_worker_and_earlier_primary_attempts(
    session_factory, tmp_path
):
    await retained_receipts_case(session_factory, tmp_path)
