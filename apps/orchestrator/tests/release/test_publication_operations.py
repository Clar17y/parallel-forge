from dataclasses import replace
from hashlib import sha256
from uuid import uuid4

import pytest
from forge.domain.approval import PrApprovalEvidence
from forge.domain.operation import OperationIntent, OperationStatus
from forge.domain.policy import RunnerMode
from forge.release.controller import (
    Publication,
    PullRequestOperation,
    ReleaseReconciliationRequired,
)
from forge.release.fake_github_write import FakeGitHubWrite, FakeGitHubWriteCrash


def publication():
    return Publication(
        run_id=uuid4(),
        approval_id=uuid4(),
        policy_version=1,
        branch="forge/run-1",
        evidence=PrApprovalEvidence(
            candidate_commit="a" * 40,
            diff_digest="d" * 64,
            validation_digest="e" * 64,
            review_digest="f" * 64,
            repository="owner/repo",
            base_ref="refs/heads/main",
            base_sha="b" * 40,
            title="Approved task",
            body_digest=sha256(b"Approved body").hexdigest(),
            runner_mode=RunnerMode.DOCKER,
            runner_evidence_digest="c" * 64,
            remote_remediation_limit=3,
        ),
    )


def intent(request):
    return OperationIntent(
        run_id=request.run_id,
        kind=request.kind,
        idempotency_key=request.idempotency_key,
        request_digest=request.request_digest,
        request_payload=request.request_payload,
    )


async def test_crash_after_creation_adopts_exact_pr_without_second_write():
    candidate = publication()
    github = FakeGitHubWrite()
    github.branch_shas["owner/repo", candidate.branch] = "a" * 40
    github.branch_shas["owner/repo", "main"] = "b" * 40
    adapter = PullRequestOperation(candidate, github, b"Approved body")
    admitted = intent(adapter.request)
    github.crash_next_write = True
    with pytest.raises(FakeGitHubWriteCrash):
        await adapter.invoke(admitted)
    recovered = await adapter.reconcile(admitted)
    assert recovered.status is OperationStatus.SUCCEEDED
    assert recovered.payload["number"] == 1
    assert recovered.payload["node_id"] == "PR_1"
    assert len(github.pull_requests) == 1


async def test_reconciliation_with_no_remote_match_never_creates_pr():
    adapter = PullRequestOperation(publication(), FakeGitHubWrite(), b"Approved body")
    with pytest.raises(ReleaseReconciliationRequired):
        await adapter.reconcile(intent(adapter.request))


@pytest.mark.parametrize("mutation", ["closed", "head", "base", "duplicate"])
async def test_ambiguous_or_drifted_existing_pr_is_not_adopted(mutation):
    candidate = publication()
    github = FakeGitHubWrite()
    github.branch_shas["owner/repo", candidate.branch] = "a" * 40
    github.branch_shas["owner/repo", "main"] = "b" * 40
    adapter = PullRequestOperation(candidate, github, b"Approved body")
    admitted = intent(adapter.request)
    await adapter.invoke(admitted)
    pr = github.pull_requests["owner/repo", 1]
    if mutation == "duplicate":
        github.pull_requests["owner/repo", 2] = replace(pr, number=2, node_id="PR_2")
    else:
        changes = {
            "closed": {"state": "closed"},
            "head": {"head_sha": "c" * 40},
            "base": {"base_sha": "c" * 40},
        }
        github.pull_requests["owner/repo", 1] = replace(pr, **changes[mutation])
    with pytest.raises(ReleaseReconciliationRequired):
        await adapter.reconcile(admitted)


async def test_intent_or_body_substitution_is_rejected():
    candidate = publication()
    github = FakeGitHubWrite()
    with pytest.raises(ReleaseReconciliationRequired):
        PullRequestOperation(candidate, github, b"different")
    adapter = PullRequestOperation(candidate, github, b"Approved body")
    with pytest.raises(ReleaseReconciliationRequired):
        await adapter.invoke(replace(intent(adapter.request), run_id=uuid4()))
    assert not github.pull_requests
