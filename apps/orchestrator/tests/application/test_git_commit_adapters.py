from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from forge.application.adapters.git_commit import (
    PREPARE_GIT_COMMIT_KIND,
    PUBLISH_GIT_COMMIT_KIND,
    GitCommitOperationError,
    PrepareGitCommitAdapter,
    PublishGitCommitAdapter,
)
from forge.application.ports.worktrees import (
    ManagedWorktree,
    PreparedGitCommit,
    PublishedGitCommit,
)
from forge.domain.operation import OperationIntent, OperationStatus, canonical_digest
from forge.domain.resource import WorktreeIdentity


class _Git:
    def __init__(self, worktree: ManagedWorktree) -> None:
        self.worktree = worktree
        self.prepare_calls = 0
        self.publish_calls = 0
        self.inspect_calls = 0
        self.published: PublishedGitCommit | None = None

    def prepare_commit(self, worktree: ManagedWorktree, message: str) -> PreparedGitCommit:
        self.prepare_calls += 1
        return PreparedGitCommit(
            worktree_identity=worktree.identity,
            previous_sha="a" * 40,
            tree_sha="b" * 40,
            message=message,
        )

    def commit_prepared(
        self, worktree: ManagedWorktree, prepared: PreparedGitCommit
    ) -> PublishedGitCommit:
        self.publish_calls += 1
        self.published = PublishedGitCommit(
            worktree_identity=worktree.identity,
            previous_sha=prepared.previous_sha,
            tree_sha=prepared.tree_sha,
            new_sha="c" * 40,
            message=prepared.message,
        )
        return self.published

    def inspect_prepared_commit(
        self, worktree: ManagedWorktree, prepared: PreparedGitCommit
    ) -> PublishedGitCommit | None:
        self.inspect_calls += 1
        return self.published


class _Reader:
    def __init__(self, intent: OperationIntent) -> None:
        self.intent = intent

    async def get(self, intent_id: UUID) -> OperationIntent:
        assert intent_id == self.intent.id
        return self.intent


def _worktree(run_id: UUID) -> ManagedWorktree:
    return ManagedWorktree(
        identity=WorktreeIdentity.for_run(uuid4(), run_id, "forge/test", False),
        path=Path("/managed/forge-test").resolve(),
        base_sha="a" * 40,
    )


def _payload(worktree: ManagedWorktree, run_id: UUID) -> dict[str, object]:
    message = "prepare commit"
    return {
        "agent_execution_id": str(uuid4()),
        "base_sha": worktree.base_sha,
        "message": message,
        "message_digest": hashlib.sha256(message.encode()).hexdigest(),
        "policy_version": 1,
        "request_digest": canonical_digest({"message": message}),
        "run_id": str(run_id),
        "step_id": str(uuid4()),
        "tool_call_id": str(uuid4()),
        "worktree_id": worktree.identity.worktree_name,
    }


def _intent(kind: str, run_id: UUID, payload: dict[str, object]) -> OperationIntent:
    return OperationIntent(
        run_id=run_id,
        kind=kind,
        idempotency_key=f"{kind}:{uuid4()}",
        request_digest=canonical_digest(payload),
        request_payload=payload,
    )


def _succeeded_preparation(
    worktree: ManagedWorktree, run_id: UUID
) -> tuple[OperationIntent, dict[str, object]]:
    payload = _payload(worktree, run_id)
    prepared = _intent(PREPARE_GIT_COMMIT_KIND, run_id, payload)
    receipt = {key: value for key, value in payload.items() if key != "message"} | {
        "preparation_intent_id": str(prepared.id),
        "previous_sha": "a" * 40,
        "tree_sha": "b" * 40,
    }
    return (
        OperationIntent(
            id=prepared.id,
            run_id=prepared.run_id,
            kind=prepared.kind,
            idempotency_key=prepared.idempotency_key,
            request_digest=prepared.request_digest,
            request_payload=prepared.request_payload,
            status=OperationStatus.SUCCEEDED,
            outcome=receipt,
            outcome_schema_version=1,
            completed_at=datetime.now(UTC),
        ),
        payload,
    )


def _publication_intent(
    worktree: ManagedWorktree,
    run_id: UUID,
    preparation: OperationIntent,
    preparation_payload: dict[str, object],
) -> OperationIntent:
    payload = preparation_payload | {
        "preparation_intent_id": str(preparation.id),
        "previous_sha": "a" * 40,
        "tree_sha": "b" * 40,
    }
    return _intent(PUBLISH_GIT_COMMIT_KIND, run_id, payload)


@pytest.mark.asyncio
async def test_prepare_calls_typed_git_and_returns_exact_authority_bound_receipt() -> None:
    run_id = uuid4()
    worktree = _worktree(run_id)
    payload = _payload(worktree, run_id)
    intent = _intent(PREPARE_GIT_COMMIT_KIND, run_id, payload)
    git = _Git(worktree)

    outcome = await PrepareGitCommitAdapter(git, worktree).invoke(intent)

    assert git.prepare_calls == 1
    assert outcome.status is OperationStatus.SUCCEEDED
    assert outcome.payload == {key: value for key, value in payload.items() if key != "message"} | {
        "preparation_intent_id": str(intent.id),
        "previous_sha": "a" * 40,
        "tree_sha": "b" * 40,
    }


@pytest.mark.asyncio
async def test_mismatched_worktree_or_request_digest_has_zero_prepare_effects() -> None:
    run_id = uuid4()
    worktree = _worktree(run_id)
    payload = _payload(worktree, run_id)
    payload["worktree_id"] = "other-worktree"
    intent = _intent(PREPARE_GIT_COMMIT_KIND, run_id, payload)
    git = _Git(worktree)

    with pytest.raises(GitCommitOperationError):
        await PrepareGitCommitAdapter(git, worktree).invoke(intent)

    assert git.prepare_calls == 0


@pytest.mark.asyncio
async def test_foreign_run_intent_cannot_prepare_the_bound_worktree() -> None:
    bound_run_id = uuid4()
    worktree = _worktree(bound_run_id)
    foreign_run_id = uuid4()
    intent = _intent(PREPARE_GIT_COMMIT_KIND, foreign_run_id, _payload(worktree, foreign_run_id))
    git = _Git(worktree)

    with pytest.raises(GitCommitOperationError):
        await PrepareGitCommitAdapter(git, worktree).invoke(intent)

    assert git.prepare_calls == 0


@pytest.mark.asyncio
async def test_nil_authority_ids_cannot_prepare_before_git_effect() -> None:
    run_id = uuid4()
    worktree = _worktree(run_id)
    payload = _payload(worktree, run_id)
    payload["tool_call_id"] = str(UUID(int=0))
    git = _Git(worktree)

    with pytest.raises(GitCommitOperationError):
        await PrepareGitCommitAdapter(git, worktree).invoke(
            _intent(PREPARE_GIT_COMMIT_KIND, run_id, payload)
        )

    assert git.prepare_calls == 0


@pytest.mark.asyncio
async def test_publish_and_reconcile_return_exact_receipt_without_repeat_publication() -> None:
    run_id = uuid4()
    worktree = _worktree(run_id)
    preparation, preparation_payload = _succeeded_preparation(worktree, run_id)
    intent = _publication_intent(worktree, run_id, preparation, preparation_payload)
    git = _Git(worktree)
    adapter = PublishGitCommitAdapter(git, worktree, _Reader(preparation))

    published = await adapter.invoke(intent)
    reconciled = await adapter.reconcile(intent)

    expected = {key: value for key, value in intent.request_payload.items() if key != "message"} | {
        "new_sha": "c" * 40
    }
    assert published.status is OperationStatus.SUCCEEDED
    assert published.payload == expected
    assert reconciled == published
    assert git.publish_calls == 1
    assert git.inspect_calls == 1


@pytest.mark.asyncio
async def test_publish_reconcile_without_receipt_needs_reconciliation_without_publication() -> None:
    run_id = uuid4()
    worktree = _worktree(run_id)
    preparation, preparation_payload = _succeeded_preparation(worktree, run_id)
    intent = _publication_intent(worktree, run_id, preparation, preparation_payload)
    git = _Git(worktree)

    outcome = await PublishGitCommitAdapter(git, worktree, _Reader(preparation)).reconcile(intent)

    assert outcome.status is OperationStatus.NEEDS_RECONCILIATION
    assert git.publish_calls == 0
    assert git.inspect_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ("tree_sha", "previous_sha", "message"))
async def test_publish_rejects_mismatched_published_receipt(field: str) -> None:
    run_id = uuid4()
    worktree = _worktree(run_id)
    preparation, preparation_payload = _succeeded_preparation(worktree, run_id)
    intent = _publication_intent(worktree, run_id, preparation, preparation_payload)
    git = _Git(worktree)
    original_publish = git.commit_prepared

    def mismatched_publish(
        received_worktree: ManagedWorktree, prepared: PreparedGitCommit
    ) -> PublishedGitCommit:
        published = original_publish(received_worktree, prepared)
        if field == "message":
            return PublishedGitCommit(
                worktree_identity=published.worktree_identity,
                previous_sha=published.previous_sha,
                tree_sha=published.tree_sha,
                new_sha=published.new_sha,
                message="different message",
            )
        return PublishedGitCommit(
            worktree_identity=published.worktree_identity,
            previous_sha=("d" * 40 if field == "previous_sha" else published.previous_sha),
            tree_sha="d" * 40 if field == "tree_sha" else published.tree_sha,
            new_sha=published.new_sha,
            message=published.message,
        )

    git.commit_prepared = mismatched_publish  # type: ignore[method-assign]
    with pytest.raises(GitCommitOperationError):
        await PublishGitCommitAdapter(git, worktree, _Reader(preparation)).invoke(intent)

    assert git.publish_calls == 1


@pytest.mark.asyncio
async def test_publish_rejects_preparation_receipt_with_mismatched_authority_binding() -> None:
    run_id = uuid4()
    worktree = _worktree(run_id)
    preparation_payload = _payload(worktree, run_id)
    preparation = _intent(PREPARE_GIT_COMMIT_KIND, run_id, preparation_payload)
    receipt = {key: value for key, value in preparation_payload.items() if key != "message"} | {
        "preparation_intent_id": str(preparation.id),
        "previous_sha": "a" * 40,
        "tree_sha": "b" * 40,
    }
    receipt["tool_call_id"] = str(uuid4())
    preparation = OperationIntent(
        id=preparation.id,
        run_id=preparation.run_id,
        kind=preparation.kind,
        idempotency_key=preparation.idempotency_key,
        request_digest=preparation.request_digest,
        request_payload=preparation.request_payload,
        status=OperationStatus.SUCCEEDED,
        outcome=receipt,
        outcome_schema_version=1,
        completed_at=datetime.now(UTC),
    )
    publication_payload = _payload(worktree, run_id) | {
        "preparation_intent_id": str(preparation.id),
        "previous_sha": "a" * 40,
        "tree_sha": "b" * 40,
    }
    for key in (
        "agent_execution_id",
        "policy_version",
        "request_digest",
        "step_id",
        "tool_call_id",
    ):
        publication_payload[key] = preparation_payload[key]
    intent = _intent(PUBLISH_GIT_COMMIT_KIND, run_id, publication_payload)
    git = _Git(worktree)

    with pytest.raises(GitCommitOperationError):
        await PublishGitCommitAdapter(git, worktree, _Reader(preparation)).invoke(intent)

    assert git.publish_calls == 0


@pytest.mark.asyncio
async def test_publish_rejects_paired_receipt_and_publication_authority_tamper() -> None:
    run_id = uuid4()
    worktree = _worktree(run_id)
    preparation_payload = _payload(worktree, run_id)
    preparation = _intent(PREPARE_GIT_COMMIT_KIND, run_id, preparation_payload)
    tampered_tool_call_id = str(uuid4())
    receipt = {key: value for key, value in preparation_payload.items() if key != "message"} | {
        "preparation_intent_id": str(preparation.id),
        "previous_sha": "a" * 40,
        "tree_sha": "b" * 40,
        "tool_call_id": tampered_tool_call_id,
    }
    preparation = OperationIntent(
        id=preparation.id,
        run_id=preparation.run_id,
        kind=preparation.kind,
        idempotency_key=preparation.idempotency_key,
        request_digest=preparation.request_digest,
        request_payload=preparation.request_payload,
        status=OperationStatus.SUCCEEDED,
        outcome=receipt,
        outcome_schema_version=1,
        completed_at=datetime.now(UTC),
    )
    publication_payload = _payload(worktree, run_id) | {
        "preparation_intent_id": str(preparation.id),
        "previous_sha": "a" * 40,
        "tree_sha": "b" * 40,
        "tool_call_id": tampered_tool_call_id,
    }
    for key in ("agent_execution_id", "policy_version", "request_digest", "step_id"):
        publication_payload[key] = preparation_payload[key]
    git = _Git(worktree)

    with pytest.raises(GitCommitOperationError):
        await PublishGitCommitAdapter(git, worktree, _Reader(preparation)).invoke(
            _intent(PUBLISH_GIT_COMMIT_KIND, run_id, publication_payload)
        )

    assert git.publish_calls == 0
