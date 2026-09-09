from dataclasses import asdict, replace
from uuid import uuid4

import pytest
from forge.application.ports.release import ReleaseRecord
from forge.domain.github import CheckSnapshot, MergeProtection
from forge.domain.operation import OperationStatus, canonical_digest
from forge.domain.release import GitHubPullRequest
from forge.release.fake_github import FakeGitHub
from forge.release.fake_github_write import FakeGitHubWrite, FakeGitHubWriteCrash
from forge.release.github_client import GitHubClientError
from forge.release.github_write import GitHubWriteError
from forge.release.merge import MergeController, MergeOperation, StaleMergeEvidence

from apps.orchestrator.tests.domain.test_approval import merge_evidence
from apps.orchestrator.tests.release.test_publication_operations import intent


def ready():
    read, write = FakeGitHub(), FakeGitHubWrite()
    protection = MergeProtection(True, False, False, "strict", True, ("test", "typecheck"))
    evidence = merge_evidence(protection_digest=canonical_digest(asdict(protection)))
    repository = evidence.repository
    pr = GitHubPullRequest(
        42,
        "PR_42",
        f"https://github.com/{repository}/pull/42",
        repository,
        "forge/run",
        "a" * 40,
        repository,
        "main",
        "b" * 40,
        "open",
        False,
        None,
    )
    write.pull_requests[repository, 42] = pr
    write.branch_shas[repository, "forge/run"] = "a" * 40
    write.branch_shas[repository, "main"] = "b" * 40
    read.checks[repository.casefold(), "a" * 40] = tuple(
        CheckSnapshot(name, "completed", "success", head_sha="a" * 40)
        for name in evidence.required_checks
    )
    read.bases[repository.casefold(), "main"] = "b" * 40
    read.merge_protections[repository.casefold(), "main"] = protection
    record = ReleaseRecord(uuid4(), uuid4(), pr, uuid4(), uuid4(), None)
    return read, write, evidence, record


async def test_preflight_rejects_new_required_context_absent_from_observation():
    read, write, evidence, record = ready()
    protection = MergeProtection(True, False, False, "strict", True, ("missing-ci",))
    read.merge_protections[evidence.repository.casefold(), "main"] = protection
    evidence = evidence.model_copy(
        update={"protection_digest": canonical_digest(asdict(protection))}
    )
    with pytest.raises(StaleMergeEvidence):
        await MergeController(read, write).preflight(record, evidence, evidence)


@pytest.mark.parametrize("queue_method", [None, "unknown", "rebase"])
async def test_preflight_rejects_queue_method_different_from_approval(queue_method):
    read, write, evidence, record = ready()
    protection = replace(
        read.merge_protections[evidence.repository.casefold(), "main"],
        merge_queue_enabled=True,
        merge_queue_method=queue_method,
    )
    read.merge_protections[evidence.repository.casefold(), "main"] = protection
    evidence = evidence.model_copy(
        update={
            "merge_method": "squash",
            "protection_digest": canonical_digest(asdict(protection)),
        }
    )
    with pytest.raises(StaleMergeEvidence):
        await MergeController(read, write).preflight(record, evidence, evidence)


async def test_optional_check_does_not_replace_or_block_required_checks():
    read, write, evidence, record = ready()
    key = evidence.repository.casefold(), evidence.head_sha
    read.checks[key] += (
        CheckSnapshot("optional", "completed", "failure", head_sha=evidence.head_sha),
    )
    assert (
        await MergeController(read, write).preflight(record, evidence, evidence)
        == record.pull_request
    )


async def test_merge_crash_reconciles_approved_head_without_another_write():
    read, write, evidence, record = ready()

    async def current():
        return evidence

    operation = MergeOperation(MergeController(read, write), record, uuid4(), evidence, current)
    write.crash_next_write = True
    with pytest.raises(FakeGitHubWriteCrash):
        await operation.invoke(intent(operation.request))
    # A second merge would fail because the fake PR is now closed.
    result = await operation.reconcile(intent(operation.request))
    assert result.payload["merged"] is True
    assert result.payload["merge_sha"] != evidence.head_sha
    assert (
        result.payload["merge_sha"]
        == write.pull_requests[evidence.repository, evidence.pull_request_number].merge_sha
    )


@pytest.mark.parametrize(
    "change",
    [
        "head",
        "base",
        "check",
        "check_conclusion",
        "protection",
        "local",
        "review_digest",
        "runner_mode",
        "runner_evidence_digest",
        "policy_version",
        "merge_method",
        "repository",
        "pull_request_number",
        "unresolved_blocking_findings",
    ],
)
async def test_changed_preflight_evidence_performs_zero_merge(change):
    read, write, evidence, record = ready()
    current_evidence = evidence
    calls = []
    original_merge = write.merge_pull_request

    async def merge(*args):
        calls.append(args)
        return await original_merge(*args)

    write.merge_pull_request = merge
    if change == "head":
        write.pull_requests[evidence.repository, 42] = replace(
            record.pull_request, head_sha="c" * 40
        )
    elif change == "base":
        read.bases[evidence.repository.casefold(), "main"] = "c" * 40
    elif change == "check":
        read.checks[evidence.repository.casefold(), "a" * 40] = ()
    elif change == "check_conclusion":
        read.checks[evidence.repository.casefold(), "a" * 40] = (
            CheckSnapshot("ci", "completed", "failure", head_sha=evidence.head_sha),
        )
    elif change == "protection":
        read.merge_protections[evidence.repository.casefold(), "main"] = MergeProtection(
            True, False, True, "strict", True
        )
    elif change == "local":
        current_evidence = evidence.model_copy(update={"validation_digest": "a" * 64})
    else:
        replacements = {
            "review_digest": "f" * 64,
            "runner_mode": "trusted_host",
            "runner_evidence_digest": "f" * 64,
            "policy_version": evidence.policy_version + 1,
            "merge_method": "rebase",
            "repository": "Other/Repo",
            "pull_request_number": evidence.pull_request_number + 1,
            "unresolved_blocking_findings": 1,
        }
        current_evidence = evidence.model_copy(update={change: replacements[change]})

    async def current():
        return current_evidence

    operation = MergeOperation(MergeController(read, write), record, uuid4(), evidence, current)
    outcome = await operation.invoke(intent(operation.request))
    assert outcome.status is OperationStatus.FAILED
    assert outcome.error == "merge_preflight_rejected"
    assert not write.pull_requests[evidence.repository, 42].merged
    assert calls == []


async def test_head_race_is_submitted_with_exact_sha_and_never_retried():
    read, write, evidence, record = ready()
    original = write.merge_pull_request
    calls = []

    async def raced(repository, number, sha, method):
        calls.append((sha, method))
        write.pull_requests[repository, number] = replace(record.pull_request, head_sha="c" * 40)
        return await original(repository, number, sha, method)

    write.merge_pull_request = raced

    async def current():
        return evidence

    operation = MergeOperation(MergeController(read, write), record, uuid4(), evidence, current)
    outcome = await operation.invoke(intent(operation.request))
    assert outcome.status is OperationStatus.FAILED
    assert outcome.error == "merge_remote_rejected"
    assert calls == [(evidence.head_sha, evidence.merge_method)]


async def test_uncertain_merge_response_is_never_classified_as_no_effect():
    read, write, evidence, record = ready()

    async def current():
        return evidence

    async def uncertain(*args):
        raise GitHubWriteError("uncertain")

    write.merge_pull_request = uncertain
    operation = MergeOperation(MergeController(read, write), record, uuid4(), evidence, current)
    with pytest.raises(GitHubWriteError, match="uncertain"):
        await operation.invoke(intent(operation.request))


async def test_second_queue_protection_read_failure_is_preflight_rejection():
    read, write, evidence, record = ready()
    protection_reads = 0
    merge_calls = []
    original_protection = read.get_merge_protection

    async def protection(*args):
        nonlocal protection_reads
        protection_reads += 1
        if protection_reads == 2:
            raise GitHubClientError("unavailable")
        return await original_protection(*args)

    async def merge(*args):
        merge_calls.append(args)
        raise AssertionError("preflight failure must not issue a merge write")

    async def current():
        return evidence

    read.get_merge_protection = protection
    write.merge_pull_request = merge
    operation = MergeOperation(MergeController(read, write), record, uuid4(), evidence, current)
    outcome = await operation.invoke(intent(operation.request))
    assert outcome.status is OperationStatus.FAILED
    assert outcome.error == "merge_preflight_rejected"
    assert protection_reads == 2 and merge_calls == []


async def test_preflight_pull_read_failure_is_preflight_rejection():
    read, write, evidence, record = ready()
    merge_calls = []

    async def unavailable_pull(*args):
        raise GitHubWriteError("unavailable")

    async def merge(*args):
        merge_calls.append(args)
        raise AssertionError("preflight failure must not issue a merge write")

    async def current():
        return evidence

    write.get_pull_request = unavailable_pull
    write.merge_pull_request = merge
    operation = MergeOperation(MergeController(read, write), record, uuid4(), evidence, current)
    outcome = await operation.invoke(intent(operation.request))
    assert outcome.status is OperationStatus.FAILED
    assert outcome.error == "merge_preflight_rejected"
    assert merge_calls == []


async def test_definite_provider_refusal_is_a_failed_receipt():
    read, write, evidence, record = ready()
    calls = []

    async def current():
        return evidence

    async def refused(*args):
        calls.append(args)
        raise GitHubWriteError("rejected")

    write.merge_pull_request = refused
    operation = MergeOperation(MergeController(read, write), record, uuid4(), evidence, current)
    result = await operation.invoke(intent(operation.request))
    assert result.status is OperationStatus.FAILED
    assert result.error == "merge_remote_rejected"
    assert len(calls) == 1
