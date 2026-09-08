from dataclasses import replace
from uuid import uuid4

import pytest
from forge.domain.approval import canonical_digest
from forge.release.controller import ReleaseReconciliationRequired, ReviewedPushOperation
from forge.release.fake_github_write import FakeGitHubWrite

from apps.orchestrator.tests.release.test_base_adoption_operation import prepared
from apps.orchestrator.tests.release.test_publication_operations import intent, publication


@pytest.mark.parametrize("drift", [None, "missing_adoption", "remote_base"])
async def test_reviewed_push_after_adoption_preserves_original_evidence_base(drift):
    record, receipt, tree, policy = prepared()
    pull = replace(record.pull_request, head_sha="e" * 40, base_sha="c" * 40)
    record = replace(
        record,
        pull_request=pull,
        base_update_intent_id=receipt.id,
        base_adoption_intent_id=None if drift == "missing_adoption" else uuid4(),
    )
    evidence = publication().evidence.model_copy(
        update={
            "repository": policy.github_repository,
            "base_sha": tree.base_sha,
            "remote_remediation_limit": policy.remote_remediation_limit,
        }
    )
    github = FakeGitHubWrite()
    github.pull_requests[policy.github_repository, pull.number] = pull
    github.branch_shas[policy.github_repository, pull.head_ref] = pull.head_sha
    github.branch_shas[policy.github_repository, pull.base_ref] = (
        tree.base_sha if drift == "remote_base" else pull.base_sha
    )
    pushes = []

    class Push:
        async def push(self, worktree, policy, head):
            pushes.append(head)
            github.branch_shas[policy.github_repository, pull.head_ref] = head
            github.pull_requests[policy.github_repository, pull.number] = replace(
                pull, head_sha=head
            )

    if drift == "missing_adoption":
        with pytest.raises(ReleaseReconciliationRequired):
            ReviewedPushOperation(record, uuid4(), "d" * 64, evidence, github, Push(), tree, policy)
        return
    operation = ReviewedPushOperation(
        record, uuid4(), "d" * 64, evidence, github, Push(), tree, policy
    )
    assert operation.request.request_payload["base_sha"] == pull.base_sha
    assert operation.request.request_payload["candidate_evidence_digest"] == canonical_digest(
        evidence
    )
    if drift:
        with pytest.raises(ReleaseReconciliationRequired):
            await operation.invoke(intent(operation.request))
        assert not pushes
        return
    outcome = await operation.invoke(intent(operation.request))
    assert outcome.payload["base_sha"] == pull.base_sha
    assert outcome.payload["head_sha"] == evidence.candidate_commit
    assert await operation.reconcile(intent(operation.request)) == outcome
    assert pushes == [evidence.candidate_commit] and evidence.base_sha == tree.base_sha
