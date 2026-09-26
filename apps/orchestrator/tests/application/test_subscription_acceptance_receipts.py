"""Mixed primary/worker receipt evidence never upgrades stale checks to current checks."""

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from forge.application.adapters.named_check_receipts import encode_command_result
from forge.application.ports.runner import CommandResult
from forge.application.ports.subscription_acceptance_receipts import AcceptanceReceiptSource
from forge.application.ports.subscription_candidate import CandidateInspection
from forge.application.ports.tool_recovery import VerifiedTerminalEffect, terminal_call_digest
from forge.application.ports.worktrees import GitSnapshotFile, GitWorkingTreeSnapshot
from forge.application.services.subscription_acceptance_receipts import (
    SubscriptionAcceptanceReceiptVerification,
)
from forge.domain.artifact import ArtifactDescriptor, canonical_storage_pointer
from forge.domain.policy import RunnerMode, StepKind
from forge.domain.subscription import SpecialistPurpose
from forge.domain.tool import ToolName
from test_subscription_handoff import Work, case


def mixed_case(*, before=None, after=None, exit_code=0):
    state, handoff_verifier, _, kwargs = case()
    call = next(iter(state.calls.values()))
    task = kwargs["task"]
    primary = replace(task, purpose=SpecialistPurpose.PRIMARY, parent_task_id=None)
    call = replace(call, subscription_purpose=primary.purpose.value)
    manifest = json.loads(next(iter(state.data.values())))
    candidate = CandidateInspection.from_snapshot(
        GitWorkingTreeSnapshot(
            head_sha=manifest["head_sha"],
            base_sha=manifest["base_sha"],
            files=tuple(GitSnapshotFile(**item) for item in manifest["files"]),
            changed_paths=tuple(manifest["changed_paths"]),
        )
    )
    worker = replace(task, task_id=uuid4(), parent_task_id=primary.task_id)
    identity, attempt = uuid4(), uuid4()
    result = CommandResult(
        command_name="unit",
        kind=StepKind.TEST,
        command_digest="a" * 64,
        policy_version=1,
        exit_code=exit_code,
        timed_out=False,
        started_at=datetime.now(UTC),
        duration_ms=25,
        stdout_digest="b" * 64,
        stderr_digest="c" * 64,
        runner_mode=RunnerMode.TRUSTED_HOST,
        image_digest=None,
        network_enabled=False,
        stdout_original_byte_count=0,
        stderr_original_byte_count=0,
        stdout_truncated=False,
        stderr_truncated=False,
        unsandboxed=True,
    )
    data = encode_command_result(result)
    digest = hashlib.sha256(data).hexdigest()
    descriptor = ArtifactDescriptor(
        digest=digest,
        media_type="application/json",
        byte_count=len(data),
        storage_path=canonical_storage_pointer(digest),
        run_id=task.run_id,
        producer_type="command_result",
        producer_id=identity,
    )
    check = replace(
        call,
        id=identity,
        subscription_task_id=worker.task_id,
        subscription_attempt_id=attempt,
        subscription_purpose=worker.purpose.value,
        tool_name=ToolName.BUILD_RUN_NAMED_CHECK,
        normalized_arguments={"command_name": "unit"},
        artifact_digests=(digest,),
        operation_intent_id=identity,
        result_metadata={
            "command_result_digest": digest,
            "candidate_tree_digest_before": before,
            "candidate_tree_digest_after": after,
        },
    )
    state.descriptors[digest], state.data[digest] = descriptor, data
    terminal = VerifiedTerminalEffect(
        effect_id=identity,
        call_digest=terminal_call_digest(check),
        intent_digests=((identity, "e" * 64),),
    )
    handoff_verifier._terminal.verify_terminal_effect.return_value = terminal
    proposal = SimpleNamespace(
        result_digest="d" * 64,
        decision=SimpleNamespace(run_id=task.run_id),
        policy=SimpleNamespace(version=1),
        worktree=SimpleNamespace(
            base_sha="a" * 40, identity=SimpleNamespace(worktree_name="worktree")
        ),
        review=SimpleNamespace(
            candidate=candidate, payload=lambda: {"candidate": candidate.payload()}
        ),
    )
    sources = (
        AcceptanceReceiptSource(call, primary, "b" * 64, "c" * 64),
        AcceptanceReceiptSource(check, worker, "d" * 64, "e" * 64),
    )
    verifier = SubscriptionAcceptanceReceiptVerification(
        lambda: Work(state),
        handoff_verifier._store,
        handoff_verifier._terminal,
    )
    return state, proposal, sources, verifier


@pytest.mark.parametrize("binding", ["both", "before", "after", "neither"])
async def test_mixed_receipts_preserve_producers_and_exact_check_candidate_binding(binding):
    _, proposal, _, _ = mixed_case()
    digest = proposal.review.candidate.tree_digest
    _, proposal, sources, verifier = mixed_case(
        before=digest if binding in {"both", "before"} else None,
        after=digest if binding in {"both", "after"} else None,
    )
    proof = await verifier._verify(proposal, sources)
    assert proof is not None
    snapshot, check = proof.receipts
    assert snapshot.attempt_id != check.attempt_id and snapshot.task_id != check.task_id
    assert snapshot.matches_candidate
    assert check.matches_candidate is (binding == "both")
    assert check.command_name == "unit"
    assert check.command_result_digest in dict(proof.artifact_proofs)
    assert type(proof).from_payload(proof.payload()) == proof


@pytest.mark.parametrize(
    "change", ["failed", "terminal", "result", "producer", "bytes", "oversize"]
)
async def test_unverifiable_check_receipts_never_produce_acceptance_proof(change):
    state, proposal, sources, verifier = mixed_case(exit_code=1 if change == "failed" else 0)
    check = sources[1].call
    digest = check.artifact_digests[0]
    if change == "terminal":
        verifier._terminal.verify_terminal_effect.return_value = None
    elif change == "result":
        changed = replace(
            check, result_metadata={**check.result_metadata, "command_result_digest": "f" * 64}
        )
        sources = (sources[0], replace(sources[1], call=changed))
    elif change == "producer":
        state.descriptors[digest] = replace(state.descriptors[digest], producer_id=uuid4())
    elif change == "bytes":
        state.data[digest] = b"forged"
    elif change == "oversize":
        state.descriptors[digest] = replace(
            state.descriptors[digest],
            byte_count=9 * 1024 * 1024,
            original_byte_count=9 * 1024 * 1024,
        )
    assert await verifier._verify(proposal, sources) is None


@pytest.mark.parametrize(
    "lineage", ["linked", "unlinked", "foreign_producer", "changed_bytes", "wrong_media"]
)
async def test_check_result_must_be_reachable_through_verified_receipt_lineage(lineage):
    state, proposal, sources, verifier = mixed_case()
    source = sources[1]
    check = source.call
    result_digest = check.artifact_digests[0]
    state.descriptors[result_digest] = replace(
        state.descriptors[result_digest],
        media_type="text/plain"
        if lineage == "wrong_media"
        else "application/vnd.forge.command-result+json",
    )
    # Production stores a named-check receipt as the tool's root artifact;
    # its command result is a parent artifact, not another top-level root.
    data = json.dumps({"schema_version": 3, "command_result_digest": result_digest}).encode()
    root = hashlib.sha256(data).hexdigest()
    state.data[root] = data
    state.descriptors[root] = ArtifactDescriptor(
        digest=root,
        media_type="application/json",
        byte_count=len(data),
        storage_path=canonical_storage_pointer(root),
        run_id=check.run_id,
        producer_type="named_check",
        producer_id=check.id,
        parent_digests=() if lineage == "unlinked" else (result_digest,),
    )
    if lineage == "foreign_producer":
        state.descriptors[result_digest] = replace(
            state.descriptors[result_digest], producer_id=uuid4()
        )
    elif lineage == "changed_bytes":
        state.data[result_digest] = b"changed command output"
    check = replace(check, artifact_digests=(root,))
    sources = (sources[0], replace(source, call=check))
    terminal = verifier._terminal.verify_terminal_effect.return_value
    verifier._terminal.verify_terminal_effect.return_value = replace(
        terminal, call_digest=terminal_call_digest(check)
    )
    proof = await verifier._verify(proposal, sources)
    assert (proof is not None) is (lineage == "linked")
    if proof is not None:
        assert proof.receipts[1].command_result_digest == result_digest
        assert {root, result_digest} <= dict(proof.artifact_proofs).keys()


@pytest.mark.parametrize("change", ["version", "extra", "nil", "boolean", "duplicate", "terminal"])
async def test_retained_receipt_codec_rejects_noncanonical_or_inconsistent_proofs(change):
    _, proposal, sources, verifier = mixed_case()
    proof = await verifier._verify(proposal, sources)
    assert proof is not None
    payload = proof.payload()
    if change == "version":
        payload["schema_version"] = True
    elif change == "extra":
        payload["approval"] = True
    elif change == "nil":
        payload["receipts"][0]["task_id"] = "00000000-0000-0000-0000-000000000000"
    elif change == "boolean":
        payload["receipts"][0]["matches_candidate"] = 1
    elif change == "duplicate":
        payload["receipts"].append(payload["receipts"][0])
    else:
        payload["receipts"][1]["call"]["terminal"] = None
    with pytest.raises(ValueError):
        type(proof).from_payload(payload)


async def test_commit_at_selected_head_does_not_certify_selected_working_tree():
    _, proposal, sources, verifier = mixed_case()
    check = sources[1].call
    commit = replace(
        check,
        tool_name=ToolName.GIT_COMMIT,
        normalized_arguments={"message": "checkpoint"},
        result_metadata={"new_sha": proposal.review.candidate.head_sha, "tree_sha": "a" * 40},
    )
    sources = (sources[0], replace(sources[1], call=commit))
    verifier._terminal.verify_terminal_effect.return_value = VerifiedTerminalEffect(
        effect_id=commit.id,
        call_digest=terminal_call_digest(commit),
        intent_digests=((commit.id, "e" * 64),),
    )
    proof = await verifier._verify(proposal, sources)
    assert proof is not None
    assert proof.receipts[1].commit_sha == proposal.review.candidate.head_sha
    assert not proof.receipts[1].matches_candidate
