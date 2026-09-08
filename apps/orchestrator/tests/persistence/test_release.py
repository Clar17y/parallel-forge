from dataclasses import asdict, replace
from uuid import uuid4

import pytest
from forge.domain.operation import OperationOutcome, canonical_digest
from forge.domain.release import GitHubPullRequest
from forge.persistence.models import Approval, PullRequest
from forge.persistence.repositories.release import PostgresReleaseRepository, ReleaseRecordConflict
from sqlalchemy.exc import IntegrityError

from apps.orchestrator.tests.release.test_publication_operations import publication


async def prepared(work, run_id):
    candidate = replace(publication(), run_id=run_id)
    pull = GitHubPullRequest(
        1,
        "PR_1",
        "https://github.com/owner/repo/pull/1",
        "owner/repo",
        candidate.branch,
        "a" * 40,
        "owner/repo",
        "main",
        "b" * 40,
        "open",
        False,
        None,
    )
    ids = []
    for kind in ("push_branch", "create_pr"):
        request = candidate.request(kind)
        row = await work.operations.begin(
            run_id=run_id,
            operation_type=kind,
            idempotency_key=request.idempotency_key,
            request_digest=request.request_digest,
            request_payload=request.request_payload,
            execution_owner="test-publication",
            execution_lease_seconds=30,
        )
        payload = (
            asdict(pull)
            if kind == "create_pr"
            else {
                "repository": "owner/repo",
                "branch": candidate.branch,
                "head_sha": "a" * 40,
            }
        )
        await work.operations.complete(
            row.id,
            OperationOutcome(
                remote_resource_id="PR_1" if kind == "create_pr" else None,
                payload=payload,
            ),
            owner_id="test-publication",
        )
        ids.append(row.id)
    return pull, ids


@pytest.mark.integration
async def test_release_identity_and_receipts_persist_together_and_replay(uow, persisted_run):
    async with uow as work:
        pull, ids = await prepared(work, persisted_run.id)
        releases = PostgresReleaseRepository(work.session)
        recorded = await releases.record_publication(persisted_run.id, pull, *ids)
        assert recorded.pull_request == pull
        await work.commit()
    async with uow as work:
        replay = await work.releases.record_publication(persisted_run.id, pull, *ids)
        assert replay == recorded
        assert await work.releases.get_for_run(persisted_run.id) == recorded
        await work.commit()


@pytest.mark.integration
async def test_release_cannot_substitute_receipt_identity(uow, persisted_run):
    async with uow as work:
        pull, ids = await prepared(work, persisted_run.id)
        releases = PostgresReleaseRepository(work.session)
        with pytest.raises(ReleaseRecordConflict):
            await releases.record_publication(
                persisted_run.id, replace(pull, head_sha="e" * 40), *ids
            )
        assert await releases.get_for_run(persisted_run.id) is None


@pytest.mark.integration
async def test_reviewed_push_preserves_publication_receipts_and_replays(uow, persisted_run):
    async with uow as work:
        pull, ids = await prepared(work, persisted_run.id)
        original = await work.releases.record_publication(persisted_run.id, pull, *ids)
        with pytest.raises(IntegrityError), work.session.no_autoflush:
            async with work.session.begin_nested():
                row = await work.session.get(PullRequest, original.id)
                row.candidate_evidence_digest = "d" * 64
                await work.session.flush()
        publication_intent = await work.operations.get(ids[1])
        updated = replace(pull, head_sha="c" * 40)
        payload = dict(publication_intent.request_payload) | {
            "head_sha": updated.head_sha,
            "previous_head_sha": pull.head_sha,
            "candidate_evidence_digest": "d" * 64,
            "pull_request_id": str(original.id),
            "pull_request_number": pull.number,
            "node_id": pull.node_id,
        }
        digest = canonical_digest(payload)
        intent = await work.operations.begin(
            run_id=persisted_run.id,
            operation_type="push_branch",
            idempotency_key=f"{persisted_run.id}:push-reviewed:{digest}",
            request_digest=digest,
            request_payload=payload,
            execution_owner="reviewed-push",
            execution_lease_seconds=30,
        )
        with pytest.raises(ReleaseRecordConflict):
            await work.releases.record_reviewed_push(persisted_run.id, updated, intent.id)
        await work.operations.complete(
            intent.id,
            OperationOutcome(remote_resource_id=pull.node_id, payload=asdict(updated)),
            owner_id="reviewed-push",
        )
        changed = await work.releases.record_reviewed_push(persisted_run.id, updated, intent.id)
        assert changed.push_intent_id == original.push_intent_id
        assert changed.publication_intent_id == original.publication_intent_id
        assert changed.reviewed_push_intent_id == intent.id
        assert changed.candidate_evidence_digest == "d" * 64
        await work.commit()
    async with uow as work:
        assert (
            await work.releases.record_reviewed_push(persisted_run.id, updated, intent.id)
            == changed
        )
        with pytest.raises(ReleaseRecordConflict):
            await work.releases.record_reviewed_push(
                persisted_run.id, replace(updated, node_id="other"), intent.id
            )


@pytest.mark.integration
@pytest.mark.parametrize(
    "defect", [None, "extra_field", "approval_digest", "base_sha", "node_id", "pending"]
)
async def test_merge_receipt_requires_exact_authority_and_settled_effect(
    uow, persisted_run, defect
):
    async with uow as work:
        pull, ids = await prepared(work, persisted_run.id)
        original = await work.releases.record_publication(persisted_run.id, pull, *ids)
        approval = Approval(
            id=uuid4(),
            run_id=persisted_run.id,
            gate="merge",
            evidence_digest="d" * 64,
            run_version=1,
            policy_version=persisted_run.policy_version,
            authenticated_actor_id=uuid4(),
        )
        work.session.add(approval)
        await work.session.flush()
        merged = replace(pull, state="closed", merged=True, merge_sha="e" * 40, base_sha="f" * 40)
        payload = {
            "approval_id": str(approval.id),
            "approval_digest": approval.evidence_digest,
            "policy_version": approval.policy_version,
            "repository": pull.base_repository,
            "pull_request_number": pull.number,
            "node_id": pull.node_id,
            "head_sha": pull.head_sha,
            "base_ref": "refs/heads/main",
            "base_sha": pull.base_sha,
            "merge_method": "squash",
        }
        if defect == "extra_field":
            payload["unapproved_option"] = True
        elif defect == "approval_digest":
            payload["approval_digest"] = "c" * 64
        elif defect == "base_sha":
            payload["base_sha"] = "c" * 40
        elif defect == "node_id":
            payload["node_id"] = "PR_other"
        digest = canonical_digest(payload)
        intent = await work.operations.begin(
            run_id=persisted_run.id,
            operation_type="merge_pr",
            idempotency_key=f"{persisted_run.id}:merge_pr:{digest}",
            request_digest=digest,
            request_payload=payload,
            execution_owner="merger",
            execution_lease_seconds=30,
        )
        if defect != "pending":
            await work.operations.complete(
                intent.id,
                OperationOutcome(remote_resource_id=pull.node_id, payload=asdict(merged)),
                owner_id="merger",
            )
        if defect is not None:
            with pytest.raises(ReleaseRecordConflict):
                await work.releases.record_merge(persisted_run.id, merged, intent.id)
            assert await work.releases.get_for_run(persisted_run.id) == original
        else:
            recorded = await work.releases.record_merge(persisted_run.id, merged, intent.id)
            assert recorded.pull_request == merged
            assert recorded.merge_intent_id == intent.id
            assert recorded.publication_intent_id == original.publication_intent_id
            receipt = await work.operations.get(intent.id)
            row = await work.session.get(PullRequest, recorded.id)
            assert row.merged_at == receipt.completed_at
            assert await work.releases.record_merge(persisted_run.id, merged, intent.id) == recorded
            assert row.merged_at == receipt.completed_at
